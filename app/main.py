import os
import time
import uuid
import json
import random
import logging
from datetime import datetime

from flask import Flask, request, jsonify
from prometheus_client import (
    Counter, Histogram, Gauge,
    generate_latest, CONTENT_TYPE_LATEST
)

# ============================================================
# CONFIGURATION
# ============================================================

MOCK_MODE = os.environ.get("MOCK_MODE", "false").lower() == "true"
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "llama-3.1-8b-instant")

if not MOCK_MODE and GROQ_API_KEY:
    from groq import Groq
    groq_client = Groq(api_key=GROQ_API_KEY)
else:
    MOCK_MODE = True
    groq_client = None

# ============================================================
# FLASK APP
# ============================================================

app = Flask(__name__)

# Configure structured JSON logging to stdout
class JSONFormatter(logging.Formatter):
    def format(self, record):
        if isinstance(record.msg, dict):
            return json.dumps(record.msg)
        return json.dumps({"message": record.getMessage(), "level": record.levelname})

handler = logging.StreamHandler()
handler.setFormatter(JSONFormatter())

# Application logger — this is what FluentBit will collect
app_logger = logging.getLogger("aiops")
app_logger.setLevel(logging.INFO)
app_logger.addHandler(handler)

# Suppress Flask default logs to keep output clean
logging.getLogger("werkzeug").setLevel(logging.WARNING)

# ============================================================
# PROMETHEUS METRICS
# ============================================================

REQUEST_COUNT = Counter(
    'llm_requests_total',
    'Total LLM requests',
    ['model', 'endpoint', 'status']
)

ERROR_COUNT = Counter(
    'llm_errors_total',
    'Total errors',
    ['model', 'error_type']
)

TOKEN_INPUT = Histogram(
    'llm_input_tokens',
    'Input tokens per request',
    ['model'],
    buckets=[10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000]
)

TOKEN_OUTPUT = Histogram(
    'llm_output_tokens',
    'Output tokens per request',
    ['model'],
    buckets=[10, 25, 50, 100, 250, 500, 1000, 2000, 4000]
)

TOKEN_TOTAL = Histogram(
    'llm_total_tokens',
    'Total tokens per request',
    ['model'],
    buckets=[20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 15000]
)

REQUEST_LATENCY = Histogram(
    'llm_request_duration_seconds',
    'Request latency in seconds',
    ['model', 'endpoint'],
    buckets=[0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0]
)

PROMPT_LENGTH = Histogram(
    'llm_prompt_length_chars',
    'Prompt length in characters',
    buckets=[10, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 25000]
)

ESTIMATED_COST = Counter(
    'llm_estimated_cost_dollars_total',
    'Estimated cost in dollars',
    ['model']
)

ACTIVE_REQUESTS = Gauge(
    'llm_active_requests',
    'Currently processing requests'
)

REQUESTS_PER_USER = Counter(
    'llm_requests_per_user_total',
    'Requests per user',
    ['user_id']
)

# ============================================================
# COST CALCULATION
# ============================================================

MODEL_PRICING = {
    "llama-3.1-8b-instant":    {"input": 0.05,  "output": 0.08},
    "llama-3.1-70b-versatile": {"input": 0.59,  "output": 0.79},
    "llama-3.3-70b-versatile": {"input": 0.59,  "output": 0.79},
    "gemma2-9b-it":            {"input": 0.20,  "output": 0.20},
    "mixtral-8x7b-32768":      {"input": 0.24,  "output": 0.24},
    "gpt-4":                   {"input": 30.0,  "output": 60.0},
    "gpt-3.5-turbo":           {"input": 0.50,  "output": 1.50},
}

def calculate_cost(model, input_tokens, output_tokens):
    pricing = MODEL_PRICING.get(model, {"input": 0.10, "output": 0.20})
    cost = (
        input_tokens / 1_000_000 * pricing["input"] +
        output_tokens / 1_000_000 * pricing["output"]
    )
    return round(cost, 8)

# ============================================================
# MOCK LLM (when no API key / for simulation)
# ============================================================

def mock_llm_response(prompt, model):
    """Simulate an LLM response with realistic metrics"""
    # Simulate processing time (varies by prompt length)
    base_latency = random.uniform(0.1, 0.5)
    length_factor = len(prompt) / 1000 * random.uniform(0.1, 0.3)
    time.sleep(base_latency + length_factor)

    # Simulate token counts
    input_tokens = max(5, int(len(prompt.split()) * 1.3))
    output_tokens = random.randint(30, 500)

    # Simulate occasional errors (5% chance)
    if random.random() < 0.05:
        error_types = ["rate_limit_error", "timeout_error", "model_overloaded"]
        raise Exception(random.choice(error_types))

    response_text = f"Mock response to: {prompt[:50]}... [Generated {output_tokens} tokens]"

    return {
        "text": response_text,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens
    }

# ============================================================
# REAL LLM CALL
# ============================================================

def real_llm_response(prompt, model):
    """Call Groq API"""
    response = groq_client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": prompt}
        ],
        max_tokens=1024,
        temperature=0.7
    )

    return {
        "text": response.choices[0].message.content,
        "input_tokens": response.usage.prompt_tokens,
        "output_tokens": response.usage.completion_tokens,
        "total_tokens": response.usage.total_tokens
    }

# ============================================================
# API ENDPOINTS
# ============================================================

@app.route("/chat", methods=["POST"])
def chat():
    return handle_llm_request("chat")

@app.route("/summarize", methods=["POST"])
def summarize():
    return handle_llm_request("summarize")

@app.route("/analyze", methods=["POST"])
def analyze():
    return handle_llm_request("analyze")

def handle_llm_request(endpoint):
    """Core request handler with full logging and metrics"""
    request_id = str(uuid.uuid4())
    data = request.get_json(force=True)

    user_id = data.get("user_id", "anonymous")
    prompt = data.get("message", data.get("prompt", ""))
    model = data.get("model", DEFAULT_MODEL)

    if not prompt:
        return jsonify({"error": "No prompt provided"}), 400

    ACTIVE_REQUESTS.inc()
    start_time = time.time()

    try:
        # Call LLM (real or mock)
        if MOCK_MODE:
            result = mock_llm_response(prompt, model)
        else:
            result = real_llm_response(prompt, model)

        elapsed = time.time() - start_time
        cost = calculate_cost(model, result["input_tokens"], result["output_tokens"])

        # ---- UPDATE PROMETHEUS METRICS ----
        REQUEST_COUNT.labels(model=model, endpoint=endpoint, status="success").inc()
        TOKEN_INPUT.labels(model=model).observe(result["input_tokens"])
        TOKEN_OUTPUT.labels(model=model).observe(result["output_tokens"])
        TOKEN_TOTAL.labels(model=model).observe(result["total_tokens"])
        REQUEST_LATENCY.labels(model=model, endpoint=endpoint).observe(elapsed)
        PROMPT_LENGTH.observe(len(prompt))
        ESTIMATED_COST.labels(model=model).inc(cost)
        REQUESTS_PER_USER.labels(user_id=user_id).inc()

        # ---- STRUCTURED LOG (FluentBit will collect this) ----
        log_entry = {
            "log_type": "llm_request",
            "request_id": request_id,
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "user_id": user_id,
            "model": model,
            "endpoint": endpoint,
            "input_tokens": result["input_tokens"],
            "output_tokens": result["output_tokens"],
            "total_tokens": result["total_tokens"],
            "latency_seconds": round(elapsed, 4),
            "latency_ms": round(elapsed * 1000, 2),
            "estimated_cost_usd": cost,
            "prompt_length_chars": len(prompt),
            "response_length_chars": len(result["text"]),
            "status": "success",
            "error_type": None,
            "hour_of_day": datetime.utcnow().hour,
            "day_of_week": datetime.utcnow().strftime("%A"),
            "mock_mode": MOCK_MODE
        }
        app_logger.info(log_entry)

        ACTIVE_REQUESTS.dec()

        return jsonify({
            "request_id": request_id,
            "response": result["text"],
            "model": model,
            "tokens": result["total_tokens"],
            "latency_ms": round(elapsed * 1000, 2)
        })

    except Exception as e:
        elapsed = time.time() - start_time
        error_type = type(e).__name__

        # ---- ERROR METRICS ----
        REQUEST_COUNT.labels(model=model, endpoint=endpoint, status="error").inc()
        ERROR_COUNT.labels(model=model, error_type=error_type).inc()
        REQUEST_LATENCY.labels(model=model, endpoint=endpoint).observe(elapsed)

        # ---- ERROR LOG ----
        log_entry = {
            "log_type": "llm_request",
            "request_id": request_id,
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "user_id": user_id,
            "model": model,
            "endpoint": endpoint,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "latency_seconds": round(elapsed, 4),
            "latency_ms": round(elapsed * 1000, 2),
            "estimated_cost_usd": 0,
            "prompt_length_chars": len(prompt),
            "response_length_chars": 0,
            "status": "error",
            "error_type": error_type,
            "error_message": str(e),
            "hour_of_day": datetime.utcnow().hour,
            "day_of_week": datetime.utcnow().strftime("%A"),
            "mock_mode": MOCK_MODE
        }
        app_logger.info(log_entry)

        ACTIVE_REQUESTS.dec()

        return jsonify({
            "request_id": request_id,
            "error": str(e),
            "error_type": error_type
        }), 500

# ============================================================
# PROMETHEUS METRICS ENDPOINT
# ============================================================

@app.route("/metrics")
def metrics():
    return generate_latest(), 200, {"Content-Type": CONTENT_TYPE_LATEST}

# ============================================================
# HEALTH ENDPOINTS
# ============================================================

@app.route("/health")
def health():
    return jsonify({"status": "healthy", "mock_mode": MOCK_MODE})

@app.route("/")
def index():
    return jsonify({
        "service": "Context-Aware AIOps - LLM Application",
        "endpoints": ["/chat", "/summarize", "/analyze", "/metrics", "/health"],
        "mock_mode": MOCK_MODE
    })

# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    print(f"Starting LLM Application (mock_mode={MOCK_MODE})")
    app.run(host="0.0.0.0", port=5000, debug=False)
