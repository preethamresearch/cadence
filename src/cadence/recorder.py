"""The duplex turn state machine.

This is the part of cadence that does not exist elsewhere.

A request/response instrumentor knows when a span starts (you called the API)
and when it ends (the reply arrived). On a duplex stream neither moment
exists, so the recorder reconstructs conversational structure from the raw
signal stream:

    realtime.session                        one connected session
      +- realtime.turn                      one exchange
      |    +- realtime.audio.user_utterance   input start -> input end
      |    +- chat                          input end -> generation done  [gen_ai.*]
      |    |    +- execute_tool             tools, including mid-stream
      |    +- realtime.audio.agent_utterance  first output -> playback done
      |         (event) realtime.barge_in   if the user cut in
      +- realtime.turn ...

Load-bearing decisions
----------------------

**Time to first audio is measured at the audio boundary, not the model
boundary.** From the last inbound user frame to the first outbound agent
frame. That interval deliberately includes VAD dwell, network, queueing and
time-to-first-token, because all of it is silence to the person waiting.

**Barge-in ends the current turn and opens a new one.** An interruption is not
an error inside a turn; it is the user taking the floor, which is what a new
turn *is*. Any other model nests incoherently once a conversation gets lively.

**The agent may speak first.** Proactive greetings have no preceding user
input, so TTFA is omitted rather than fabricated from session start. A missing
metric is honest; a wrong one poisons the percentile.

**Overlap is tracked separately from barge-in.** Barge-in is the event of
cutting in; overlap is how long both parties continued at once before the
agent yielded. They diagnose different faults.

Overhead
--------
The hot path is ``handle()``, called once per inbound signal. It performs a
dict lookup and a handful of float comparisons; no allocation beyond the span
attributes themselves, no copying of audio, and no I/O. Exceptions are caught
and swallowed — instrumentation must never take down the agent it is watching.
See ``tests/test_overhead.py`` for the measured cost.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from opentelemetry import trace
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

from . import semconv
from .dialogue import DEFAULT_CLASSIFIER, DialogueClassifier
from .events import EventType, VoiceEvent
from .metrics import base_attributes, voice_metrics
from .tracing import get_tracer

logger = logging.getLogger(__name__)

EventHook = Callable[[str, dict[str, Any]], None]
"""Fired as turns progress so a UI can render the same structure the spans
describe without re-deriving it."""


@dataclass(slots=True)
class ToolRecord:
    span: Span
    name: str
    started: float
    retries: int = 0


@dataclass(slots=True)
class TurnState:
    """Mutable bookkeeping for the turn currently in flight."""

    index: int
    turn_id: str
    span: Span
    started_monotonic: float

    user_utterance_span: Span | None = None
    agent_utterance_span: Span | None = None
    inference_span: Span | None = None

    speech_end_monotonic: float | None = None
    """When the user stopped. Start of the TTFA clock."""

    agent_audio_start_monotonic: float | None = None
    """First byte of agent audio out. End of the TTFA clock, and the origin
    against which barge-in offset is measured."""

    last_agent_chunk_monotonic: float | None = None
    max_stream_gap_ms: float = 0.0

    ttfa_ms: float | None = None
    interrupted: bool = False
    generation_complete: bool = False
    tool_call_count: int = 0
    overlap_ms: float = 0.0
    silence_ms: float = 0.0
    agent_audio_ms: float = 0.0
    user_audio_ms: float = 0.0
    repair: str | None = None
    fallback: str | None = None
    user_transcript: list[str] = field(default_factory=list)
    agent_transcript: list[str] = field(default_factory=list)
    tools: dict[str, ToolRecord] = field(default_factory=dict)
    ended: bool = False


class ConversationRecorder:
    """Consumes normalized :class:`VoiceEvent`s and emits spans and metrics.

    One recorder per session. Not thread-safe by design: a duplex session is
    driven by a single asyncio task, and a lock would buy nothing but
    contention on the hot audio path.
    """

    def __init__(
        self,
        *,
        session_id: str | None = None,
        provider: str = "gemini_live",
        model: str | None = None,
        transport: str = semconv.Transport.WEBSOCKET,
        prompt_version: str | None = None,
        agent_version: str | None = None,
        capture_content: bool = False,
        classifier: DialogueClassifier | None = None,
        on_event: EventHook | None = None,
    ) -> None:
        self.session_id = session_id or uuid.uuid4().hex[:16]
        self.provider = provider
        self.model = model
        self.transport = transport
        self.prompt_version = prompt_version
        self.agent_version = agent_version
        self.capture_content = capture_content
        self.classifier = classifier or DEFAULT_CLASSIFIER
        self.on_event = on_event

        self._tracer = get_tracer()
        self._metrics = voice_metrics()
        self._attrs = base_attributes(
            provider,
            model,
            transport=transport,
            prompt_version=prompt_version,
            agent_version=agent_version,
        )

        self._session_span: Span | None = None
        self._session_ctx = None
        self._session_start: float | None = None
        self._turn: TurnState | None = None
        self._turn_index = 0

        # session-scoped tallies
        self._completed_turns = 0
        self._barge_ins = 0
        self._repairs = 0
        self._fallbacks = 0
        self._handoff = False
        self._silence_ms = 0.0

        # duplex activity state, from which overlap and silence are derived
        self._user_active = False
        self._agent_active = False
        self._overlap_start: float | None = None
        self._quiet_since: float | None = None

        self._closed = False

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._session_span is not None:
            return
        now = time.monotonic()
        self._session_start = now
        self._quiet_since = now

        attributes: dict[str, Any] = {
            semconv.SESSION_ID: self.session_id,
            semconv.PROVIDER: self.provider,
            semconv.TRANSPORT: self.transport,
            semconv.ATTR_SCHEMA_VERSION: semconv.SCHEMA_VERSION,
            semconv.GEN_AI_CONVERSATION_ID: self.session_id,
        }
        if self.model:
            attributes[semconv.GEN_AI_REQUEST_MODEL] = self.model
        if self.prompt_version:
            attributes[semconv.PROMPT_VERSION] = self.prompt_version
        if self.agent_version:
            attributes[semconv.AGENT_VERSION] = self.agent_version

        self._session_span = self._tracer.start_span(
            semconv.SPAN_SESSION, kind=SpanKind.SERVER, attributes=attributes
        )
        self._session_ctx = trace.set_span_in_context(self._session_span)
        self._metrics.session_count.add(1, self._attrs)
        self._emit("session_open", {"session_id": self.session_id})

    def close(
        self,
        error: BaseException | None = None,
        outcome: str | None = None,
    ) -> None:
        """End the session.

        ``outcome`` may be supplied by the application when it knows better;
        otherwise it is inferred. A session that escalated to a human is
        `transferred`; one that produced no completed turns at all is
        `abandoned`; anything else is `contained`.
        """
        if self._closed:
            return
        self._closed = True

        if self._turn is not None and not self._turn.ended:
            self._end_turn(semconv.EndReason.SESSION_CLOSED)

        now = time.monotonic()
        self._accumulate_silence(now)

        if outcome is None:
            if self._handoff:
                outcome = semconv.Outcome.TRANSFERRED
            elif self._completed_turns == 0:
                outcome = semconv.Outcome.ABANDONED
            else:
                outcome = semconv.Outcome.CONTAINED

        duration_ms = (now - (self._session_start or now)) * 1000.0
        silence_ratio = (
            round(self._silence_ms / duration_ms, 4) if duration_ms > 0 else 0.0
        )

        if self._session_span is not None:
            span = self._session_span
            span.set_attribute(semconv.SESSION_TURN_COUNT, self._completed_turns)
            span.set_attribute(semconv.SESSION_DURATION_MS, round(duration_ms, 2))
            span.set_attribute(semconv.SESSION_SILENCE_SECONDS, round(self._silence_ms / 1000, 3))
            span.set_attribute(semconv.SESSION_SILENCE_RATIO, silence_ratio)
            span.set_attribute(semconv.SESSION_OUTCOME, outcome)
            span.set_attribute(
                semconv.SESSION_CONTAINED, outcome == semconv.Outcome.CONTAINED
            )
            if error is not None:
                span.record_exception(error)
                span.set_status(Status(StatusCode.ERROR, str(error)))
            span.end()

        self._metrics.outcome_count.add(1, {**self._attrs, semconv.SESSION_OUTCOME: outcome})
        self._metrics.silence_seconds.add(self._silence_ms / 1000.0, self._attrs)

        self._emit(
            "session_close",
            {
                "session_id": self.session_id,
                "turns": self._completed_turns,
                "barge_ins": self._barge_ins,
                "repairs": self._repairs,
                "fallbacks": self._fallbacks,
                "outcome": outcome,
                "silence_ratio": silence_ratio,
            },
        )

    # ------------------------------------------------------------------
    # Event intake
    # ------------------------------------------------------------------

    def handle(self, event: VoiceEvent) -> None:
        """Feed one normalized event through the state machine."""
        if self._session_span is None:
            self.start()
        try:
            handler = self._HANDLERS.get(event.type)
            if handler is not None:
                handler(self, event)
        except Exception:
            # Instrumentation must never take down the agent it is watching.
            logger.exception("cadence: error handling %s", event.type)

    # -- inbound -------------------------------------------------------

    def _on_user_speech_start(self, event: VoiceEvent) -> None:
        self._accumulate_silence(event.monotonic)
        self._user_active = True
        self._maybe_start_overlap(event.monotonic)

        # The user talking over the agent is a barge-in *and* the start of the
        # next turn. Record the interruption against the utterance it cut off,
        # then hand the floor over.
        if self._turn is not None and self._agent_active:
            self._record_barge_in(self._turn, event)
            self._end_turn(semconv.EndReason.INTERRUPTED)

        if self._turn is None or self._turn.ended:
            self._begin_turn(event)

        turn = self._turn
        assert turn is not None
        if turn.user_utterance_span is None:
            turn.user_utterance_span = self._tracer.start_span(
                semconv.SPAN_USER_UTTERANCE,
                context=trace.set_span_in_context(turn.span),
                attributes={
                    semconv.AUDIO_UTTERANCE_ROLE: semconv.Role.USER,
                    semconv.SESSION_ID: self.session_id,
                    semconv.TURN_ID: turn.turn_id,
                },
            )
        self._emit("user_speech_start", {"turn": turn.index})

    def _on_user_speech_end(self, event: VoiceEvent) -> None:
        self._user_active = False
        self._close_overlap(event.monotonic)
        self._mark_quiet(event.monotonic)

        turn = self._turn
        if turn is None or turn.ended:
            return
        turn.speech_end_monotonic = event.monotonic  # TTFA clock starts

        if turn.user_utterance_span is not None:
            span = turn.user_utterance_span
            span.set_attribute(
                semconv.AUDIO_UTTERANCE_DURATION_MS,
                round((event.monotonic - turn.started_monotonic) * 1000, 2),
            )
            span.set_attribute(semconv.AUDIO_UTTERANCE_AUDIO_MS, round(turn.user_audio_ms, 2))
            if self.capture_content and turn.user_transcript:
                span.set_attribute(
                    semconv.AUDIO_UTTERANCE_TRANSCRIPT, " ".join(turn.user_transcript)
                )
            span.end()
            turn.user_utterance_span = None

        # The only part of a realtime turn that maps cleanly onto the existing
        # GenAI convention gets a standard `chat` span.
        attributes = {
            semconv.GEN_AI_OPERATION_NAME: "chat",
            semconv.GEN_AI_PROVIDER_NAME: self.provider,
            semconv.GEN_AI_CONVERSATION_ID: self.session_id,
            semconv.TURN_ID: turn.turn_id,
        }
        if self.model:
            attributes[semconv.GEN_AI_REQUEST_MODEL] = self.model
        turn.inference_span = self._tracer.start_span(
            semconv.SPAN_CHAT,
            kind=SpanKind.CLIENT,
            context=trace.set_span_in_context(turn.span),
            attributes=attributes,
        )
        self._emit("user_speech_end", {"turn": turn.index})

    def _on_user_audio_sent(self, event: VoiceEvent) -> None:
        seconds = (event.audio_ms or 0.0) / 1000.0
        if seconds <= 0:
            return
        if self._turn is not None and not self._turn.ended:
            self._turn.user_audio_ms += event.audio_ms or 0.0
        self._metrics.audio_input_seconds.add(seconds, self._attrs)

    def _on_video_frame_sent(self, event: VoiceEvent) -> None:
        attrs = self._attrs
        if event.payload.get("source"):
            attrs = {**attrs, semconv.VISION_SOURCE: event.payload["source"]}
        self._metrics.vision_frames.add(1, attrs)

    def _on_user_transcript(self, event: VoiceEvent) -> None:
        turn = self._turn
        if turn is not None and event.text:
            turn.user_transcript.append(event.text)

            # Repair detection runs here, in-process. Only the resulting
            # classification is exported -- the transcript text itself never
            # leaves unless content capture is explicitly enabled.
            if turn.repair is None:
                repair = self.classifier.classify_user(event.text)
                if repair:
                    self._record_repair(turn, repair)

        self._emit("user_transcript", {"text": event.text, "final": event.payload.get("final")})

    # -- outbound ------------------------------------------------------

    def _on_agent_audio_chunk(self, event: VoiceEvent) -> None:
        # An agent that speaks unprompted still owns a turn.
        if self._turn is None or self._turn.ended:
            self._begin_turn(event, agent_initiated=True)
        turn = self._turn
        assert turn is not None

        self._accumulate_silence(event.monotonic)
        was_active = self._agent_active
        self._agent_active = True
        if not was_active:
            self._maybe_start_overlap(event.monotonic)

        if turn.agent_audio_start_monotonic is None:
            turn.agent_audio_start_monotonic = event.monotonic
            turn.agent_utterance_span = self._tracer.start_span(
                semconv.SPAN_AGENT_UTTERANCE,
                context=trace.set_span_in_context(turn.span),
                attributes={
                    semconv.AUDIO_UTTERANCE_ROLE: semconv.Role.AGENT,
                    semconv.SESSION_ID: self.session_id,
                    semconv.TURN_ID: turn.turn_id,
                },
            )
            # Close the TTFA clock -- but only when there was user input to
            # measure from. Agent-initiated turns have no meaningful TTFA.
            if turn.speech_end_monotonic is not None:
                ttfa = (event.monotonic - turn.speech_end_monotonic) * 1000.0
                turn.ttfa_ms = round(ttfa, 2)
                turn.span.set_attribute(semconv.TURN_TTFA_MS, turn.ttfa_ms)
                turn.span.set_attribute(semconv.TURN_TTFR_MS, turn.ttfa_ms)
                self._metrics.ttfa.record(ttfa, self._attrs)
                self._metrics.ttfr.record(ttfa, self._attrs)
                self._emit("ttfa", {"turn": turn.index, "ttfa_ms": turn.ttfa_ms})
        else:
            # Stutter detection: a long pause between chunks mid-utterance is
            # a different fault from a slow start, and sounds worse.
            if turn.last_agent_chunk_monotonic is not None:
                gap_ms = (event.monotonic - turn.last_agent_chunk_monotonic) * 1000.0
                # Anything under a chunk's own duration is just pacing.
                if gap_ms > max(120.0, (event.audio_ms or 0.0) * 1.5):
                    turn.max_stream_gap_ms = max(turn.max_stream_gap_ms, gap_ms)
                    self._metrics.stream_gap.record(gap_ms, self._attrs)

        turn.last_agent_chunk_monotonic = event.monotonic
        turn.agent_audio_ms += event.audio_ms or 0.0
        if event.audio_ms:
            self._metrics.audio_output_seconds.add(event.audio_ms / 1000.0, self._attrs)

    def _on_agent_transcript(self, event: VoiceEvent) -> None:
        turn = self._turn
        if turn is not None and event.text:
            turn.agent_transcript.append(event.text)
            if turn.fallback is None:
                fallback = self.classifier.classify_agent(event.text)
                if fallback:
                    self._record_fallback(turn, fallback)
        self._emit("agent_transcript", {"text": event.text})

    def _on_generation_complete(self, event: VoiceEvent) -> None:
        turn = self._turn
        if turn is None:
            return
        turn.generation_complete = True
        if turn.inference_span is not None:
            turn.inference_span.set_attribute(
                semconv.GEN_AI_RESPONSE_FINISH_REASONS, ["stop"]
            )
            turn.inference_span.end()
            turn.inference_span = None

    # -- turn control --------------------------------------------------

    def _on_interrupted(self, event: VoiceEvent) -> None:
        """Provider-reported interruption.

        Providers report this after their own detection decides the user has
        taken the floor. We may already have recorded the barge-in locally, in
        which case this is a confirmation and must not double count -- or every
        barge-in dashboard reads double.
        """
        turn = self._turn
        if turn is None or turn.ended:
            return
        if not turn.interrupted:
            self._record_barge_in(turn, event, source=semconv.BargeInSource.SERVER_SIGNAL)
        self._end_turn(semconv.EndReason.INTERRUPTED)

    def _on_turn_complete(self, event: VoiceEvent) -> None:
        if self._turn is None or self._turn.ended:
            return
        self._end_turn(event.reason or semconv.EndReason.COMPLETED)

    def _on_playback_finished(self, event: VoiceEvent) -> None:
        """Agent audio finished playing. Ends overlap and starts dead air."""
        self._agent_active = False
        self._close_overlap(event.monotonic)
        self._mark_quiet(event.monotonic)

    # -- tools -----------------------------------------------------------

    def _on_tool_call(self, event: VoiceEvent) -> None:
        turn = self._turn
        if turn is None or turn.ended:
            return
        turn.tool_call_count += 1
        name = event.name or "unknown"
        key = event.call_id or f"{name}:{turn.tool_call_count}"

        # A repeat call to the same tool inside one turn is a retry.
        existing = next((r for r in turn.tools.values() if r.name == name), None)
        retries = existing.retries + 1 if existing else 0
        if retries:
            self._metrics.tool_retries.add(1, {**self._attrs, semconv.TOOL_NAME: name})

        parent = turn.inference_span or turn.span
        span = self._tracer.start_span(
            semconv.SPAN_EXECUTE_TOOL,
            kind=SpanKind.INTERNAL,
            context=trace.set_span_in_context(parent),
            attributes={
                semconv.GEN_AI_OPERATION_NAME: "execute_tool",
                semconv.GEN_AI_TOOL_NAME: name,
                semconv.TOOL_NAME: name,
                semconv.TURN_ID: turn.turn_id,
                semconv.SESSION_ID: self.session_id,
                semconv.TOOL_MID_STREAM: self._agent_active,
                semconv.TOOL_RETRY_COUNT: retries,
            },
        )
        turn.tools[key] = ToolRecord(span=span, name=name, started=event.monotonic, retries=retries)
        self._emit("tool_call", {"name": name, "turn": turn.index})

    def _on_tool_result(self, event: VoiceEvent) -> None:
        turn = self._turn
        if turn is None:
            return
        key = event.call_id or ""
        record = turn.tools.pop(key, None)
        if record is None and turn.tools:
            # Providers do not always echo the call id; FIFO degrades into
            # slightly fuzzy timing rather than a leaked span.
            key, record = turn.tools.popitem()
        if record is None:
            return

        duration_ms = (event.monotonic - record.started) * 1000.0
        record.span.set_attribute(semconv.TOOL_DURATION_MS, round(duration_ms, 2))
        record.span.set_attribute(semconv.TOOL_INTERRUPTED, False)
        error = event.payload.get("error")
        if error:
            record.span.set_attribute(semconv.TOOL_ERROR, str(error))
            record.span.set_status(Status(StatusCode.ERROR, str(error)))
        record.span.end()

        self._metrics.tool_duration.record(
            duration_ms, {**self._attrs, semconv.TOOL_NAME: record.name}
        )
        self._emit("tool_result", {"name": record.name, "turn": turn.index})

    # -- accounting ------------------------------------------------------

    def _on_usage(self, event: VoiceEvent) -> None:
        """Token spend, split by direction and media modality.

        The split is the point: a realtime session's bill is dominated by audio
        and video, and an aggregate total hides which one is running away.
        """
        turn = self._turn
        target = turn.inference_span if turn and turn.inference_span else self._session_span

        for direction, total_key, details_key in (
            ("input", "prompt_token_count", "prompt_tokens_details"),
            ("output", "response_token_count", "response_tokens_details"),
        ):
            total = event.payload.get(total_key)
            if total and target is not None:
                target.set_attribute(
                    semconv.GEN_AI_USAGE_INPUT_TOKENS
                    if direction == "input"
                    else semconv.GEN_AI_USAGE_OUTPUT_TOKENS,
                    int(total),
                )
            for entry in event.payload.get(details_key) or []:
                modality = (entry.get("modality") or "unspecified").lower()
                count = entry.get("token_count") or 0
                if count:
                    self._metrics.token_usage.add(
                        count,
                        {
                            **self._attrs,
                            semconv.GEN_AI_TOKEN_TYPE: direction,
                            semconv.GEN_AI_TOKEN_MODALITY: modality,
                        },
                    )
        self._emit("usage", dict(event.payload))

    def _on_error(self, event: VoiceEvent) -> None:
        span = (
            self._turn.span
            if self._turn and not self._turn.ended
            else self._session_span
        )
        if span is not None:
            span.set_status(Status(StatusCode.ERROR, event.text or "error"))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _begin_turn(self, event: VoiceEvent, *, agent_initiated: bool = False) -> None:
        index = self._turn_index
        self._turn_index += 1
        turn_id = f"{self.session_id}-{index}"

        attributes: dict[str, Any] = {
            semconv.TURN_ID: turn_id,
            semconv.TURN_INDEX: index,
            semconv.SESSION_ID: self.session_id,
            semconv.PROVIDER: self.provider,
            semconv.TURN_AGENT_INITIATED: agent_initiated,
        }
        if self.model:
            attributes[semconv.GEN_AI_REQUEST_MODEL] = self.model
        if self.prompt_version:
            attributes[semconv.PROMPT_VERSION] = self.prompt_version

        span = self._tracer.start_span(
            semconv.SPAN_TURN,
            kind=SpanKind.SERVER,
            context=self._session_ctx,
            attributes=attributes,
        )
        self._turn = TurnState(
            index=index, turn_id=turn_id, span=span, started_monotonic=event.monotonic
        )
        self._emit(
            "turn_start",
            {"turn": index, "turn_id": turn_id, "agent_initiated": agent_initiated},
        )

    def _end_turn(self, reason: str) -> None:
        turn = self._turn
        if turn is None or turn.ended:
            return
        turn.ended = True
        now = time.monotonic()
        duration_ms = (now - turn.started_monotonic) * 1000.0

        # Close anything still open, innermost first.
        for record in list(turn.tools.values()):
            record.span.set_attribute(semconv.TOOL_INTERRUPTED, True)
            record.span.set_attribute(
                semconv.TOOL_DURATION_MS, round((now - record.started) * 1000, 2)
            )
            record.span.set_status(
                Status(StatusCode.ERROR, "tool did not return before turn end")
            )
            record.span.end()
        turn.tools.clear()

        if turn.user_utterance_span is not None:
            turn.user_utterance_span.end()
            turn.user_utterance_span = None

        cancelled = turn.inference_span is not None and not turn.generation_complete
        if turn.inference_span is not None:
            turn.inference_span.end()
            turn.inference_span = None

        if turn.agent_utterance_span is not None:
            span = turn.agent_utterance_span
            span.set_attribute(semconv.AUDIO_UTTERANCE_AUDIO_MS, round(turn.agent_audio_ms, 2))
            span.set_attribute(
                semconv.AUDIO_UTTERANCE_DURATION_MS,
                round((now - (turn.agent_audio_start_monotonic or now)) * 1000, 2),
            )
            span.set_attribute(semconv.AUDIO_UTTERANCE_TRUNCATED, turn.interrupted)
            if turn.max_stream_gap_ms:
                span.set_attribute(
                    semconv.AUDIO_STREAM_GAP_MS, round(turn.max_stream_gap_ms, 2)
                )
            if self.capture_content and turn.agent_transcript:
                span.set_attribute(
                    semconv.AUDIO_UTTERANCE_TRANSCRIPT, " ".join(turn.agent_transcript)
                )
            span.end()
            turn.agent_utterance_span = None

        span = turn.span
        span.set_attribute(semconv.TURN_END_REASON, reason)
        span.set_attribute(semconv.TURN_INTERRUPTED, turn.interrupted)
        span.set_attribute(semconv.TURN_TOOL_CALL_COUNT, turn.tool_call_count)
        span.set_attribute(semconv.TURN_DURATION_MS, round(duration_ms, 2))
        span.set_attribute(semconv.TURN_OVERLAP_MS, round(turn.overlap_ms, 2))
        span.set_attribute(semconv.GENERATION_CANCELLED, cancelled)
        span.set_attribute(
            semconv.AUDIO_OUTPUT_SECONDS, round(turn.agent_audio_ms / 1000.0, 3)
        )
        span.set_attribute(
            semconv.AUDIO_INPUT_SECONDS, round(turn.user_audio_ms / 1000.0, 3)
        )
        if turn.max_stream_gap_ms:
            span.set_attribute(
                semconv.TURN_MAX_STREAM_GAP_MS, round(turn.max_stream_gap_ms, 2)
            )
        span.end()

        metric_attrs = {**self._attrs, semconv.TURN_END_REASON: reason}
        self._metrics.turn_duration.record(duration_ms, metric_attrs)
        self._metrics.turn_count.add(1, metric_attrs)
        self._completed_turns += 1

        self._emit(
            "turn_end",
            {
                "turn": turn.index,
                "reason": reason,
                "interrupted": turn.interrupted,
                "ttfa_ms": turn.ttfa_ms,
                "duration_ms": round(duration_ms, 2),
                "agent_audio_ms": round(turn.agent_audio_ms, 2),
                "overlap_ms": round(turn.overlap_ms, 2),
                "tool_calls": turn.tool_call_count,
                "repair": turn.repair,
                "fallback": turn.fallback,
                "max_stream_gap_ms": round(turn.max_stream_gap_ms, 2) or None,
            },
        )

    def _record_barge_in(
        self,
        turn: TurnState,
        event: VoiceEvent,
        source: str = semconv.BargeInSource.CLIENT_VAD,
    ) -> None:
        """Attach the interruption to the utterance it cut off.

        Recorded as a span event rather than a separate span so it lands at the
        exact point in the waterfall where the user cut in, which is what makes
        the trace legible at a glance.
        """
        if turn.interrupted:
            return
        turn.interrupted = True
        self._barge_ins += 1

        if event.server_offset_ms is not None:
            offset_ms = event.server_offset_ms
        elif turn.agent_audio_start_monotonic is not None:
            offset_ms = (event.monotonic - turn.agent_audio_start_monotonic) * 1000.0
        else:
            offset_ms = 0.0
        offset_ms = max(0.0, round(offset_ms, 2))

        target = turn.agent_utterance_span or turn.span
        target.add_event(
            semconv.EVENT_BARGE_IN,
            attributes={
                semconv.TURN_BARGE_IN_OFFSET_MS: offset_ms,
                semconv.BARGE_IN_SOURCE: source,
                semconv.TURN_ID: turn.turn_id,
            },
        )
        turn.span.set_attribute(semconv.TURN_BARGE_IN_OFFSET_MS, offset_ms)

        self._metrics.barge_in_count.add(1, {**self._attrs, semconv.BARGE_IN_SOURCE: source})
        self._metrics.barge_in_offset.record(offset_ms, self._attrs)
        self._emit("barge_in", {"turn": turn.index, "offset_ms": offset_ms, "source": source})

    def _record_repair(self, turn: TurnState, repair_type: str) -> None:
        turn.repair = repair_type
        self._repairs += 1
        turn.span.set_attribute(semconv.TURN_REPAIR, True)
        turn.span.add_event(
            semconv.EVENT_REPAIR, attributes={semconv.REPAIR_TYPE: repair_type}
        )
        self._metrics.repair_count.add(1, {**self._attrs, semconv.REPAIR_TYPE: repair_type})
        self._emit("repair", {"turn": turn.index, "type": repair_type})

    def _record_fallback(self, turn: TurnState, reason: str) -> None:
        turn.fallback = reason
        self._fallbacks += 1
        turn.span.set_attribute(semconv.TURN_FALLBACK, True)
        turn.span.add_event(
            semconv.EVENT_FALLBACK, attributes={semconv.FALLBACK_REASON: reason}
        )
        self._metrics.fallback_count.add(1, {**self._attrs, semconv.FALLBACK_REASON: reason})

        if reason == semconv.FallbackReason.HANDOFF:
            self._handoff = True
            turn.span.add_event(
                semconv.EVENT_HANDOFF, attributes={semconv.HANDOFF_REASON: reason}
            )
            self._metrics.handoff_count.add(1, self._attrs)

        self._emit("fallback", {"turn": turn.index, "reason": reason})

    # -- overlap and silence -------------------------------------------

    def _maybe_start_overlap(self, now: float) -> None:
        if self._user_active and self._agent_active and self._overlap_start is None:
            self._overlap_start = now

    def _close_overlap(self, now: float) -> None:
        if self._overlap_start is None:
            return
        overlap_ms = (now - self._overlap_start) * 1000.0
        self._overlap_start = None
        if overlap_ms <= 0:
            return
        if self._turn is not None:
            self._turn.overlap_ms += overlap_ms
        self._metrics.overlap_duration.record(overlap_ms, self._attrs)
        self._emit("overlap", {"duration_ms": round(overlap_ms, 2)})

    def _mark_quiet(self, now: float) -> None:
        """Both parties inactive: dead air starts here."""
        if not self._user_active and not self._agent_active and self._quiet_since is None:
            self._quiet_since = now

    def _accumulate_silence(self, now: float) -> None:
        if self._quiet_since is None:
            return
        self._silence_ms += max(0.0, (now - self._quiet_since) * 1000.0)
        self._quiet_since = None

    def _emit(self, kind: str, data: dict[str, Any]) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(kind, data)
        except Exception:
            logger.debug("cadence: on_event hook raised", exc_info=True)

    # ------------------------------------------------------------------

    @property
    def trace_id(self) -> str | None:
        """Hex trace id of the session, for deep-linking into a backend."""
        if self._session_span is None:
            return None
        ctx = self._session_span.get_span_context()
        return format(ctx.trace_id, "032x") if ctx.trace_id else None

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "turns": self._completed_turns,
            "barge_ins": self._barge_ins,
            "repairs": self._repairs,
            "fallbacks": self._fallbacks,
            "handoff": self._handoff,
            "trace_id": self.trace_id,
        }

    _HANDLERS: dict[EventType, Callable[[ConversationRecorder, VoiceEvent], None]] = {}


ConversationRecorder._HANDLERS = {
    EventType.USER_SPEECH_START: ConversationRecorder._on_user_speech_start,
    EventType.USER_SPEECH_END: ConversationRecorder._on_user_speech_end,
    EventType.USER_AUDIO_SENT: ConversationRecorder._on_user_audio_sent,
    EventType.USER_TRANSCRIPT: ConversationRecorder._on_user_transcript,
    EventType.VIDEO_FRAME_SENT: ConversationRecorder._on_video_frame_sent,
    EventType.AGENT_AUDIO_CHUNK: ConversationRecorder._on_agent_audio_chunk,
    EventType.AGENT_TRANSCRIPT: ConversationRecorder._on_agent_transcript,
    EventType.AGENT_GENERATION_COMPLETE: ConversationRecorder._on_generation_complete,
    EventType.PLAYBACK_FINISHED: ConversationRecorder._on_playback_finished,
    EventType.INTERRUPTED: ConversationRecorder._on_interrupted,
    EventType.TURN_COMPLETE: ConversationRecorder._on_turn_complete,
    EventType.TOOL_CALL: ConversationRecorder._on_tool_call,
    EventType.TOOL_RESULT: ConversationRecorder._on_tool_result,
    EventType.USAGE: ConversationRecorder._on_usage,
    EventType.ERROR: ConversationRecorder._on_error,
}
