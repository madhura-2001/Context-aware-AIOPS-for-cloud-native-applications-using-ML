import os
import time
import threading
import logging
import hashlib
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import requests
from flask import Flask, jsonify
from prometheus_client import Counter, Gauge, generate_latest, CONTENT_TYPE_LATEST
from sklearn.ensemble import IsolationForest
from collections import deque

# ============================================================
# CONFIG
# ============================================================

PROMETHEUS_URL        = os.environ.get("PROMETHEUS_URL", "http://prometheus-server.aiops.svc.cluster.local")
CHECK_INTERVAL        = int(os.environ.get("CHECK_INTERVAL", "30"))
DEDUP_WINDOW_SECONDS  = int(os.environ.get("DEDUP_WINDOW_SECONDS", "120"))

# ============================================================
# FLASK + LOGGING
# ============================================================

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("anomaly-detector")

# ============================================================
# PROMETHEUS METRICS (self-telemetry)
# ============================================================

ANOMALIES_DETECTED  = Counter('aiops_anomalies_detected_total',  'Total anomalies detected',         ['anomaly_type', 'severity'])
ANOMALY_SCORE       = Gauge  ('aiops_anomaly_score',              'Current anomaly score',             ['metric_name'])
DETECTOR_RUNS       = Counter('aiops_detector_runs_total',        'Total detection cycles run')

# LLM app metrics
TOKEN_RATE_G    = Gauge('aiops_token_rate_per_minute',   'Token rate/min')
REQUEST_RATE_G  = Gauge('aiops_request_rate_per_minute', 'Request rate/min')
ERROR_RATE_G    = Gauge('aiops_error_rate_percent',      'Error rate %')
COST_RATE_G     = Gauge('aiops_cost_rate_per_minute',    'Cost rate $/min')

# System metrics (our app's own resource usage)
CPU_USAGE_G     = Gauge('aiops_app_cpu_usage_percent',       'App CPU usage %')
MEM_RSS_G       = Gauge('aiops_app_memory_rss_mb',           'App RSS memory MB')
MEM_PCT_G       = Gauge('aiops_app_memory_percent',          'App memory usage %')
GC_PAUSE_G      = Gauge('aiops_app_gc_pause_seconds',        'GC pause time seconds')
THREAD_COUNT_G  = Gauge('aiops_app_thread_count',            'App thread count')
FD_COUNT_G      = Gauge('aiops_app_open_fds',                'Open file descriptors')
NET_RX_G        = Gauge('aiops_app_network_rx_bytes_per_sec','Network RX bytes/sec')
NET_TX_G        = Gauge('aiops_app_network_tx_bytes_per_sec','Network TX bytes/sec')
DISK_READ_G     = Gauge('aiops_app_disk_read_bytes_per_sec', 'Disk read bytes/sec')
DISK_WRITE_G    = Gauge('aiops_app_disk_write_bytes_per_sec','Disk write bytes/sec')
GOROUTINES_G    = Gauge('aiops_app_goroutines',              'Goroutine / worker count')
QUEUE_DEPTH_G   = Gauge('aiops_app_queue_depth',             'Request queue depth')

# ============================================================
# STATE
# ============================================================

recent_alerts: deque      = deque(maxlen=200)
_alert_dedup: dict        = {}
_last_metrics: dict       = {}
_baseline_model: IsolationForest | None = None
_baseline_trained_at: float = 0
BASELINE_RETRAIN_INTERVAL = int(os.environ.get("BASELINE_RETRAIN_INTERVAL", "3600"))  # retrain every 1 hour
BASELINE_MIN_SAMPLES = int(os.environ.get("BASELINE_MIN_SAMPLES", "60"))  # need 60 clean points (~30 min)


# All metric streams we track (LLM + system)
historical_data = {
    # LLM metrics
    "token_rates":      deque(maxlen=200),
    "request_rates":    deque(maxlen=200),
    "error_rates":      deque(maxlen=200),
    "cost_rates":       deque(maxlen=200),
    "latency_avgs":     deque(maxlen=200),
    # System / infrastructure
    "cpu_usage":        deque(maxlen=200),
    "mem_rss_mb":       deque(maxlen=200),
    "mem_pct":          deque(maxlen=200),
    "gc_pause":         deque(maxlen=200),
    "thread_count":     deque(maxlen=200),
    "fd_count":         deque(maxlen=200),
    "net_rx":           deque(maxlen=200),
    "net_tx":           deque(maxlen=200),
    "disk_read":        deque(maxlen=200),
    "disk_write":       deque(maxlen=200),
    "queue_depth":      deque(maxlen=200),
}

# ============================================================
# CORRELATION MAP
# (describes which system metric is affected by which LLM metric)
# Used by RCA to find cross-metric root causes.
# ============================================================

CORRELATION_RULES = [
    # (trigger_metric_pattern, correlated_system_metric, explanation)
    ("token",    "cpu_usage",    "High token throughput drives CPU-intensive tokenisation and inference"),
    ("token",    "mem_rss_mb",   "Large token batches require proportionally more heap memory"),
    ("token",    "net_tx",       "Bigger completions mean more bytes sent over the wire"),
    ("request",  "cpu_usage",    "High request rate increases per-request CPU overhead (auth, routing, serialisation)"),
    ("request",  "thread_count", "Thread-per-request models spawn new threads under traffic spikes"),
    ("request",  "queue_depth",  "Queue builds up when arrival rate exceeds processing capacity"),
    ("request",  "fd_count",     "Each HTTP connection consumes a file descriptor"),
    ("error",    "gc_pause",     "Error handling paths often allocate extra objects, triggering GC"),
    ("latency",  "cpu_usage",    "High CPU can cause slow response processing and inflate latency"),
    ("latency",  "gc_pause",     "Long GC pauses add directly to response latency (stop-the-world)"),
    ("latency",  "queue_depth",  "Deep request queues increase wait time before processing begins"),
    ("cost",     "net_tx",       "Expensive high-token responses transfer more data out"),
]

# ============================================================
# HELPERS
# ============================================================

def safe_float(v, default=0.0) -> float:
    try:
        f = float(v)
        return default if (np.isnan(f) or np.isinf(f)) else f
    except (TypeError, ValueError):
        return default

def _fingerprint(atype: str, metric: str) -> str:
    return hashlib.md5(f"{atype}:{metric}".encode()).hexdigest()[:12]

def _is_duplicate(fp: str) -> bool:
    now = time.time()
    if now - _alert_dedup.get(fp, 0) < DEDUP_WINDOW_SECONDS:
        return True
    _alert_dedup[fp] = now
    return False

# ============================================================
# PROMETHEUS QUERIES
# ============================================================

def query_prometheus(query):
    try:
        r = requests.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": query}, timeout=10)
        data = r.json()
        if data["status"] == "success" and data["data"]["result"]:
            return data["data"]["result"]
    except Exception as e:
        logger.error(f"Prometheus query failed: {e}")
    return []

def get_metric_value(query, default=0.0) -> float:
    result = query_prometheus(query)
    if result:
        try:
            return safe_float(result[0]["value"][1], default)
        except (IndexError, KeyError):
            pass
    return default

def get_latency_safe() -> float:
    count = get_metric_value('sum(rate(llm_request_duration_seconds_count[2m]))')
    if count == 0:
        return 0.0
    total = get_metric_value('sum(rate(llm_request_duration_seconds_sum[2m]))')
    return safe_float(total / count)

# ============================================================
# COLLECT ALL METRICS
# ============================================================

def collect_metrics() -> dict:
    """Collect LLM + system metrics in one shot."""

    # --- LLM ---
    token_rate   = get_metric_value('sum(rate(llm_total_tokens_sum[2m])) * 60')
    request_rate = get_metric_value('sum(rate(llm_requests_total[2m])) * 60')
    total_rate   = get_metric_value('sum(rate(llm_requests_total[2m]))')
    error_raw    = get_metric_value('sum(rate(llm_requests_total{status="error"}[2m]))')
    error_pct    = (error_raw / total_rate * 100) if total_rate > 0 else 0.0
    cost_rate    = get_metric_value('sum(rate(llm_estimated_cost_dollars_total[2m])) * 60')
    latency_avg  = get_latency_safe()

    # --- System / app resource metrics ---
    # CPU: process_cpu_seconds_total is standard in Python prometheus_client
    cpu_usage    = get_metric_value('rate(process_cpu_seconds_total{job="llm-app"}[2m]) * 100')
    mem_rss_mb   = get_metric_value('process_resident_memory_bytes{job="llm-app"} / 1024 / 1024')
    mem_pct      = get_metric_value(
        'process_resident_memory_bytes{job="llm-app"} / '
        'node_memory_MemTotal_bytes * 100'
    )
    gc_pause     = get_metric_value('rate(go_gc_duration_seconds_sum{job="llm-app"}[2m])')
    thread_count = get_metric_value('process_num_threads{job="llm-app"}')
    fd_count     = get_metric_value('process_open_fds{job="llm-app"}')
    net_rx       = get_metric_value('rate(container_network_receive_bytes_total{pod=~"llm-app.*"}[2m])')
    net_tx       = get_metric_value('rate(container_network_transmit_bytes_total{pod=~"llm-app.*"}[2m])')
    disk_read    = get_metric_value('rate(container_fs_reads_bytes_total{pod=~"llm-app.*"}[2m])')
    disk_write   = get_metric_value('rate(container_fs_writes_bytes_total{pod=~"llm-app.*"}[2m])')
    queue_depth  = get_metric_value('llm_request_queue_depth{job="llm-app"}')

    return dict(
        token_rate=token_rate, request_rate=request_rate,
        error_pct=error_pct, cost_rate=cost_rate, latency_avg=latency_avg,
        cpu_usage=cpu_usage, mem_rss_mb=mem_rss_mb, mem_pct=mem_pct,
        gc_pause=gc_pause, thread_count=thread_count, fd_count=fd_count,
        net_rx=net_rx, net_tx=net_tx,
        disk_read=disk_read, disk_write=disk_write,
        queue_depth=queue_depth,
    )

# ============================================================
# DETECTION
# ============================================================

def detect_statistical(data_points, metric_name, threshold=2.5):
    if len(data_points) < 10:
        return None
    values = list(data_points)
    current, hist = values[-1], values[:-1]
    mean, std = np.mean(hist), np.std(hist)
    if std == 0:
        return None
    z = (current - mean) / std
    if abs(z) <= threshold:
        return None
    direction = "above" if z > 0 else "below"
    return {
        "type": "statistical", "metric": metric_name,
        "severity": "critical" if abs(z) > 4 else "warning",
        "current_value": round(current, 4), "mean": round(mean, 4),
        "std": round(std, 4), "z_score": round(z, 2), "direction": direction,
        "message": (
            f"{metric_name} is {abs(z):.1f}σ {direction} normal "
            f"(current={current:.3f}, baseline={mean:.3f}±{std:.3f})"
        )
    }

def _train_baseline(df: pd.DataFrame):
    global _baseline_model, _baseline_trained_at
    model = IsolationForest(contamination=0.02, random_state=42, n_estimators=200)
    model.fit(df)
    _baseline_model = model
    _baseline_trained_at = time.time()
    logger.info(f"Baseline model trained on {len(df)} samples.")
    
def detect_ml_anomalies():
    """Isolation Forest over ALL metrics (LLM + system)."""
    col_order = list(historical_data.keys())
    lengths = [len(historical_data[k]) for k in col_order]
    if min(lengths) < BASELINE_MIN_SAMPLES:
        logger.debug(f"Waiting for baseline: {min(lengths)}/{BASELINE_MIN_SAMPLES} samples")
        return []

    n = min(lengths)
    raw = np.column_stack([list(historical_data[k])[-n:] for k in col_order])

    # Impute NaN/Inf with column medians
    for c in range(raw.shape[1]):
        bad = ~np.isfinite(raw[:, c])
        raw[bad, c] = np.nanmedian(raw[:, c]) if np.any(~bad) else 0.0

    df = pd.DataFrame(raw, columns=col_order)
    
    # ============================================================
    # BASELINE TRAINING + SCORING
    # ============================================================

    global _baseline_model, _baseline_trained_at

    # Train baseline if needed
    if (
        _baseline_model is None
        and len(df) >= BASELINE_MIN_SAMPLES
    ):
        _train_baseline(df.iloc[:-5])

    elif (
        _baseline_model is not None
        and time.time() - _baseline_trained_at > BASELINE_RETRAIN_INTERVAL
    ):
        _train_baseline(df.iloc[:-5])

    # If no model yet → skip
    if _baseline_model is None:
        return []

    # Score only latest point
    new_point = df.iloc[[-1]]

    score = float(_baseline_model.score_samples(new_point)[0])
    is_anomaly = _baseline_model.predict(new_point)[0] == -1

    if not is_anomaly:
        return []

    baseline_df = df.iloc[:-5]
    row, means, stds = df.iloc[-1], baseline_df.mean(), baseline_df.std()
    abnormal = {}
    for col in col_order:
        if stds[col] > 0:
            z = (row[col] - means[col]) / stds[col]
            if abs(z) > 1.5:
                abnormal[col] = {
                    "current":   round(float(row[col]), 4),
                    "baseline":  round(float(means[col]), 4),
                    "z_score":   round(float(z), 2),
                    "direction": "above" if z > 0 else "below"
                }

    primary = max(abnormal, key=lambda k: abs(abnormal[k]["z_score"])) if abnormal else "unknown"

    # Find correlated metric pairs (LLM -> system)
    correlations = _find_correlations(abnormal)

    return [{
        "type":                "ml_isolation_forest",
        "severity":            "critical" if score < -0.3 else "warning",
        "anomaly_score":       round(score, 4),
        "primary_metric":      primary,
        "abnormal_metrics":    abnormal,
        "correlations_found":  correlations,
        "message":             _ml_message(primary, abnormal, score)
    }]


    
def _find_correlations(abnormal: dict) -> list:
    """Check if abnormal LLM metrics have corresponding system metric anomalies."""
    found = []
    for rule_trigger, rule_system, explanation in CORRELATION_RULES:
        # Is the trigger metric abnormal?
        trigger_hit = any(rule_trigger in k for k in abnormal)
        # Is the system metric also abnormal?
        system_hit  = any(rule_system in k for k in abnormal)
        if trigger_hit and system_hit:
            trigger_key = next((k for k in abnormal if rule_trigger in k), "?")
            system_key  = next((k for k in abnormal if rule_system  in k), "?")
            found.append({
                "cause":       trigger_key,
                "effect":      system_key,
                "explanation": explanation,
                "cause_z":     abnormal[trigger_key]["z_score"],
                "effect_z":    abnormal[system_key]["z_score"],
            })
    return found


def _ml_message(primary, abnormal, score):
    if not abnormal:
        return f"ML anomaly (score={score:.3f}) — no dominant metric"
    parts = [
        f"{m}={i['current']:.3f} ({i['direction']} {i['baseline']:.3f}, z={i['z_score']:+.1f})"
        for m, i in sorted(abnormal.items(), key=lambda x: -abs(x[1]["z_score"]))
    ]
    return f"ML anomaly score={score:.3f}. Primary={primary}. Deviating: {' | '.join(parts)}"

# ============================================================
# ROOT CAUSE ANALYSIS
# ============================================================

def build_context(anomaly_info: dict) -> dict:
    top_users = query_prometheus('topk(5, sum by (user_id) (rate(llm_requests_per_user_total[2m])))')
    model_usage = query_prometheus('sum by (model) (rate(llm_requests_total[2m]))')
    error_breakdown = query_prometheus('topk(5, sum by (error_code) (rate(llm_requests_total{status="error"}[2m])))')

    users  = [{"user": r["metric"].get("user_id","?"), "rate": safe_float(r["value"][1])} for r in (top_users or [])]
    models = {r["metric"].get("model","?"): safe_float(r["value"][1]) for r in (model_usage or [])}
    errors = {r["metric"].get("error_code","?"): safe_float(r["value"][1]) for r in (error_breakdown or [])}

    snap = _last_metrics
    ctx = {
        "top_users":              users,
        "model_usage":            models,
        "error_breakdown":        errors,
        "current_token_rate":     snap.get("token_rate",   0.0),
        "current_request_rate":   snap.get("request_rate", 0.0),
        "current_error_rate":     snap.get("error_pct",    0.0),
        "current_avg_latency":    snap.get("latency_avg",  0.0),
        "current_cost_rate":      snap.get("cost_rate",    0.0),
        # system
        "current_cpu_pct":        snap.get("cpu_usage",    0.0),
        "current_mem_rss_mb":     snap.get("mem_rss_mb",   0.0),
        "current_mem_pct":        snap.get("mem_pct",      0.0),
        "current_gc_pause_s":     snap.get("gc_pause",     0.0),
        "current_thread_count":   snap.get("thread_count", 0),
        "current_fd_count":       snap.get("fd_count",     0),
        "current_net_tx_bps":     snap.get("net_tx",       0.0),
        "current_queue_depth":    snap.get("queue_depth",  0),
    }

    metric = (anomaly_info.get("metric","") or anomaly_info.get("primary_metric","")).lower()
    correlations = anomaly_info.get("correlations_found", [])
    rca, rec = _rca_for_metric(metric, anomaly_info, ctx, correlations)

    return {
        "timestamp":             datetime.utcnow().isoformat() + "Z",
        "anomaly":               anomaly_info,
        "context":               ctx,
        "root_cause_hypothesis": rca,
        "recommendation":        rec,
        "correlation_chain":     _format_correlation_chain(correlations),
    }


def _format_correlation_chain(correlations: list) -> str:
    if not correlations:
        return ""
    lines = []
    for c in correlations:
        lines.append(
            f"  {c['cause']} (z={c['cause_z']:+.1f}) → {c['effect']} (z={c['effect_z']:+.1f}): "
            f"{c['explanation']}"
        )
    return "Confirmed causal chains:\n" + "\n".join(lines)


def _rca_for_metric(metric: str, anomaly: dict, ctx: dict, correlations: list):
    users       = ctx["top_users"]
    models      = ctx["model_usage"]
    errors      = ctx["error_breakdown"]
    top_user    = users[0]["user"] if users else None
    top_rate    = users[0]["rate"] if users else 0.0
    error_pct   = ctx["current_error_rate"]
    cpu_pct     = ctx["current_cpu_pct"]
    mem_mb      = ctx["current_mem_rss_mb"]
    gc_pause    = ctx["current_gc_pause_s"]
    threads     = ctx["current_thread_count"]
    queue       = ctx["current_queue_depth"]

    corr_str = ""
    if correlations:
        corr_str = (
            "\n\nSystem-level confirmation: "
            + "; ".join(f"{c['cause']}→{c['effect']}" for c in correlations)
        )

    # ---------- CPU HIGH ----------
    if "cpu" in metric:
        pct = _pct_above(anomaly, "cpu_usage")
        rca = (
            f"App CPU usage is {pct:.0f}% above baseline (currently {cpu_pct:.1f}%). "
            f"The most common causes in LLM serving: (1) tokenisation CPU for large batches, "
            f"(2) request rate spike forcing concurrent processing, "
            f"(3) JSON serialisation of huge response payloads."
        )
        # enrich with correlated LLM signal
        if any("token" in c["cause"] for c in correlations):
            rca += f" Correlated token_rate spike strongly suggests cause #1."
        if any("request" in c["cause"] for c in correlations):
            rca += f" Correlated request_rate spike points to cause #2."
        rec = (
            "1. Profile CPU — identify whether tokeniser, HTTP handler, or serialiser dominates.\n"
            "2. Enable response streaming to distribute CPU load over time.\n"
            "3. Add horizontal pod autoscaling (HPA) with CPU target 70%.\n"
            "4. If token spike is the driver, enforce per-user max_tokens.\n"
            f"5. Current request rate: {ctx['current_request_rate']:.1f}/min — "
            f"{'scale NOW' if ctx['current_request_rate'] > 1000 else 'monitor closely'}."
        )

    # ---------- MEMORY HIGH ----------
    elif "mem" in metric:
        pct = _pct_above(anomaly, "mem_rss_mb")
        rca = (
            f"App RSS memory is {pct:.0f}% above baseline ({mem_mb:.0f} MB currently). "
            f"Memory pressure in LLM apps is typically caused by: "
            f"(1) buffering large completions before sending, "
            f"(2) prompt/completion caching growing unbounded, "
            f"(3) a memory leak accumulating over time (gradual upward drift)."
        )
        if any("token" in c["cause"] for c in correlations):
            rca += " The correlated token spike suggests large buffered completions are filling the heap."
        rec = (
            "1. Check if memory is growing monotonically (leak) or spiked with traffic.\n"
            "2. Audit in-memory caches — cap with LRU and a max-size.\n"
            "3. Switch to streaming responses to avoid buffering full completions.\n"
            f"4. If memory % is {ctx['current_mem_pct']:.1f}% of node — consider OOM risk.\n"
            "5. Set container memory limit + request correctly so k8s scheduler places pod on right node."
        )

    # ---------- GC PAUSE HIGH ----------
    elif "gc" in metric:
        pct = _pct_above(anomaly, "gc_pause")
        rca = (
            f"GC pause time is {pct:.0f}% above baseline ({gc_pause*1000:.1f}ms). "
            f"Long GC pauses directly add to p99 latency because the JVM/runtime "
            f"stops all threads. Common triggers: rapid object allocation in error "
            f"paths, unbounded cache growth, or large prompt strings being copied repeatedly."
        )
        if any("latency" in c["effect"] for c in correlations):
            rca += " This is confirmed as a latency driver by the correlated latency anomaly."
        rec = (
            "1. Add GC tuning flags (e.g. G1GC, lower GC target pause).\n"
            "2. Profile allocation hotspots — likely in request/response serialisation.\n"
            "3. Pre-allocate buffers for known response size classes.\n"
            "4. If error_rate is elevated, error-path allocations may be triggering GC.\n"
            "5. Monitor heap occupancy — if consistently >80%, add memory or reduce object retention."
        )

    # ---------- THREAD COUNT HIGH ----------
    elif "thread" in metric:
        pct = _pct_above(anomaly, "thread_count")
        rca = (
            f"Thread count is {pct:.0f}% above baseline ({threads:.0f} threads). "
            f"Thread explosion usually means: (1) thread-per-request model under a traffic spike, "
            f"(2) a thread leak where request threads are not returned to the pool on timeout, "
            f"(3) a stuck downstream call (e.g. LLM API timeout) holding threads open."
        )
        if any("request" in c["cause"] for c in correlations):
            rca += " The simultaneous request_rate spike confirms cause #1 (traffic-driven)."
        rec = (
            "1. Switch to async/non-blocking I/O — threads should wait on events, not be blocked.\n"
            "2. Set a hard thread pool cap and a request queue with back-pressure.\n"
            "3. Add client-side timeouts to the LLM API call (e.g. 30s) so threads are released.\n"
            "4. Alert if thread count exceeds 80% of pool limit."
        )

    # ---------- QUEUE DEPTH HIGH ----------
    elif "queue" in metric:
        pct = _pct_above(anomaly, "queue_depth")
        rca = (
            f"Request queue depth is {pct:.0f}% above baseline ({queue:.0f} items). "
            f"Queue buildup means arrival rate > processing capacity. "
            f"Downstream effect: users experience high latency even when no error is returned."
        )
        rec = (
            "1. Scale up worker replicas immediately.\n"
            "2. Return 503 (service busy) instead of queuing indefinitely — fail fast.\n"
            "3. Implement priority queuing to protect premium users.\n"
            "4. Add queue-depth based HPA trigger in addition to CPU.\n"
            f"5. Current latency: {ctx['current_avg_latency']:.2f}s — "
            f"{'CRITICAL: already degraded' if ctx['current_avg_latency'] > 5 else 'not yet user-visible'}."
        )

    # ---------- FILE DESCRIPTORS HIGH ----------
    elif "fd" in metric:
        pct = _pct_above(anomaly, "fd_count")
        rca = (
            f"Open file descriptors are {pct:.0f}% above baseline. "
            f"Each HTTP keep-alive connection, log file, and socket holds an FD. "
            f"Under a request spike the app opens many connections to the LLM API. "
            f"If FDs approach the OS limit (ulimit -n), new connections fail with EMFILE."
        )
        rec = (
            "1. Check `ulimit -n` and raise if needed (production: 65536+).\n"
            "2. Audit connection pool settings — ensure idle connections are closed.\n"
            "3. Use HTTP/2 multiplexing to the LLM API to reduce per-request sockets.\n"
            "4. Add prometheus alert at 80% of FD limit."
        )

    # ---------- NETWORK TX HIGH ----------
    elif "net_tx" in metric:
        pct = _pct_above(anomaly, "net_tx")
        rca = (
            f"Network egress is {pct:.0f}% above baseline ({ctx['current_net_tx_bps']/1024:.1f} KB/s). "
            f"LLM apps send completions back to clients — larger outputs = more TX. "
            f"A token rate spike directly increases TX volume."
        )
        if any("token" in c["cause"] for c in correlations):
            rca += " The correlated token_rate spike is the confirmed driver."
        rec = (
            "1. Enable gzip/brotli compression on API responses.\n"
            "2. Enforce max_tokens to cap completion length.\n"
            "3. If streaming, ensure chunks flush promptly to avoid burst TX at end.\n"
            "4. Check for clients that poll repeatedly instead of using streaming."
        )

    # ---------- TOKEN SPIKE ----------
    elif "token" in metric:
        z   = anomaly.get("z_score") or anomaly.get("abnormal_metrics",{}).get("token_rates",{}).get("z_score",0)
        pct = _pct_above(anomaly, "token_rates")
        system_effects = [c["effect"] for c in correlations]

        rca = (
            f"Token rate is {pct:.0f}% above baseline (z={z:+.1f}). "
        )
        if top_user and top_rate > 0:
            share = top_rate / ctx["current_request_rate"] * 100 if ctx["current_request_rate"] > 0 else 0
            rca += (
                f"User '{top_user}' generates {share:.0f}% of current traffic. "
                f"Likely causes: very long prompts, many few-shot examples, or a retry loop."
            )
        else:
            rca += "No single dominant user — broad traffic or batch job increase."

        if system_effects:
            rca += (
                f"\nSystem impact confirmed: {', '.join(system_effects)} are also elevated, "
                f"consistent with the token throughput increase."
            )
        rec = (
            f"1. Apply per-user token budget (e.g. 100K tokens/min).\n"
            f"2. Set max_tokens on every request.\n"
            f"3. Cache frequent prompts — avoid re-sending identical large prompts.\n"
            f"4. Review system prompts for bloat.\n"
        )
        if top_user:
            rec += f"5. Investigate requests from '{top_user}' first."

    # ---------- COST SPIKE ----------
    elif "cost" in metric:
        pct = _pct_above(anomaly, "cost_rates")
        expensive = {m: r for m, r in models.items() if any(kw in m.lower() for kw in ["gpt-4","opus","ultra"])}
        rca = f"Cost rate is {pct:.0f}% above baseline. "
        if expensive:
            rca += f"Expensive models active: {list(expensive.keys())}. These cost 20-100x standard models."
        else:
            rca += "No expensive model switch detected — driver is higher volume on existing models."
        rec = (
            "1. Add model-selection guardrail — whitelist models per route.\n"
            "2. Cost circuit-breaker: fall back to cheaper model when $/min > threshold.\n"
            "3. Set hard budget alerts in LLM provider dashboard.\n"
            "4. Log cost per request to identify expensive call sites."
        )

    # ---------- ERROR RATE ----------
    elif "error" in metric:
        pct = _pct_above(anomaly, "error_rates")
        top_errors = ", ".join(f"{code}({r:.2f}/s)" for code,r in list(errors.items())[:3]) or "unknown"
        rca = f"Error rate is {pct:.0f}% above baseline ({error_pct:.1f}% currently). Top errors: {top_errors}. "
        if error_pct > 50:
            rca += "Rates above 50% almost certainly indicate an API provider outage or hard rate-limit (429)."
        elif error_pct > 10:
            rca += "Partial degradation — likely rate limiting or malformed requests from specific clients."
        else:
            rca += "Mild increase — possibly a small number of bad requests."
        if gc_pause > 0.1:
            rca += f" GC pauses of {gc_pause*1000:.0f}ms may be contributing to timeouts."
        rec = (
            "1. Check provider status page if >50%.\n"
            "2. Filter by error code — 429=rate-limit, 503=outage, 400/422=bad request.\n"
            "3. Implement exponential back-off for 429s.\n"
            "4. Add request validation to reject malformed inputs before hitting the LLM.\n"
            "5. Set circuit breaker to stop sending when error rate exceeds threshold."
        )

    # ---------- LATENCY ----------
    elif "latency" in metric:
        pct     = _pct_above(anomaly, "latency_avgs")
        cur_lat = anomaly.get("current_value", ctx["current_avg_latency"])
        causes  = []
        if gc_pause > 0.05:  causes.append(f"GC pauses ({gc_pause*1000:.0f}ms)")
        if queue > 10:       causes.append(f"queue depth ({queue:.0f} items)")
        if cpu_pct > 80:     causes.append(f"high CPU ({cpu_pct:.1f}%)")
        if ctx["current_request_rate"] > 0: causes.append("provider throttling under load")

        cause_str = ", ".join(causes) if causes else "provider-side slowness or large prompt/completion"
        rca = (
            f"Average latency is {pct:.0f}% above baseline ({cur_lat:.2f}s). "
            f"Correlated system signals identify these contributors: {cause_str}."
        )
        rec = (
            "1. Add client-side timeout (30s) to fail fast and release threads.\n"
            "2. Enable response streaming to improve perceived latency.\n"
            "3. Cache frequent identical prompts — avoid round-trip for repeated queries.\n"
            f"4. {'Reduce GC pressure — see GC recommendations above.' if gc_pause > 0.05 else 'GC is not a factor currently.'}\n"
            f"5. {'Drain queue — scale workers.' if queue > 10 else 'Queue is healthy.'}"
        )

    # ---------- ML MULTI-METRIC (no dominant known metric) ----------
    else:
        ab_keys = list(anomaly.get("abnormal_metrics", {}).keys())
        # Separate LLM vs system metrics in the abnormal set
        llm_ab  = [k for k in ab_keys if k in ("token_rates","request_rates","error_rates","cost_rates","latency_avgs")]
        sys_ab  = [k for k in ab_keys if k not in llm_ab]
        rca = (
            f"Isolation Forest detected a correlated multi-metric anomaly "
            f"(score={anomaly.get('anomaly_score','?')}). "
            f"LLM metrics deviating: {llm_ab or 'none'}. "
            f"System metrics deviating: {sys_ab or 'none'}. "
        )
        if correlations:
            rca += (
                "The following causal chains are confirmed by simultaneous deviations: "
                + "; ".join(f"{c['cause']}→{c['effect']}" for c in correlations) + "."
            )
        else:
            rca += "No single causal chain is dominant — investigate all deviating metrics together."
        rec = (
            "1. Open the full metrics dashboard — look for simultaneous movement.\n"
            "2. Check for deployments, config changes, or infra events in last 15 min.\n"
            "3. Review provider changelog / status.\n"
            "4. Correlate with application logs for the same time window.\n"
            "5. Use /status endpoint to compare current vs baseline values."
        )

    return rca + corr_str, rec


def _pct_above(anomaly: dict, ml_key: str) -> float:
    if "current_value" in anomaly and "mean" in anomaly and anomaly["mean"] != 0:
        return (anomaly["current_value"] - anomaly["mean"]) / anomaly["mean"] * 100
    ab = anomaly.get("abnormal_metrics", {})
    if ml_key in ab and ab[ml_key]["baseline"] != 0:
        return (ab[ml_key]["current"] - ab[ml_key]["baseline"]) / ab[ml_key]["baseline"] * 100
    return 0.0

# ============================================================
# MAIN DETECTION CYCLE
# ============================================================

def detection_cycle():
    global _last_metrics
    DETECTOR_RUNS.inc()

    m = collect_metrics()
    _last_metrics = m

    # Update prometheus gauges
    TOKEN_RATE_G.set(m["token_rate"]); REQUEST_RATE_G.set(m["request_rate"])
    ERROR_RATE_G.set(m["error_pct"]);  COST_RATE_G.set(m["cost_rate"])
    CPU_USAGE_G.set(m["cpu_usage"]);   MEM_RSS_G.set(m["mem_rss_mb"])
    MEM_PCT_G.set(m["mem_pct"]);       GC_PAUSE_G.set(m["gc_pause"])
    THREAD_COUNT_G.set(m["thread_count"]); FD_COUNT_G.set(m["fd_count"])
    NET_RX_G.set(m["net_rx"]);         NET_TX_G.set(m["net_tx"])
    DISK_READ_G.set(m["disk_read"]);   DISK_WRITE_G.set(m["disk_write"])
    QUEUE_DEPTH_G.set(m["queue_depth"])

    # Append to history
    for key, hist_key in [
        ("token_rate","token_rates"), ("request_rate","request_rates"),
        ("error_pct","error_rates"),  ("cost_rate","cost_rates"),
        ("latency_avg","latency_avgs"),("cpu_usage","cpu_usage"),
        ("mem_rss_mb","mem_rss_mb"),  ("mem_pct","mem_pct"),
        ("gc_pause","gc_pause"),      ("thread_count","thread_count"),
        ("fd_count","fd_count"),      ("net_rx","net_rx"),
        ("net_tx","net_tx"),          ("disk_read","disk_read"),
        ("disk_write","disk_write"),  ("queue_depth","queue_depth"),
    ]:
        historical_data[hist_key].append(m[key])

    # Statistical checks for ALL metrics
    stat_checks = [
        ("token_rates",   "token_rate_per_min"),
        ("request_rates", "request_rate_per_min"),
        ("error_rates",   "error_rate_percent"),
        ("cost_rates",    "cost_rate_per_min"),
        ("latency_avgs",  "avg_latency_seconds"),
        ("cpu_usage",     "cpu_usage_percent"),
        ("mem_rss_mb",    "memory_rss_mb"),
        ("mem_pct",       "memory_percent"),
        ("gc_pause",      "gc_pause_seconds"),
        ("thread_count",  "thread_count"),
        ("fd_count",      "fd_count"),
        ("net_tx",        "net_tx_bps"),
        ("queue_depth",   "queue_depth"),
    ]

    for hist_key, metric_name in stat_checks:
        anomaly = detect_statistical(historical_data[hist_key], metric_name)
        if not anomaly:
            ANOMALY_SCORE.labels(metric_name=metric_name).set(0)
            continue
        fp = _fingerprint("statistical", metric_name)
        if _is_duplicate(fp):
            continue
        ANOMALIES_DETECTED.labels(anomaly_type="statistical", severity=anomaly["severity"]).inc()
        ANOMALY_SCORE.labels(metric_name=metric_name).set(abs(anomaly["z_score"]))
        alert = build_context(anomaly)
        recent_alerts.append(alert)
        logger.warning(f"⚠️  [{anomaly['severity'].upper()}] {anomaly['message']}")
        logger.info(f"   RCA: {alert['root_cause_hypothesis'][:140]}...")

    for anomaly in detect_ml_anomalies():
        fp = _fingerprint("ml", anomaly.get("primary_metric","unknown"))
        if _is_duplicate(fp):
            continue
        ANOMALIES_DETECTED.labels(anomaly_type="isolation_forest", severity=anomaly["severity"]).inc()
        alert = build_context(anomaly)
        recent_alerts.append(alert)
        logger.warning(f"🤖 [ML/{anomaly['severity'].upper()}] {anomaly['message']}")
        if alert.get("correlation_chain"):
            logger.info(f"   CORRELATION: {alert['correlation_chain'][:140]}")


def detection_loop():
    logger.info(f"Starting detection: interval={CHECK_INTERVAL}s, dedup={DEDUP_WINDOW_SECONDS}s")
    logger.info(f"Prometheus: {PROMETHEUS_URL}")
    logger.info("Collecting initial baseline (30s)...")
    time.sleep(30)
    while True:
        try:
            detection_cycle()
        except Exception as e:
            logger.error(f"Cycle failed: {e}", exc_info=True)
        time.sleep(CHECK_INTERVAL)

# ============================================================
# API ENDPOINTS
# ============================================================

@app.route("/")
def index():
    return jsonify({"service": "AIOps Anomaly Detector v2",
                    "metrics_tracked": len(historical_data),
                    "total_alerts": len(recent_alerts),
                    "endpoints": ["/alerts","/alerts/latest","/status","/metrics","/health"]})

@app.route("/health")
def health():
    return jsonify({"status":"healthy","prometheus_url":PROMETHEUS_URL,
                    "data_points":len(historical_data["token_rates"]),
                    "total_alerts":len(recent_alerts)})

@app.route("/status")
def status():
    return jsonify({"current_metrics":_last_metrics,
                    "historical_lengths":{k:len(v) for k,v in historical_data.items()}})

@app.route("/alerts")
def get_alerts():
    return jsonify({"total":len(recent_alerts),"alerts":list(recent_alerts)})

@app.route("/alerts/latest")
def get_latest_alerts():
    latest = list(recent_alerts)[-10:]
    return jsonify({"total":len(latest),"alerts":latest})

@app.route("/metrics")
def metrics():
    return generate_latest(), 200, {"Content-Type": CONTENT_TYPE_LATEST}

# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    threading.Thread(target=detection_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8000, debug=False)
