"""Metric instruments.

Traces explain one conversation. These explain whether the fleet is healthy,
and they are what the SigNoz dashboard and the conversation SLOs are built on.

Every instrument here is deliberately low-cardinality. Session and turn ids are
*not* metric attributes — unique ids on metric dimensions are the standard way
to melt a time-series backend. Correlation belongs on spans, where it is free.

The dimensions that *are* carried — provider, model, prompt version, transport
— are bounded and are exactly the ones you need to answer "which deploy caused
this?" without a second query.
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

    Created on first use rather than at import time: a library that builds
    instruments at import binds to whatever meter provider happens to exist
    then, which for a library is usually the no-op one.
    """

    # conversation
    ttfr: Histogram
    turn_duration: Histogram
    turn_count: Counter
    barge_in_count: Counter
    barge_in_offset: Histogram
    overlap_duration: Histogram
    repair_count: Counter
    fallback_count: Counter
    handoff_count: Counter
    outcome_count: Counter
    silence_seconds: Counter

    # audio
    ttfa: Histogram
    audio_input_seconds: Counter
    audio_output_seconds: Counter
    stream_gap: Histogram

    # vision
    vision_frames: Counter

    # tool
    tool_duration: Histogram
    tool_retries: Counter

    # realtime
    session_count: Counter
    token_usage: Counter

    @classmethod
    def create(cls) -> VoiceMetrics:
        m = get_meter()
        return cls(
            ttfr=m.create_histogram(
                semconv.METRIC_TTFR, unit="ms",
                description=(
                    "Delay between the user finishing input and the agent's first "
                    "observable output, in any modality."
                ),
            ),
            turn_duration=m.create_histogram(
                semconv.METRIC_TURN_DURATION, unit="ms",
                description="Wall-clock duration of a complete turn.",
            ),
            turn_count=m.create_counter(
                semconv.METRIC_TURN_COUNT, unit="{turn}",
                description="Turns completed, by end reason.",
            ),
            barge_in_count=m.create_counter(
                semconv.METRIC_BARGE_IN_COUNT, unit="{event}",
                description="Times the user interrupted the agent.",
            ),
            barge_in_offset=m.create_histogram(
                semconv.METRIC_BARGE_IN_OFFSET, unit="ms",
                description=(
                    "How far into the agent's output an interruption landed. Low "
                    "values suggest detection misfires; high values suggest verbosity."
                ),
            ),
            overlap_duration=m.create_histogram(
                semconv.METRIC_OVERLAP_DURATION, unit="ms",
                description=(
                    "How long both parties were active at once before the agent "
                    "yielded. Long overlap is experienced as being talked over."
                ),
            ),
            repair_count=m.create_counter(
                semconv.METRIC_REPAIR_COUNT, unit="{event}",
                description="User repairs — the previous turn failed to land.",
            ),
            fallback_count=m.create_counter(
                semconv.METRIC_FALLBACK_COUNT, unit="{event}",
                description="Agent gave up rather than answering.",
            ),
            handoff_count=m.create_counter(
                semconv.METRIC_HANDOFF_COUNT, unit="{event}",
                description="Escalations to a human.",
            ),
            outcome_count=m.create_counter(
                semconv.METRIC_SESSION_OUTCOME, unit="{session}",
                description="Sessions by outcome: contained, transferred, abandoned.",
            ),
            silence_seconds=m.create_counter(
                semconv.METRIC_SILENCE_SECONDS, unit="s",
                description="Seconds in which neither party was active.",
            ),
            ttfa=m.create_histogram(
                semconv.METRIC_TTFA, unit="ms",
                description=(
                    "Silence the user experienced between finishing speaking and "
                    "hearing the first audio of the reply."
                ),
            ),
            audio_input_seconds=m.create_counter(
                semconv.METRIC_AUDIO_INPUT_SECONDS, unit="s",
                description="Seconds of user audio streamed to the model.",
            ),
            audio_output_seconds=m.create_counter(
                semconv.METRIC_AUDIO_OUTPUT_SECONDS, unit="s",
                description="Seconds of agent audio streamed back.",
            ),
            stream_gap=m.create_histogram(
                semconv.METRIC_STREAM_GAP, unit="ms",
                description=(
                    "Pauses between consecutive output chunks mid-utterance — "
                    "stutter, as distinct from a slow start."
                ),
            ),
            vision_frames=m.create_counter(
                semconv.METRIC_VISION_FRAMES, unit="{frame}",
                description="Camera or screen frames forwarded to the model.",
            ),
            tool_duration=m.create_histogram(
                semconv.METRIC_TOOL_DURATION, unit="ms",
                description="Tool execution latency.",
            ),
            tool_retries=m.create_counter(
                semconv.METRIC_TOOL_RETRIES, unit="{retry}",
                description="Tool retries within a turn.",
            ),
            session_count=m.create_counter(
                semconv.METRIC_SESSION_COUNT, unit="{session}",
                description="Realtime sessions opened.",
            ),
            token_usage=m.create_counter(
                semconv.METRIC_TOKEN_USAGE, unit="{token}",
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
    """Drop cached instruments so they rebind to a new meter provider."""
    global _metrics
    _metrics = None


def base_attributes(
    provider: str,
    model: str | None = None,
    *,
    transport: str | None = None,
    prompt_version: str | None = None,
    agent_version: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Dimensions shared by every instrument.

    Prompt and agent version are included on purpose. They are bounded — you
    deploy tens of versions, not millions — and they are what turns "TTFA got
    worse" into "TTFA got worse when prompt v17 shipped".
    """
    attrs: dict[str, Any] = {semconv.PROVIDER: provider}
    if model:
        attrs[semconv.GEN_AI_REQUEST_MODEL] = model
    if transport:
        attrs[semconv.TRANSPORT] = transport
    if prompt_version:
        attrs[semconv.PROMPT_VERSION] = prompt_version
    if agent_version:
        attrs[semconv.AGENT_VERSION] = agent_version
    attrs.update({k: v for k, v in extra.items() if v is not None})
    return attrs
