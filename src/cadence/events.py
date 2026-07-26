"""Provider-neutral event vocabulary.

The turn recorder never sees a Gemini type, an OpenAI type, or a raw frame.
Each provider ships a thin adapter that translates its wire messages into the
events below; the recorder holds all of the state-machine logic exactly once.

Adding a provider is therefore an adapter, not a second implementation -- which
is the whole reason the span model is worth generalising in the first place.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class EventType(enum.Enum):
    # Inbound: the human
    USER_SPEECH_START = "user_speech_start"
    USER_SPEECH_END = "user_speech_end"
    USER_AUDIO_SENT = "user_audio_sent"
    USER_TRANSCRIPT = "user_transcript"
    VIDEO_FRAME_SENT = "video_frame_sent"

    # Outbound: the agent
    AGENT_AUDIO_CHUNK = "agent_audio_chunk"
    AGENT_TRANSCRIPT = "agent_transcript"
    AGENT_GENERATION_COMPLETE = "agent_generation_complete"

    # Turn control
    INTERRUPTED = "interrupted"
    TURN_COMPLETE = "turn_complete"

    # Tools
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"

    # Accounting
    USAGE = "usage"

    # Lifecycle
    SESSION_OPEN = "session_open"
    SESSION_CLOSE = "session_close"
    ERROR = "error"


@dataclass(slots=True)
class VoiceEvent:
    """A single normalized occurrence on the duplex stream.

    ``monotonic`` is the timing source of truth -- wall-clock is subject to NTP
    steps, and a negative time-to-first-audio would be worse than no metric.
    """

    type: EventType
    monotonic: float
    wall_ns: int
    audio_ms: float | None = None
    """Decoded audio duration carried by this event, where applicable."""

    server_offset_ms: float | None = None
    """Provider-reported offset into the audio stream, when the provider
    supplies one. Preferred over locally computed offsets because it is
    measured against the model's own view of the audio timeline."""

    text: str | None = None
    name: str | None = None
    call_id: str | None = None
    reason: str | None = None
    source: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
