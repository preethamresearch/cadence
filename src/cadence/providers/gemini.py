"""Gemini Live adapter.

Translates ``LiveServerMessage`` into cadence's provider-neutral events. All
Gemini-specific knowledge in the package lives in this file; the recorder and
the semantic conventions know nothing about it.

Field access is defensive throughout. The Live API surface is still moving --
``voice_activity_detection_signal`` and ``turn_complete_reason`` are recent
additions -- and instrumentation that raises on an unfamiliar field is worse
than instrumentation that quietly skips it.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterator

from ..events import EventType, VoiceEvent

logger = logging.getLogger(__name__)

PROVIDER = "gemini_live"

# Gemini Live streams 16 kHz mono PCM in and 24 kHz mono PCM out, 16-bit.
INPUT_SAMPLE_RATE = 16_000
OUTPUT_SAMPLE_RATE = 24_000
_BYTES_PER_SAMPLE = 2


def pcm_duration_ms(num_bytes: int, sample_rate: int) -> float:
    """Decoded duration of a raw 16-bit mono PCM buffer."""
    if num_bytes <= 0 or sample_rate <= 0:
        return 0.0
    return (num_bytes / (sample_rate * _BYTES_PER_SAMPLE)) * 1000.0


def parse_duration_ms(value: Any) -> float | None:
    """Parse a protobuf Duration as milliseconds.

    Serialized Durations arrive as strings like ``"1.500s"``; the SDK sometimes
    hands back a plain number instead. Both are accepted, anything else is
    discarded rather than guessed at.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) * 1000.0
    if isinstance(value, str):
        text = value.strip()
        try:
            if text.endswith("ms"):
                return float(text[:-2])
            if text.endswith("s"):
                return float(text[:-1]) * 1000.0
            return float(text) * 1000.0
        except ValueError:
            logger.debug("cadence: unparseable duration %r", value)
    return None


def _enum_value(value: Any) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "value", value))


def _now() -> tuple[float, int]:
    return time.monotonic(), time.time_ns()


def _event(event_type: EventType, **kwargs: Any) -> VoiceEvent:
    monotonic, wall_ns = _now()
    return VoiceEvent(type=event_type, monotonic=monotonic, wall_ns=wall_ns, **kwargs)


def _iter_audio_chunks(model_turn: Any) -> Iterator[bytes]:
    for part in getattr(model_turn, "parts", None) or []:
        inline = getattr(part, "inline_data", None)
        data = getattr(inline, "data", None) if inline is not None else None
        mime = (getattr(inline, "mime_type", "") or "") if inline is not None else ""
        if data and mime.startswith("audio"):
            yield data


def _token_details(details: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for entry in details or []:
        out.append(
            {
                "modality": _enum_value(getattr(entry, "modality", None)),
                "token_count": getattr(entry, "token_count", None),
            }
        )
    return out


def translate(message: Any) -> list[VoiceEvent]:
    """Convert one ``LiveServerMessage`` into zero or more cadence events.

    A single message can carry several meaningful occurrences -- audio plus a
    transcript plus turn completion -- so this returns a list, in the order the
    recorder should observe them.
    """
    events: list[VoiceEvent] = []

    # --- explicit VAD signals (preferred turn boundaries) -----------------
    vad = getattr(message, "voice_activity_detection_signal", None)
    signal = _enum_value(getattr(vad, "vad_signal_type", None)) if vad is not None else None
    if signal == "VAD_SIGNAL_TYPE_SOS":
        events.append(_event(EventType.USER_SPEECH_START, source="server_vad"))
    elif signal == "VAD_SIGNAL_TYPE_EOS":
        events.append(_event(EventType.USER_SPEECH_END, source="server_vad"))

    # --- activity markers, which carry a stream offset --------------------
    activity = getattr(message, "voice_activity", None)
    if activity is not None:
        kind = _enum_value(getattr(activity, "voice_activity_type", None))
        offset_ms = parse_duration_ms(getattr(activity, "audio_offset", None))
        if kind == "ACTIVITY_START":
            events.append(
                _event(
                    EventType.USER_SPEECH_START,
                    server_offset_ms=offset_ms,
                    source="server_activity",
                )
            )
        elif kind == "ACTIVITY_END":
            events.append(
                _event(
                    EventType.USER_SPEECH_END,
                    server_offset_ms=offset_ms,
                    source="server_activity",
                )
            )

    content = getattr(message, "server_content", None)
    if content is not None:
        # Interruption is reported before turn_complete, and the recorder
        # relies on that ordering to attribute the barge-in correctly.
        if getattr(content, "interrupted", None):
            events.append(_event(EventType.INTERRUPTED, source="server_signal"))

        model_turn = getattr(content, "model_turn", None)
        if model_turn is not None:
            for chunk in _iter_audio_chunks(model_turn):
                events.append(
                    _event(
                        EventType.AGENT_AUDIO_CHUNK,
                        audio_ms=pcm_duration_ms(len(chunk), OUTPUT_SAMPLE_RATE),
                    )
                )

        for attr, event_type, final_attr in (
            ("input_transcription", EventType.USER_TRANSCRIPT, True),
            ("interim_input_transcription", EventType.USER_TRANSCRIPT, False),
            ("output_transcription", EventType.AGENT_TRANSCRIPT, True),
        ):
            transcription = getattr(content, attr, None)
            text = getattr(transcription, "text", None) if transcription is not None else None
            if text:
                events.append(
                    _event(
                        event_type,
                        text=text,
                        payload={
                            "final": bool(getattr(transcription, "finished", final_attr))
                        },
                    )
                )

        if getattr(content, "generation_complete", None):
            events.append(_event(EventType.AGENT_GENERATION_COMPLETE))

        if getattr(content, "turn_complete", None):
            reason = _enum_value(getattr(content, "turn_complete_reason", None))
            # The unspecified enum value carries no information; treat a clean
            # completion as exactly that rather than surfacing the placeholder.
            if reason in (None, "TURN_COMPLETE_REASON_UNSPECIFIED"):
                reason = "completed"
            events.append(_event(EventType.TURN_COMPLETE, reason=reason))

    # --- tool calls -------------------------------------------------------
    tool_call = getattr(message, "tool_call", None)
    if tool_call is not None:
        for fc in getattr(tool_call, "function_calls", None) or []:
            events.append(
                _event(
                    EventType.TOOL_CALL,
                    name=getattr(fc, "name", None),
                    call_id=getattr(fc, "id", None),
                    payload={"args": getattr(fc, "args", None)},
                )
            )

    cancellation = getattr(message, "tool_call_cancellation", None)
    if cancellation is not None:
        for call_id in getattr(cancellation, "ids", None) or []:
            events.append(
                _event(
                    EventType.TOOL_RESULT,
                    call_id=call_id,
                    payload={"error": "cancelled"},
                )
            )

    # --- usage ------------------------------------------------------------
    usage = getattr(message, "usage_metadata", None)
    if usage is not None:
        events.append(
            _event(
                EventType.USAGE,
                payload={
                    "prompt_token_count": getattr(usage, "prompt_token_count", None),
                    "response_token_count": getattr(usage, "response_token_count", None),
                    "total_token_count": getattr(usage, "total_token_count", None),
                    "thoughts_token_count": getattr(usage, "thoughts_token_count", None),
                    "prompt_tokens_details": _token_details(
                        getattr(usage, "prompt_tokens_details", None)
                    ),
                    "response_tokens_details": _token_details(
                        getattr(usage, "response_tokens_details", None)
                    ),
                },
            )
        )

    return events


def user_audio_event(num_bytes: int, sample_rate: int = INPUT_SAMPLE_RATE) -> VoiceEvent:
    """Event for a chunk of microphone audio forwarded to the model."""
    return _event(
        EventType.USER_AUDIO_SENT,
        audio_ms=pcm_duration_ms(num_bytes, sample_rate),
    )


def video_frame_event() -> VoiceEvent:
    return _event(EventType.VIDEO_FRAME_SENT)


def tool_result_event(call_id: str | None, name: str | None = None, error: str | None = None):
    return _event(
        EventType.TOOL_RESULT,
        call_id=call_id,
        name=name,
        payload={"error": error} if error else {},
    )
