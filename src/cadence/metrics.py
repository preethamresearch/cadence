"""Metric instruments for real-time voice agents.

Traces tell you what happened in one conversation. These instruments tell you
whether the agent is healthy across all of them -- and they are what the SigNoz
dashboard and the TTFA alert are built on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from opentelemetry.metrics import Counter, Histogram

from . import semconv
from .tracing import get_meter


@dataclass(slots=True)
class VoiceMetrics:
    """Lazily-created instrument bundle.

    Instruments are cheap but not free, and creating them at import time would
    bind to whatever meter provider happened to exist then -- which, for a
    library, is usually the no-op one. Created on first use instead.
    """

    ttfa: Histogram
    turn_duration: Histogram
    turn_count: Counter
    barge_in_count: Counter
    barge_in_offset: Histogram
    audio_input_seconds: Counter
    audio_output_seconds: Counter
    video_frames: Counter
    session_count: Counter
    token_usage: Counter

    @classmethod
    def create(cls) -> VoiceMetrics:
        meter = get_meter()
        return cls(
            ttfa=meter.create_histogram(
                semconv.METRIC_TTFA,
                unit="ms",
                description=(
                    "Silence the user experienced between finishing speaking and "
                    "hearing the first audio of the reply."
                ),
            ),
            turn_duration=meter.create_histogram(
                semconv.METRIC_TURN_DURATION,
                unit="ms",
                description="Wall-clock duration of a complete conversational turn.",
            ),
            turn_count=meter.create_counter(
                semconv.METRIC_TURN_COUNT,
                unit="{turn}",
                description="Conversational turns completed, by end reason.",
            ),
            barge_in_count=meter.create_counter(
                semconv.METRIC_BARGE_IN_COUNT,
                unit="{event}",
                description="Times the user interrupted the agent mid-utterance.",
            ),
            barge_in_offset=meter.create_histogram(
                semconv.METRIC_BARGE_IN_OFFSET,
                unit="ms",
                description=(
                    "How far into the agent's utterance an interruption landed. "
                    "Low values suggest VAD misfires; high values suggest verbosity."
                ),
            ),
            audio_input_seconds=meter.create_counter(
                semconv.METRIC_AUDIO_INPUT_SECONDS,
                unit="s",
                description="Seconds of user audio streamed to the model.",
            ),
            audio_output_seconds=meter.create_counter(
                semconv.METRIC_AUDIO_OUTPUT_SECONDS,
                unit="s",
                description="Seconds of agent audio streamed back.",
            ),
            video_frames=meter.create_counter(
                semconv.METRIC_VIDEO_FRAMES,
                unit="{frame}",
                description="Video frames forwarded to the model.",
            ),
            session_count=meter.create_counter(
                semconv.METRIC_SESSION_COUNT,
                unit="{session}",
                description="Voice sessions opened.",
            ),
            token_usage=meter.create_counter(
                semconv.METRIC_TOKEN_USAGE,
                unit="{token}",
                description=(
                    "Tokens consumed, split by direction and media modality. "
                    "Audio and video dominate spend in realtime sessions."
                ),
            ),
        )


_metrics: VoiceMetrics | None = None


def voice_metrics() -> VoiceMetrics:
    global _metrics
    if _metrics is None:
        _metrics = VoiceMetrics.create()
    return _metrics


def reset() -> None:
    """Drop cached instruments so they rebind to a new meter provider.
    Used by tests and by ``configure(force=True)``."""
    global _metrics
    _metrics = None


def base_attributes(
    session_id: str,
    provider: str,
    model: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Attribute set shared by every voice instrument.

    Deliberately low-cardinality: session id is *not* included, because putting
    a unique id on a metric attribute is the classic way to melt a time-series
    backend. Session correlation belongs on spans, where it is free.
    """
    attrs: dict[str, Any] = {semconv.VOICE_PROVIDER: provider}
    if model:
        attrs[semconv.GEN_AI_REQUEST_MODEL] = model
    attrs.update({k: v for k, v in extra.items() if v is not None})
    return attrs
