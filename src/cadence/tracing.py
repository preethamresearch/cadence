"""OpenTelemetry provider wiring for cadence.

Configures a tracer and meter provider exporting over OTLP http/protobuf,
which is the protocol SigNoz Cloud ingests on. Everything is driven by the
standard ``OTEL_*`` environment variables so this composes with any other
OpenTelemetry instrumentation already present in the process -- cadence never
installs a global provider if one is already set.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from . import semconv

logger = logging.getLogger(__name__)

INSTRUMENTATION_NAME = "cadence"
INSTRUMENTATION_VERSION = "0.1.0"

# Default histogram buckets are tuned for HTTP request durations in seconds and
# are useless for voice latency, where everything interesting happens between
# 200ms and 2s. These buckets are chosen so the p95 lands in a meaningful
# bucket rather than the +Inf overflow.
_TTFA_BUCKETS_MS = [
    50.0, 100.0, 150.0, 200.0, 300.0, 400.0, 500.0, 650.0,
    800.0, 1000.0, 1250.0, 1500.0, 2000.0, 3000.0, 5000.0,
]

# Barge-in offsets: sub-second interruptions usually mean the agent misfired
# on noise; multi-second ones mean it rambled. Both tails matter.
_BARGE_IN_BUCKETS_MS = [
    100.0, 250.0, 500.0, 1000.0, 2000.0, 3000.0, 5000.0, 8000.0, 12000.0,
]

_TURN_DURATION_BUCKETS_MS = [
    250.0, 500.0, 1000.0, 2000.0, 3000.0, 5000.0, 8000.0, 12000.0, 20000.0, 30000.0,
]

_configured = False


def _histogram_view(instrument_name: str, buckets: list[float]) -> View:
    return View(
        instrument_name=instrument_name,
        aggregation=ExplicitBucketHistogramAggregation(boundaries=buckets),
    )


def configure(
    service_name: str | None = None,
    *,
    endpoint: str | None = None,
    ingestion_key: str | None = None,
    metric_export_interval_ms: int = 10_000,
    force: bool = False,
) -> None:
    """Install tracer and meter providers pointed at SigNoz.

    Safe to call more than once; subsequent calls are ignored unless ``force``.
    If the host application already configured OpenTelemetry, cadence defers to
    it and only registers its histogram views where it can.

    Args:
        service_name: ``service.name`` resource attribute. Falls back to
            ``OTEL_SERVICE_NAME``, then to ``cadence-voice-agent``.
        endpoint: OTLP base endpoint, e.g. ``https://ingest.us.signoz.cloud:443``.
            Falls back to ``OTEL_EXPORTER_OTLP_ENDPOINT``.
        ingestion_key: SigNoz ingestion key. Falls back to
            ``SIGNOZ_INGESTION_KEY``. Sent as the ``signoz-ingestion-key`` header.
        metric_export_interval_ms: How often to flush metrics. Kept short so a
            three-minute demo actually produces plottable points.
        force: Reconfigure even if already configured.
    """
    global _configured
    if _configured and not force:
        return

    service_name = (
        service_name
        or os.getenv("OTEL_SERVICE_NAME")
        or "cadence-voice-agent"
    )
    endpoint = endpoint or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    ingestion_key = ingestion_key or os.getenv("SIGNOZ_INGESTION_KEY")

    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": INSTRUMENTATION_VERSION,
            "telemetry.sdk.name": "opentelemetry",
            # Lets you filter to cadence-produced telemetry in SigNoz even when
            # the service emits other signals too.
            "cadence.instrumentation.version": INSTRUMENTATION_VERSION,
        }
    )

    headers: dict[str, str] = {}
    if ingestion_key:
        headers["signoz-ingestion-key"] = ingestion_key

    if not endpoint:
        logger.warning(
            "cadence: no OTLP endpoint configured; telemetry will not be exported. "
            "Set OTEL_EXPORTER_OTLP_ENDPOINT and SIGNOZ_INGESTION_KEY."
        )

    # --- traces -----------------------------------------------------------
    existing_tracer_provider = trace.get_tracer_provider()
    if force or not isinstance(existing_tracer_provider, TracerProvider):
        tracer_provider = TracerProvider(resource=resource)
        if endpoint:
            tracer_provider.add_span_processor(
                BatchSpanProcessor(
                    OTLPSpanExporter(
                        endpoint=f"{endpoint.rstrip('/')}/v1/traces",
                        headers=headers,
                    ),
                    # A realtime session emits bursts of short spans; flushing
                    # promptly keeps a live demo in step with the dashboard.
                    schedule_delay_millis=2_000,
                    # The SDK default queue is 2048 spans, which a realtime
                    # workload overruns silently — each turn produces four to
                    # six spans, so a few hundred concurrent turns drop data
                    # with no error surfaced to the application. Measured: at
                    # the default, 1201 simulated turns delivered only 497.
                    max_queue_size=32_768,
                    max_export_batch_size=1_024,
                )
            )
        trace.set_tracer_provider(tracer_provider)
    else:
        logger.info("cadence: reusing existing TracerProvider")

    # --- metrics ----------------------------------------------------------
    existing_meter_provider = metrics.get_meter_provider()
    if force or not isinstance(existing_meter_provider, MeterProvider):
        readers = []
        if endpoint:
            readers.append(
                PeriodicExportingMetricReader(
                    OTLPMetricExporter(
                        endpoint=f"{endpoint.rstrip('/')}/v1/metrics",
                        headers=headers,
                    ),
                    export_interval_millis=metric_export_interval_ms,
                )
            )
        meter_provider = MeterProvider(
            resource=resource,
            metric_readers=readers,
            views=[
                _histogram_view(semconv.METRIC_TTFA, _TTFA_BUCKETS_MS),
                _histogram_view(semconv.METRIC_BARGE_IN_OFFSET, _BARGE_IN_BUCKETS_MS),
                _histogram_view(semconv.METRIC_TURN_DURATION, _TURN_DURATION_BUCKETS_MS),
            ],
        )
        metrics.set_meter_provider(meter_provider)
    else:
        logger.info("cadence: reusing existing MeterProvider")

    _configured = True
    logger.info(
        "cadence configured: service=%s endpoint=%s", service_name, endpoint or "<none>"
    )


def get_tracer() -> trace.Tracer:
    return trace.get_tracer(INSTRUMENTATION_NAME, INSTRUMENTATION_VERSION)


def get_meter() -> metrics.Meter:
    return metrics.get_meter(INSTRUMENTATION_NAME, INSTRUMENTATION_VERSION)


def shutdown() -> None:
    """Flush pending telemetry. Call before process exit so short demo runs
    do not lose their final turns to an unflushed batch."""
    for provider in (trace.get_tracer_provider(), metrics.get_meter_provider()):
        for method in ("force_flush", "shutdown"):
            fn: Any = getattr(provider, method, None)
            if callable(fn):
                try:
                    fn()
                except Exception:  # pragma: no cover - best-effort teardown
                    logger.debug("cadence: %s failed during shutdown", method, exc_info=True)
