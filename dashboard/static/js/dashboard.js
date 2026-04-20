// ── State ─────────────────────────────────────────────
let allAlerts     = [];
let activeFilter  = 'all';
let charts        = {};
let autoRefreshId = null;

// ── Chart Colours ─────────────────────────────────────
const COLORS = {
  purple:  'rgba(124,111,247,1)',
  purpleA: 'rgba(124,111,247,0.15)',
  green:   'rgba(46,213,115,1)',
  greenA:  'rgba(46,213,115,0.15)',
  orange:  'rgba(255,165,2,1)',
  orangeA: 'rgba(255,165,2,0.15)',
  red:     'rgba(255,71,87,1)',
  redA:    'rgba(255,71,87,0.15)',
};

// ── Chart Factory ─────────────────────────────────────
function makeChart(id, label, color, colorA) {
  const ctx = document.getElementById(id).getContext('2d');
  return new Chart(ctx, {
    type: 'line',
    data: {
      labels: [],
      datasets: [{
        label,
        data: [],
        borderColor: color,
        backgroundColor: colorA,
        borderWidth: 2,
        pointRadius: 0,
        fill: true,
        tension: 0.4,
      }]
    },
    options: {
      responsive: true,
      animation: { duration: 400 },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: ctx => ` ${ctx.parsed.y.toFixed(2)}`
          }
        }
      },
      scales: {
        x: {
          ticks: {
            color: '#7b82b0',
            maxTicksLimit: 6,
            font: { size: 10 }
          },
          grid: { color: 'rgba(46,50,80,0.5)' }
        },
        y: {
          ticks: { color: '#7b82b0', font: { size: 10 } },
          grid: { color: 'rgba(46,50,80,0.5)' }
        }
      }
    }
  });
}

function initCharts() {
  charts.tokens   = makeChart(
    'chartTokens',   'Tokens/min',  COLORS.purple, COLORS.purpleA
  );
  charts.cost     = makeChart(
    'chartCost',     '$/min',       COLORS.orange, COLORS.orangeA
  );
  charts.requests = makeChart(
    'chartRequests', 'Req/min',     COLORS.green,  COLORS.greenA
  );
  charts.anomaly  = makeChart(
    'chartAnomaly',  'Anomaly Score', COLORS.red,  COLORS.redA
  );

  // Doughnut for models
  const ctx = document.getElementById('chartModels').getContext('2d');
  charts.models = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: [],
      datasets: [{
        data: [],
        backgroundColor: [
          COLORS.purple, COLORS.green,
          COLORS.orange, COLORS.red,
          '#5352ed', '#eccc68'
        ],
        borderWidth: 0,
      }]
    },
    options: {
      responsive: true,
      cutout: '65%',
      plugins: {
        legend: {
          position: 'bottom',
          labels: {
            color: '#7b82b0',
            font: { size: 11 },
            boxWidth: 10,
          }
        }
      }
    }
  });
}

// ── Update Chart Data ─────────────────────────────────
function updateChart(chart, seriesData) {
  if (!seriesData || !seriesData.length) return;
  const KEEP = 60;                       // keep last 60 points
  const slice = seriesData.slice(-KEEP);

  chart.data.labels             = slice.map(p => p.time);
  chart.data.datasets[0].data   = slice.map(p => p.value);
  chart.update('none');
}

function updateModelChart(models) {
  if (!models || !Object.keys(models).length) return;
  const labels  = Object.keys(models);
  const values  = Object.values(models);

  charts.models.data.labels            = labels;
  charts.models.data.datasets[0].data  = values;
  charts.models.update('none');

  // Text list below chart
  const total = values.reduce((a, b) => a + b, 0);
  const list  = document.getElementById('modelList');
  list.innerHTML = labels.map((lbl, i) => {
    const pct = total > 0 ? ((values[i] / total) * 100).toFixed(0) : 0;
    return `
      <div class="model-row">
        <span class="model-name">${lbl}</span>
        <div class="model-bar-wrap">
          <div class="model-bar" style="width:${pct}%"></div>
        </div>
        <span class="model-rate">${values[i].toFixed(2)}/m</span>
      </div>`;
  }).join('');
}

// ── Stat Cards ────────────────────────────────────────
function updateStats(data) {
  const { metrics, alerts } = data;

  setText('statTotal',    alerts.total);
  setText('statCritical', alerts.critical);
  setText('statWarning',  alerts.warnings);
  setText('statTokenRate', fmt(metrics.token_rate, 0));
  setText('statCostRate',  `$${fmt(metrics.cost_rate, 4)}`);
  setText('statErrorRate', `${fmt(metrics.error_rate, 1)}%`);
  setText('statReqRate',   fmt(metrics.request_rate, 1));
  setText('statLatency',   `${fmt(metrics.avg_latency, 3)}s`);
  setText('lastUpdated',   data.last_updated);

  // Colour error rate card
  const errEl = document.getElementById('statErrorRate');
  errEl.style.color = metrics.error_rate > 10
    ? 'var(--critical)'
    : metrics.error_rate > 5
      ? 'var(--warning)'
      : 'var(--success)';
}

// ── Service Health Badges ─────────────────────────────
function updateHealth(health) {
  const map = {
    'badge-llm':        health.services?.llm_app,
    'badge-detector':   health.services?.anomaly_detector,
    'badge-prometheus': health.services?.prometheus,
  };
  for (const [id, status] of Object.entries(map)) {
    const el = document.getElementById(id);
    if (!el) continue;
    el.classList.remove('up', 'down');
    el.classList.add(status === 'up' ? 'up' : 'down');
  }

  // Health panel
  const names = {
    llm_app:          '🤖 LLM Application',
    anomaly_detector: '🔍 Anomaly Detector',
    prometheus:       '📊 Prometheus',
  };
  const list = document.getElementById('healthList');
  list.innerHTML = Object.entries(health.services || {}).map(
    ([k, v]) => `
    <div class="health-row">
      <span class="health-name">${names[k] || k}</span>
      <span class="health-status ${v === 'up' ? 'status-up' : 'status-down'}">
        ${v === 'up' ? '● UP' : '● DOWN'}
      </span>
    </div>`
  ).join('');
}

// ── Users Panel ───────────────────────────────────────
function updateUsers(users, topUsers) {
  const flagged = new Set(
    (users || []).map(u => u.toLowerCase())
  );
  const list = document.getElementById('userList');

  if (!topUsers || !topUsers.length) {
    list.innerHTML = '<div class="no-alerts"><p>No user data yet</p></div>';
    return;
  }

  list.innerHTML = topUsers.map((u, i) => {
    const isSusp = u.user.includes('bot')
                || u.user.includes('suspicious')
                || u.user.includes('malicious')
                || u.user.includes('attacker');
    const cls  = isSusp ? 'suspicious' : i === 0 ? 'active' : '';
    const tag  = isSusp
      ? '<span class="user-tag tag-suspicious">⚠ Suspicious</span>'
      : i === 0
        ? '<span class="user-tag tag-active">Top</span>'
        : '';

    return `
      <div class="user-row ${cls}">
        <span class="user-id">${u.user}</span>
        ${tag}
        <span class="user-rate">${u.rate.toFixed(2)}/m</span>
      </div>`;
  }).join('');
}

// ── Alert Cards ───────────────────────────────────────
function renderAlerts(items) {
  const container = document.getElementById('alertsContainer');

  if (!items || !items.length) {
    container.innerHTML = `
      <div class="no-alerts">
        <div class="icon">✅</div>
        <p>No anomalies detected yet.<br>
           Run the traffic simulator to generate anomalies.</p>
      </div>`;
    return;
  }

  container.innerHTML = items.map((a, idx) => {
    const sevClass = a.severity === 'critical'
      ? 'sev-critical' : 'sev-warning';
    const sevIcon  = a.severity === 'critical' ? '🔴' : '🟡';
    const typeIcon = a.type.includes('ml') ? '🤖' : '📊';

    // Z-score or anomaly score pill
    const scorePill = a.z_score != null
      ? `<span class="alert-stat-pill highlight">
           Z-score: ${a.z_score}
         </span>`
      : a.anomaly_score != null
        ? `<span class="alert-stat-pill highlight">
             ML Score: ${a.anomaly_score}
           </span>`
        : '';

    // Abnormal metrics pills
    const metricPills = (a.abnormal_metrics || []).map(m =>
      `<span class="alert-stat-pill">${m}</span>`
    ).join('');

    // Context users
    const userPills = (a.context.top_users || [])
      .slice(0, 3)
      .map((u, i) =>
        `<span class="ctx-user ${i === 0 ? 'top' : ''}">
           ${i === 0 ? '👑' : '👤'} ${u.user}
           <small>(${u.rate.toFixed(2)}/m)</small>
         </span>`
      ).join('');

    return `
    <div class="alert-card ${a.severity}"
         onclick="openModal(${idx})">
      <div class="alert-header">
        <div class="alert-title">
          <span class="severity-badge ${sevClass}">
            ${sevIcon} ${a.severity}
          </span>
          <span class="alert-metric">${a.metric}</span>
        </div>
        <div class="alert-meta">
          <span class="alert-type-badge">${typeIcon} ${a.type}</span>
          <span class="alert-time">🕐 ${a.timestamp}</span>
        </div>
      </div>

      <div class="alert-body">
        <div>
          <div class="alert-section-title">🔗 Root Cause</div>
          <div class="root-cause-text">${a.root_cause}</div>

          <div style="margin-top:10px">
            <div class="alert-section-title">📊 Metrics</div>
            <div class="alert-stats">
              ${scorePill}
              ${metricPills}
            </div>
          </div>
        </div>

        <div>
          <div class="alert-section-title">✅ Recommendation</div>
          <div class="recommendation-text">${a.recommendation}</div>

          <div style="margin-top:10px">
            <div class="alert-section-title">👤 Top Users</div>
            <div>${userPills || '<span style="color:var(--text-dim);font-size:12px">No user data</span>'}</div>
          </div>
        </div>
      </div>
    </div>`;
  }).join('');
}

// ── Modal ─────────────────────────────────────────────
function openModal(idx) {
  const visible = getFilteredAlerts();
  const a = visible[idx];
  if (!a) return;

  const modelRows = Object.entries(a.context.models || {}).map(
    ([m, r]) => `
    <tr>
      <td style="padding:6px 10px;font-family:monospace">${m}</td>
      <td style="padding:6px 10px;color:var(--text-dim)">${r.toFixed(2)}/min</td>
    </tr>`
  ).join('');

  document.getElementById('modalContent').innerHTML = `
    <h3 style="margin-bottom:16px;font-size:16px">
      🔍 Full Anomaly Report
    </h3>

    <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:16px">
      <span class="severity-badge ${a.severity === 'critical' ? 'sev-critical' : 'sev-warning'}">
        ${a.severity.toUpperCase()}
      </span>
      <span class="alert-stat-pill">${a.metric}</span>
      <span class="alert-stat-pill">${a.type}</span>
      <span class="alert-stat-pill">🕐 ${a.timestamp}</span>
    </div>

    <div style="margin-bottom:14px">
      <div class="alert-section-title">Detection Message</div>
      <div class="root-cause-text" style="font-family:monospace;font-size:12px">
        ${a.message}
      </div>
    </div>

    <div style="margin-bottom:14px">
      <div class="alert-section-title">🔗 Root Cause Hypothesis</div>
      <div class="root-cause-text">${a.root_cause}</div>
    </div>

    <div style="margin-bottom:14px">
      <div class="alert-section-title">✅ Recommended Actions</div>
      <div class="recommendation-text">${a.recommendation}</div>
    </div>

    <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px">
      <div>
        <div class="alert-section-title">📊 Context Metrics</div>
        <div class="alert-stats" style="flex-direction:column;gap:6px">
          <span class="alert-stat-pill">
            Error Rate: ${a.context.error_rate}%
          </span>
          <span class="alert-stat-pill">
            Latency: ${a.context.latency}s
          </span>
        </div>
      </div>

      <div>
        <div class="alert-section-title">🧠 Model Usage</div>
        <table style="width:100%;border-collapse:collapse">
          ${modelRows || '<tr><td style="color:var(--text-dim);font-size:12px;padding:6px 10px">No data</td></tr>'}
        </table>
      </div>
    </div>

    <div>
      <div class="alert-section-title">👤 Top Users at Time of Alert</div>
      <div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:6px">
        ${(a.context.top_users || []).map((u, i) => `
          <span class="ctx-user ${i === 0 ? 'top' : ''}">
            ${i === 0 ? '👑' : '👤'} ${u.user}
            &nbsp;${u.rate.toFixed(2)}/min
          </span>
        `).join('') || '<span style="color:var(--text-dim);font-size:12px">No user data</span>'}
      </div>
    </div>`;

  document.getElementById('modal').classList.add('open');
}

function closeModal() {
  document.getElementById('modal').classList.remove('open');
}

// ── Filter ────────────────────────────────────────────
function getFilteredAlerts() {
  if (activeFilter === 'all')         return allAlerts;
  if (activeFilter === 'critical')    return allAlerts.filter(a => a.severity === 'critical');
  if (activeFilter === 'warning')     return allAlerts.filter(a => a.severity === 'warning');
  if (activeFilter === 'statistical') return allAlerts.filter(a => a.type === 'statistical');
  if (activeFilter === 'ml')          return allAlerts.filter(a => a.type.includes('ml'));
  return allAlerts;
}

function filterAlerts(filter, btn) {
  activeFilter = filter;
  document.querySelectorAll('.filter-btn')
          .forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  renderAlerts(getFilteredAlerts());
}

// ── Main Fetch ────────────────────────────────────────
async function fetchDashboard() {
  try {
    // Fetch dashboard data
    const [dashRes, healthRes] = await Promise.all([
      fetch('/api/dashboard'),
      fetch('/api/health')
    ]);

    const dash   = await dashRes.json();
    const health = await healthRes.json();

    // Update everything
    updateStats(dash);
    updateChart(charts.tokens,   dash.charts.token_rate);
    updateChart(charts.cost,     dash.charts.cost_rate);
    updateChart(charts.requests, dash.charts.request_rate);
    updateChart(charts.anomaly,  dash.charts.anomaly_score);
    updateModelChart(dash.metrics.models);
    updateUsers(dash.flagged_users, dash.metrics.top_users);
    updateHealth(health);

    // Store alerts and render
    allAlerts = dash.alerts.items || [];
    renderAlerts(getFilteredAlerts());

  } catch (err) {
    console.error('Dashboard fetch failed:', err);
  }
}

// ── Auto-Refresh ──────────────────────────────────────
function startAutoRefresh(intervalMs = 10000) {
  if (autoRefreshId) clearInterval(autoRefreshId);
  autoRefreshId = setInterval(fetchDashboard, intervalMs);
}

// ── Init ──────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  initCharts();
  fetchDashboard();
  startAutoRefresh(10000);   // refresh every 10 seconds
});

// ── Helpers ───────────────────────────────────────────
function setText(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val ?? '—';
}

function fmt(val, dec = 2) {
  if (val == null || isNaN(val)) return '—';
  return Number(val).toFixed(dec);
}
