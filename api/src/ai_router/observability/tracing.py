"""OpenTelemetry tracing (enabled when OTEL_EXPORTER_OTLP_ENDPOINT is set).

Exports spans over OTLP/HTTP (e.g. to Jaeger in the `observability` overlay): one trace per HTTP
request with child spans for every agent run, model request and tool call (Pydantic AI
instrumentation). Prompt and message content are NOT recorded unless TRACE_INCLUDE_CONTENT=true.
"""

import logging
from collections.abc import Callable

from fastapi import FastAPI

from ai_router import __version__
from ai_router.config import Settings
from ai_router.logging_config import log_event

logger = logging.getLogger(__name__)


def setup_tracing(settings: Settings, app: FastAPI) -> Callable[[], None] | None:
    """Configure tracing; returns a shutdown callback (flushes pending spans) or None."""
    endpoint = settings.otel_exporter_otlp_endpoint
    if not endpoint:
        return None
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from pydantic_ai import Agent
    from pydantic_ai.models.instrumented import InstrumentationSettings

    resource = Resource.create(
        {"service.name": settings.otel_service_name, "service.version": __version__}
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces"))
    )
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(
        app, tracer_provider=provider, excluded_urls="api/v1/health/.*,metrics"
    )
    Agent.instrument_all(
        InstrumentationSettings(
            tracer_provider=provider, include_content=settings.trace_include_content
        )
    )
    log_event(logger, logging.INFO, "tracing.enabled", endpoint=endpoint)
    return provider.shutdown
