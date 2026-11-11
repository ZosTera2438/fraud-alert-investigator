"""OpenTelemetry setup (Strands' built-in tracing exported to Arize Phoenix) and cost accounting.

Phoenix accepts OTLP over HTTP at http://localhost:6006/v1/traces. Start it with
`docker compose up phoenix` or `uvx arize-phoenix serve`, then set
`observability.enabled: true` (or FRAUD_TRACING=1).

Personal data is masked before it reaches a model (RedactionHook) and the
alert is masked at intake, so spans never carry raw card numbers or emails.
"""

from __future__ import annotations

import logging

from .settings import Settings

logger = logging.getLogger(__name__)
_configured = False


def setup_tracing(settings: Settings) -> bool:
    global _configured
    if _configured or not settings.observability.enabled:
        return _configured
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from strands.telemetry import StrandsTelemetry

    name = settings.observability.service_name
    # openinference.project.name files the traces under their own Phoenix project instead of "default".
    provider = TracerProvider(resource=Resource.create({"service.name": name, "openinference.project.name": name}))
    telemetry = StrandsTelemetry(tracer_provider=provider)
    telemetry.setup_otlp_exporter(endpoint=settings.observability.otlp_endpoint)
    # StrandsTelemetry only registers a provider globally when it creates one itself. Strands' tracer and the
    # pipeline's own spans both read the global provider, so without this every span went to the no-op provider.
    trace.set_tracer_provider(provider)
    _configured = True
    logger.info("tracing to %s", settings.observability.otlp_endpoint)
    return True


def cost_aud(settings: Settings, model_id: str, input_tokens: int, output_tokens: int) -> float | None:
    """None when no price is on file for the model: unknown, not free."""
    price = settings.pricing.get(model_id)
    if price is None:
        return None
    return round((input_tokens * price.input + output_tokens * price.output) / 1_000_000, 6)
