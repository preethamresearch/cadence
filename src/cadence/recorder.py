"""The duplex turn state machine.

This is the part of cadence that does not exist elsewhere.

A request/response instrumentor knows when a span starts (you called the API)
and when it ends (the reply arrived). On a duplex audio socket neither of those
moments exists, so the recorder reconstructs conversational structure from the
signal stream:

    voice.conversation                     one connected session
      +- voice.turn                        one exchange
      |    +- voice.user_utterance         VAD start -> VAD end
      |    +- chat                          end of user speech -> generation done
      |    |    +- execute_tool            tools, including mid-stream ones
      |    +- voice.agent_utterance        first audio out -> playback done
      |         (event) voice.barge_in     if the user cut in
      +- voice.turn ...

Three decisions in here are load-bearing:

1. **Time to first audio is measured from end-of-user-speech to first outbound
   audio frame**, not from any model call boundary. That interval is the
   silence the human actually sat through, which is what determines whether
   the agent feels alive. It deliberately includes VAD dwell and network time.

2. **Barge-in closes the current turn and opens a new one.** An interruption is
   not an error inside a turn; it is the user taking the floor, which is the
   definition of a new turn. Modelling it any other way produces turns that
   nest incoherently once a conversation gets lively.

3. **The agent may speak first.** Proactive greetings and tool-triggered
   utterances have no preceding user speech, so TTFA is left unset rather than
   fabricated from the session start. A missing metric is honest; a wrong one
   poisons the p95.
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
from .events import EventType, VoiceEvent
from .metrics import base_attributes, voice_metrics
from .tracing import get_tracer

logger = logging.getLogger(__name__)

EventHook = Callable[[str, dict[str, Any]], None]
"""Optional callback fired as turns progress, so a UI can render the same
structure the spans describe without re-deriving it."""


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
    """When the user stopped talking. Start of the TTFA clock."""

    agent_audio_start_monotonic: float | None = None
    """When the first byte of agent audio went out. End of the TTFA clock, and
    the origin against which barge-in offset is measured."""

    ttfa_ms: float | None = None
    interrupted: bool = False
    tool_call_count: int = 0
    agent_audio_ms: float = 0.0
    user_audio_ms: float = 0.0
    user_transcript: list[str] = field(default_factory=list)
    agent_transcript: list[str] = field(default_factory=list)
    tool_spans: dict[str, Span] = field(default_factory=dict)
    ended: bool = False


class ConversationRecorder:
    """Consumes normalized :class:`VoiceEvent`s and emits spans and metrics.

    One recorder per session. Not thread-safe by design: a duplex session is
    driven by a single asyncio task, and adding a lock would buy nothing but
    contention on the hot audio path.
    """

    def __init__(
        self,
        *,
        session_id: str | None = None,
        provider: str = "gemini_live",
        model: str | None = None,
        transport: str = "websocket",
        capture_content: bool = False,
        input_sample_rate: int = 16_000,
        output_sample_rate: int = 24_000,
        on_event: EventHook | None = None,
    ) -> None:
        self.session_id = session_id or uuid.uuid4().hex[:16]
        self.provider = provider
        self.model = model
        self.transport = transport
        self.capture_content = capture_content
        self.input_sample_rate = input_sample_rate
        self.output_sample_rate = output_sample_rate
        self.on_event = on_event

        self._tracer = get_tracer()
        self._metrics = voice_metrics()
        self._attrs = base_attributes(self.session_id, provider, model)

        self._conversation_span: Span | None = None
        self._conversation_ctx = None
        self._turn: TurnState | None = None
        self._turn_index = 0
        self._completed_turns = 0
        self._barge_ins = 0
        self._closed = False

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._conversation_span is not None:
            return
        self._conversation_span = self._tracer.start_span(
            semconv.SPAN_CONVERSATION,
            kind=SpanKind.SERVER,
            attributes={
                semconv.VOICE_SESSION_ID: self.session_id,
                semconv.VOICE_PROVIDER: self.provider,
                semconv.VOICE_TRANSPORT: self.transport,
                semconv.GEN_AI_CONVERSATION_ID: self.session_id,
                **({semconv.GEN_AI_REQUEST_MODEL: self.model} if self.model else {}),
            },
        )
        self._conversation_ctx = trace.set_span_in_context(self._conversation_span)
        self._metrics.session_count.add(1, self._attrs)
        self._emit("session_open", {"session_id": self.session_id})

    def close(self, error: BaseException | None = None) -> None:
        if self._closed:
            return
        self._closed = True

        if self._turn is not None and not self._turn.ended:
            self._end_turn(semconv.EndReason.SESSION_CLOSED)

        if self._conversation_span is not None:
            self._conversation_span.set_attribute(
                semconv.VOICE_SESSION_TURN_COUNT, self._completed_turns
            )
            if error is not None:
                self._conversation_span.record_exception(error)
                self._conversation_span.set_status(Status(StatusCode.ERROR, str(error)))
            self._conversation_span.end()

        self._emit(
            "session_close",
            {"session_id": self.session_id, "turns": self._completed_turns,
             "barge_ins": self._barge_ins},
        )

    # ------------------------------------------------------------------
    # Event intake
    # ------------------------------------------------------------------

    def handle(self, event: VoiceEvent) -> None:
        """Feed one normalized event through the state machine."""
        if self._conversation_span is None:
            self.start()

        try:
            handler = self._HANDLERS.get(event.type)
            if handler is not None:
                handler(self, event)
        except Exception:
            # Instrumentation must never take down the agent it is watching.
            logger.exception("cadence: error handling %s", event.type)

    # -- inbound: the human --------------------------------------------

    def _on_user_speech_start(self, event: VoiceEvent) -> None:
        # The user talking over the agent is a barge-in *and* the start of the
        # next turn. Record the interruption against the utterance it cut off,
        # then hand the floor over.
        if self._turn is not None and self._is_agent_speaking(self._turn):
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
                    semconv.VOICE_UTTERANCE_ROLE: semconv.Role.USER,
                    semconv.VOICE_SESSION_ID: self.session_id,
                    semconv.VOICE_TURN_ID: turn.turn_id,
                },
            )
        self._emit("user_speech_start", {"turn": turn.index})

    def _on_user_speech_end(self, event: VoiceEvent) -> None:
        turn = self._turn
        if turn is None or turn.ended:
            return
        # Start of the TTFA clock.
        turn.speech_end_monotonic = event.monotonic
        if turn.user_utterance_span is not None:
            span = turn.user_utterance_span
            span.set_attribute(
                semconv.VOICE_UTTERANCE_DURATION_MS,
                round((event.monotonic - turn.started_monotonic) * 1000, 2),
            )
            span.set_attribute(semconv.VOICE_UTTERANCE_AUDIO_MS, round(turn.user_audio_ms, 2))
            if self.capture_content and turn.user_transcript:
                span.set_attribute(
                    semconv.VOICE_UTTERANCE_TRANSCRIPT, " ".join(turn.user_transcript)
                )
            span.end()
            turn.user_utterance_span = None

        # The model is now thinking. This is the only part of a realtime turn
        # that maps cleanly onto the existing GenAI convention, so it gets a
        # standard `chat` span with standard attributes.
        turn.inference_span = self._tracer.start_span(
            semconv.SPAN_CHAT,
            kind=SpanKind.CLIENT,
            context=trace.set_span_in_context(turn.span),
            attributes={
                semconv.GEN_AI_OPERATION_NAME: "chat",
                semconv.GEN_AI_PROVIDER_NAME: self.provider,
                semconv.GEN_AI_CONVERSATION_ID: self.session_id,
                semconv.VOICE_TURN_ID: turn.turn_id,
                **({semconv.GEN_AI_REQUEST_MODEL: self.model} if self.model else {}),
            },
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
        self._metrics.video_frames.add(1, self._attrs)

    def _on_user_transcript(self, event: VoiceEvent) -> None:
        if self._turn is not None and event.text:
            self._turn.user_transcript.append(event.text)
        self._emit("user_transcript", {"text": event.text, "final": event.payload.get("final")})

    # -- outbound: the agent -------------------------------------------

    def _on_agent_audio_chunk(self, event: VoiceEvent) -> None:
        # An agent that speaks unprompted still owns a turn.
        if self._turn is None or self._turn.ended:
            self._begin_turn(event, agent_initiated=True)
        turn = self._turn
        assert turn is not None

        if turn.agent_audio_start_monotonic is None:
            turn.agent_audio_start_monotonic = event.monotonic
            turn.agent_utterance_span = self._tracer.start_span(
                semconv.SPAN_AGENT_UTTERANCE,
                context=trace.set_span_in_context(turn.span),
                attributes={
                    semconv.VOICE_UTTERANCE_ROLE: semconv.Role.AGENT,
                    semconv.VOICE_SESSION_ID: self.session_id,
                    semconv.VOICE_TURN_ID: turn.turn_id,
                },
            )
            # Close the TTFA clock, but only if there was a user utterance to
            # measure from. Agent-initiated turns have no meaningful TTFA.
            if turn.speech_end_monotonic is not None:
                ttfa = (event.monotonic - turn.speech_end_monotonic) * 1000.0
                turn.ttfa_ms = round(ttfa, 2)
                turn.span.set_attribute(semconv.VOICE_TURN_TTFA_MS, turn.ttfa_ms)
                self._metrics.ttfa.record(ttfa, self._attrs)
                self._emit("ttfa", {"turn": turn.index, "ttfa_ms": turn.ttfa_ms})

        turn.agent_audio_ms += event.audio_ms or 0.0
        if event.audio_ms:
            self._metrics.audio_output_seconds.add(event.audio_ms / 1000.0, self._attrs)

    def _on_agent_transcript(self, event: VoiceEvent) -> None:
        if self._turn is not None and event.text:
            self._turn.agent_transcript.append(event.text)
        self._emit("agent_transcript", {"text": event.text})

    def _on_generation_complete(self, event: VoiceEvent) -> None:
        turn = self._turn
        if turn is None or turn.inference_span is None:
            return
        turn.inference_span.set_attribute(
            semconv.GEN_AI_RESPONSE_FINISH_REASONS, ["stop"]
        )
        turn.inference_span.end()
        turn.inference_span = None

    # -- turn control ---------------------------------------------------

    def _on_interrupted(self, event: VoiceEvent) -> None:
        """Provider-reported interruption.

        Gemini reports this after its own VAD decides the user has taken the
        floor. We may already have recorded the barge-in locally from inbound
        audio, in which case this is a confirmation and must not double count.
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
        reason = event.reason or semconv.EndReason.COMPLETED
        self._end_turn(reason)

    # -- tools -----------------------------------------------------------

    def _on_tool_call(self, event: VoiceEvent) -> None:
        turn = self._turn
        if turn is None or turn.ended:
            return
        turn.tool_call_count += 1
        parent = turn.inference_span or turn.span
        span = self._tracer.start_span(
            semconv.SPAN_EXECUTE_TOOL,
            kind=SpanKind.INTERNAL,
            context=trace.set_span_in_context(parent),
            attributes={
                semconv.GEN_AI_OPERATION_NAME: "execute_tool",
                semconv.GEN_AI_TOOL_NAME: event.name or "unknown",
                semconv.VOICE_TURN_ID: turn.turn_id,
                semconv.VOICE_SESSION_ID: self.session_id,
                # Tools firing while audio is still playing are a realtime-only
                # phenomenon and a common source of confusing latency.
                "voice.tool.mid_stream": self._is_agent_speaking(turn),
            },
        )
        key = event.call_id or f"{event.name}:{turn.tool_call_count}"
        turn.tool_spans[key] = span
        self._emit("tool_call", {"name": event.name, "turn": turn.index})

    def _on_tool_result(self, event: VoiceEvent) -> None:
        turn = self._turn
        if turn is None:
            return
        key = event.call_id or ""
        span = turn.tool_spans.pop(key, None)
        if span is None and turn.tool_spans:
            # Providers do not always echo the call id; fall back to FIFO so a
            # missing id degrades into slightly fuzzy timing rather than a leak.
            key, span = turn.tool_spans.popitem()
        if span is None:
            return
        if event.payload.get("error"):
            span.set_status(Status(StatusCode.ERROR, str(event.payload["error"])))
        span.end()
        self._emit("tool_result", {"name": event.name, "turn": turn.index})

    # -- accounting ------------------------------------------------------

    def _on_usage(self, event: VoiceEvent) -> None:
        """Record token spend, split by direction and media modality.

        The modality split is the point. A realtime session's bill is dominated
        by audio and video tokens, and an aggregate total hides which one is
        running away from you.
        """
        turn = self._turn
        target = turn.inference_span if turn and turn.inference_span else self._conversation_span

        for direction, total_key, details_key in (
            ("input", "prompt_token_count", "prompt_tokens_details"),
            ("output", "response_token_count", "response_tokens_details"),
        ):
            total = event.payload.get(total_key)
            if total:
                if target is not None:
                    attr = (
                        semconv.GEN_AI_USAGE_INPUT_TOKENS
                        if direction == "input"
                        else semconv.GEN_AI_USAGE_OUTPUT_TOKENS
                    )
                    target.set_attribute(attr, int(total))

            for entry in event.payload.get(details_key) or []:
                modality = (entry.get("modality") or "unspecified").lower()
                count = entry.get("token_count") or 0
                if count:
                    self._metrics.token_usage.add(
                        count,
                        {
                            **self._attrs,
                            semconv.GEN_AI_TOKEN_TYPE: direction,
                            "gen_ai.token.modality": modality,
                        },
                    )
        self._emit("usage", dict(event.payload))

    def _on_error(self, event: VoiceEvent) -> None:
        span = (self._turn.span if self._turn and not self._turn.ended
                else self._conversation_span)
        if span is not None:
            span.set_status(Status(StatusCode.ERROR, event.text or "error"))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _begin_turn(self, event: VoiceEvent, *, agent_initiated: bool = False) -> None:
        index = self._turn_index
        self._turn_index += 1
        turn_id = f"{self.session_id}-{index}"
        span = self._tracer.start_span(
            semconv.SPAN_TURN,
            kind=SpanKind.SERVER,
            context=self._conversation_ctx,
            attributes={
                semconv.VOICE_TURN_ID: turn_id,
                semconv.VOICE_TURN_INDEX: index,
                semconv.VOICE_SESSION_ID: self.session_id,
                semconv.VOICE_PROVIDER: self.provider,
                "voice.turn.agent_initiated": agent_initiated,
                **({semconv.GEN_AI_REQUEST_MODEL: self.model} if self.model else {}),
            },
        )
        self._turn = TurnState(
            index=index,
            turn_id=turn_id,
            span=span,
            started_monotonic=event.monotonic,
        )
        self._emit("turn_start", {"turn": index, "turn_id": turn_id,
                                  "agent_initiated": agent_initiated})

    def _end_turn(self, reason: str) -> None:
        turn = self._turn
        if turn is None or turn.ended:
            return
        turn.ended = True
        now = time.monotonic()
        duration_ms = (now - turn.started_monotonic) * 1000.0

        # Close anything still open, innermost first.
        for key, span in list(turn.tool_spans.items()):
            span.set_status(Status(StatusCode.ERROR, "tool did not return before turn end"))
            span.end()
        turn.tool_spans.clear()

        if turn.user_utterance_span is not None:
            turn.user_utterance_span.end()
            turn.user_utterance_span = None

        if turn.inference_span is not None:
            turn.inference_span.end()
            turn.inference_span = None

        if turn.agent_utterance_span is not None:
            span = turn.agent_utterance_span
            span.set_attribute(semconv.VOICE_UTTERANCE_AUDIO_MS, round(turn.agent_audio_ms, 2))
            span.set_attribute(
                semconv.VOICE_UTTERANCE_DURATION_MS,
                round((now - (turn.agent_audio_start_monotonic or now)) * 1000, 2),
            )
            span.set_attribute(semconv.VOICE_UTTERANCE_TRUNCATED, turn.interrupted)
            if self.capture_content and turn.agent_transcript:
                span.set_attribute(
                    semconv.VOICE_UTTERANCE_TRANSCRIPT, " ".join(turn.agent_transcript)
                )
            span.end()
            turn.agent_utterance_span = None

        turn.span.set_attribute(semconv.VOICE_TURN_END_REASON, reason)
        turn.span.set_attribute(semconv.VOICE_TURN_INTERRUPTED, turn.interrupted)
        turn.span.set_attribute(semconv.VOICE_TURN_TOOL_CALL_COUNT, turn.tool_call_count)
        turn.span.set_attribute(
            semconv.VOICE_AUDIO_OUTPUT_SECONDS, round(turn.agent_audio_ms / 1000.0, 3)
        )
        turn.span.set_attribute(
            semconv.VOICE_AUDIO_INPUT_SECONDS, round(turn.user_audio_ms / 1000.0, 3)
        )
        turn.span.end()

        metric_attrs = {**self._attrs, semconv.VOICE_TURN_END_REASON: reason}
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
                "tool_calls": turn.tool_call_count,
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
        exact point in the waterfall where the user cut in -- which is what
        makes the trace legible at a glance.
        """
        if turn.interrupted:
            return
        turn.interrupted = True
        self._barge_ins += 1

        # Prefer the provider's own offset; fall back to local timing.
        if event.server_offset_ms is not None:
            offset_ms = event.server_offset_ms
        elif turn.agent_audio_start_monotonic is not None:
            offset_ms = (event.monotonic - turn.agent_audio_start_monotonic) * 1000.0
        else:
            offset_ms = 0.0
        offset_ms = max(0.0, round(offset_ms, 2))

        attributes = {
            semconv.VOICE_BARGE_IN_OFFSET_MS: offset_ms,
            semconv.VOICE_BARGE_IN_SOURCE: source,
            semconv.VOICE_TURN_ID: turn.turn_id,
        }
        target = turn.agent_utterance_span or turn.span
        target.add_event(semconv.EVENT_BARGE_IN, attributes=attributes)
        turn.span.set_attribute(semconv.VOICE_BARGE_IN_OFFSET_MS, offset_ms)

        self._metrics.barge_in_count.add(1, {**self._attrs, semconv.VOICE_BARGE_IN_SOURCE: source})
        self._metrics.barge_in_offset.record(offset_ms, self._attrs)
        self._emit("barge_in", {"turn": turn.index, "offset_ms": offset_ms, "source": source})

    @staticmethod
    def _is_agent_speaking(turn: TurnState) -> bool:
        return turn.agent_utterance_span is not None

    def _emit(self, kind: str, data: dict[str, Any]) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(kind, data)
        except Exception:
            logger.debug("cadence: on_event hook raised", exc_info=True)

    @property
    def trace_id(self) -> str | None:
        """Hex trace id of the conversation, for deep-linking into SigNoz."""
        if self._conversation_span is None:
            return None
        ctx = self._conversation_span.get_span_context()
        return format(ctx.trace_id, "032x") if ctx.trace_id else None

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "turns": self._completed_turns,
            "barge_ins": self._barge_ins,
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
    EventType.INTERRUPTED: ConversationRecorder._on_interrupted,
    EventType.TURN_COMPLETE: ConversationRecorder._on_turn_complete,
    EventType.TOOL_CALL: ConversationRecorder._on_tool_call,
    EventType.TOOL_RESULT: ConversationRecorder._on_tool_result,
    EventType.USAGE: ConversationRecorder._on_usage,
    EventType.ERROR: ConversationRecorder._on_error,
}
