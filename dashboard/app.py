import os
import requests
from flask import Flask, render_template, jsonify
from datetime import datetime

app = Flask(__name__)

# ── Config ──────────────────────────────────────────────
ANOMALY_DETECTOR_URL = os.environ.get(
    "ANOMALY_DETECTOR_URL",
    "http://localhost:8000"
)
PROMETHEUS_URL = os.environ.get(
    "PROMETHEUS_URL",
    "http://localhost:9090"
)
LLM_APP_URL = os.environ.get(
    "LLM_APP_URL",
    "http://localhost:5000"
)

# ── Helpers ──────────────────────────────────────────────

def get_alerts():
    try:
        r = requests.get(
            f"{ANOMALY_DETECTOR_URL}/alerts",
            timeout=5
        )
        return r.json()
    except Exception as e:
        return {"alerts": [], "total": 0, "error": str(e)}

def get_summary():
    try:
        r = requests.get(
            f"{ANOMALY_DETECTOR_URL}/alerts/summary",
            timeout=5
        )
        return r.json()
    except Exception as e:
        return {"error": str(e)}

def query_prometheus(query):
    try:
        r = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": query},
            timeout=5
        )
        data = r.json()
        if (data["status"] == "success"
                and data["data"]["result"]):
            return float(data["data"]["result"][0]["value"][1])
        return 0.0
    except Exception:
        return 0.0

def query_prometheus_vector(query):
    """Return all label-value pairs (for per-model, per-user)"""
    try:
        r = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": query},
            timeout=5
        )
        data = r.json()
        if data["status"] == "success":
            return data["data"]["result"]
        return []
    except Exception:
        return []

def query_prometheus_range(query, minutes=30):
    """Return time-series data for charts"""
    try:
        import time
        end = time.time()
        start = end - (minutes * 60)
        r = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query_range",
            params={
                "query": query,
                "start": start,
                "end": end,
                "step": "30s"
            },
            timeout=5
        )
        data = r.json()
        if (data["status"] == "success"
                and data["data"]["result"]):
            return data["data"]["result"][0]["values"]
        return []
    except Exception:
        return []

def get_live_metrics():
    """Pull current metric values from Prometheus"""
    token_rate = query_prometheus(
        'sum(rate(llm_total_tokens_sum[2m])) * 60'
    )
    request_rate = query_prometheus(
        'sum(rate(llm_requests_total[2m])) * 60'
    )
    error_rate = query_prometheus(
        'sum(rate(llm_requests_total{status="error"}[2m])) '
        '/ sum(rate(llm_requests_total[2m])) * 100'
    )
    cost_rate = query_prometheus(
        'sum(rate(llm_estimated_cost_dollars_total[2m])) * 60'
    )
    avg_latency = query_prometheus(
        'sum(rate(llm_request_duration_seconds_sum[2m])) '
        '/ sum(rate(llm_request_duration_seconds_count[2m]))'
    )
    total_requests = query_prometheus(
        'sum(llm_requests_total)'
    )
    total_cost = query_prometheus(
        'sum(llm_estimated_cost_dollars_total)'
    )
    active_requests = query_prometheus(
        'sum(llm_active_requests)'
    )
    anomaly_score = query_prometheus(
        'max(aiops_anomaly_score)'
    )

    # Per-model usage
    model_data = query_prometheus_vector(
        'sum by (model) (rate(llm_requests_total[2m])) * 60'
    )
    models = {
        r["metric"].get("model", "unknown"):
        round(float(r["value"][1]), 2)
        for r in model_data
    }

    # Per-user top 5
    user_data = query_prometheus_vector(
        'topk(5, sum by (user_id) '
        '(rate(llm_requests_per_user_total[2m])) * 60)'
    )
    top_users = [
        {
            "user": r["metric"].get("user_id", "unknown"),
            "rate": round(float(r["value"][1]), 2)
        }
        for r in user_data
    ]

    return {
        "token_rate":      round(token_rate, 0),
        "request_rate":    round(request_rate, 2),
        "error_rate":      round(error_rate, 2),
        "cost_rate":       round(cost_rate, 4),
        "avg_latency":     round(avg_latency, 3),
        "total_requests":  int(total_requests),
        "total_cost":      round(total_cost, 4),
        "active_requests": int(active_requests),
        "anomaly_score":   round(anomaly_score, 4),
        "models":          models,
        "top_users":       top_users,
    }

def get_chart_data():
    """Time-series data for trend charts"""
    token_series = query_prometheus_range(
        'sum(rate(llm_total_tokens_sum[2m])) * 60', 30
    )
    request_series = query_prometheus_range(
        'sum(rate(llm_requests_total[2m])) * 60', 30
    )
    cost_series = query_prometheus_range(
        'sum(rate(llm_estimated_cost_dollars_total[2m])) * 60',
        30
    )
    error_series = query_prometheus_range(
        'sum(rate(llm_requests_total{status="error"}[2m])) '
        '/ sum(rate(llm_requests_total[2m])) * 100',
        30
    )
    anomaly_series = query_prometheus_range(
        'max(aiops_anomaly_score)', 30
    )

    def fmt(series):
        return [
            {
                "time": datetime.fromtimestamp(
                    float(p[0])
                ).strftime("%H:%M:%S"),
                "value": round(float(p[1]), 4)
                         if p[1] != "NaN" else 0
            }
            for p in series
        ]

    return {
        "token_rate":    fmt(token_series),
        "request_rate":  fmt(request_series),
        "cost_rate":     fmt(cost_series),
        "error_rate":    fmt(error_series),
        "anomaly_score": fmt(anomaly_series),
    }

def process_alerts(raw_alerts):
    """Format alerts for display"""
    processed = []
    for alert in raw_alerts:
        anomaly   = alert.get("anomaly", {})
        context   = alert.get("context", {})
        severity  = anomaly.get("severity", "warning")
        ts        = alert.get("timestamp", "")

        # Format timestamp nicely
        try:
            dt = datetime.fromisoformat(
                ts.replace("Z", "+00:00")
            )
            time_str = dt.strftime("%H:%M:%S")
        except Exception:
            time_str = ts

        # Pick a display title
        metric = anomaly.get(
            "metric",
            anomaly.get("type", "Unknown")
        )

        # Build abnormal metrics list
        abnormal = anomaly.get("abnormal_metrics", [])
        if not abnormal and anomaly.get("message"):
            abnormal = [anomaly["message"]]

        processed.append({
            "severity":     severity,
            "metric":       metric,
            "message":      anomaly.get("message", ""),
            "z_score":      anomaly.get("z_score"),
            "anomaly_score": anomaly.get("anomaly_score"),
            "type":         anomaly.get("type", ""),
            "root_cause":   alert.get(
                                "root_cause_hypothesis", ""
                            ),
            "recommendation": alert.get("recommendation", ""),
            "timestamp":    time_str,
            "context": {
                "top_users":   context.get("top_users", []),
                "models":      context.get("model_usage", {}),
                "error_rate":  context.get(
                                   "current_error_rate", 0
                               ),
                "latency":     context.get(
                                   "current_avg_latency", 0
                               ),
            },
            "abnormal_metrics": abnormal,
        })

    # Critical first, then by time descending
    processed.sort(
        key=lambda x: (
            0 if x["severity"] == "critical" else 1,
        )
    )
    return processed


# ── Routes ────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/dashboard")
def api_dashboard():
    """Single endpoint — frontend polls this every 10s"""
    alerts_raw = get_alerts()
    summary    = get_summary()
    metrics    = get_live_metrics()
    charts     = get_chart_data()

    all_alerts = alerts_raw.get("alerts", [])
    processed  = process_alerts(all_alerts)

    critical = [a for a in processed
                if a["severity"] == "critical"]
    warnings = [a for a in processed
                if a["severity"] == "warning"]

    # Unique flagged users across all alerts
    flagged_users = set()
    for a in processed:
        for u in a["context"]["top_users"]:
            flagged_users.add(u["user"])

    return jsonify({
        "metrics":       metrics,
        "charts":        charts,
        "alerts": {
            "total":    len(processed),
            "critical": len(critical),
            "warnings": len(warnings),
            "items":    processed[:20],   # latest 20
        },
        "summary":       summary,
        "flagged_users": list(flagged_users),
        "last_updated":  datetime.utcnow().strftime(
                             "%Y-%m-%d %H:%M:%S UTC"
                         ),
    })


@app.route("/api/health")
def api_health():
    checks = {}

    # Check anomaly detector
    try:
        r = requests.get(
            f"{ANOMALY_DETECTOR_URL}/health", timeout=3
        )
        checks["anomaly_detector"] = (
            "up" if r.status_code == 200 else "down"
        )
    except Exception:
        checks["anomaly_detector"] = "down"

    # Check Prometheus
    try:
        r = requests.get(
            f"{PROMETHEUS_URL}/-/healthy", timeout=3
        )
        checks["prometheus"] = (
            "up" if r.status_code == 200 else "down"
        )
    except Exception:
        checks["prometheus"] = "down"

    # Check LLM App
    try:
        r = requests.get(
            f"{LLM_APP_URL}/health", timeout=3
        )
        checks["llm_app"] = (
            "up" if r.status_code == 200 else "down"
        )
    except Exception:
        checks["llm_app"] = "down"

    overall = (
        "healthy"
        if all(v == "up" for v in checks.values())
        else "degraded"
    )

    return jsonify({
        "status":   overall,
        "services": checks,
        "timestamp": datetime.utcnow().isoformat() + "Z"
    })


if __name__ == "__main__":
    print("╔══════════════════════════════════════════╗")
    print("║   AIOps RCA Dashboard                   ║")
    print("║   http://localhost:4000                  ║")
    print("╚══════════════════════════════════════════╝")
    app.run(host="0.0.0.0", port=4000, debug=True)
