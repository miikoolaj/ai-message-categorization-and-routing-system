"""Prometheus metrics, exposed at `/metrics` (scraped by the `observability` Compose overlay).

Label values are kept low-cardinality: departments come from the policy, routes are path templates.
"""

from prometheus_client import Counter, Gauge, Histogram, Info

HTTP_REQUESTS = Counter(
    "ai_router_http_requests_total", "HTTP requests", ["method", "route", "status"]
)
HTTP_DURATION = Histogram(
    "ai_router_http_request_duration_seconds",
    "HTTP request duration",
    ["route"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120),
)
ROUTED = Counter("ai_router_routed_total", "Routed messages", ["routed_by", "department"])
FALLBACKS = Counter("ai_router_fallback_total", "Fallback routings by cause", ["cause"])
AGENT_DURATION = Histogram(
    "ai_router_agent_duration_seconds",
    "Duration of one agent run",
    buckets=(1, 2, 5, 8, 10, 15, 20, 30, 60, 120, 180),
)
QUEUE_WAIT = Histogram(
    "ai_router_queue_wait_seconds",
    "Time spent waiting for a free agent slot",
    buckets=(0.01, 0.1, 0.5, 1, 2, 5, 10, 20, 30, 60),
)
AGENT_RETRIES = Counter("ai_router_agent_retries_total", "Retry prompts sent back to the model")
TOOL_DUPLICATES = Counter(
    "ai_router_tool_duplicates_total", "Duplicate send_email calls blocked by the guard"
)
LLM_TOKENS = Counter("ai_router_llm_tokens_total", "LLM tokens", ["direction"])
LOW_CONFIDENCE = Counter(
    "ai_router_low_confidence_total", "Decisions below the confidence threshold"
)
CONTEXT_NEAR_LIMIT = Counter(
    "ai_router_context_near_limit_total", "Model requests close to the context window"
)
REJECTED = Counter("ai_router_rejected_total", "Requests rejected before routing", ["reason"])
OUTBOX_DELIVERIES = Counter(
    "ai_router_outbox_deliveries_total", "Outbox delivery attempts", ["result"]
)
OUTBOX_PENDING = Gauge("ai_router_outbox_pending", "Messages waiting in the outbox")
TICKETS = Gauge("ai_router_tickets", "Stored tickets by status", ["status"])
FEEDBACK = Counter("ai_router_feedback_total", "Feedback from departments", ["verdict"])
MODEL_REWARMS = Counter(
    "ai_router_model_rewarm_total",
    "Warm-ups repeated because Ollama no longer had the model loaded",
)
BUILD_INFO = Info("ai_router", "Version and routing configuration")
