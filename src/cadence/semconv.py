"""Semantic conventions for real-time, full-duplex voice agents.

The OpenTelemetry GenAI semantic conventions (still pre-1.0 as of mid-2026)
model generative AI as a *request/response* interaction: a ``chat`` span opens
when you send a prompt and closes when the completion comes back. That shape
holds for the HTTP chat-completions world it was designed around.

It does not hold for real-time voice agents.

Gemini Live, OpenAI Realtime, and their peers speak over a persistent
bidirectional WebSocket. Audio flows in both directions *simultaneously*.
There is no request. There is no response. There is no moment at which one
side is definitively "done" -- the user can and does talk over the model
mid-sentence. Consequently:

* There is no natural span boundary. A naive instrumentor produces one
  enormous span per session, which tells you nothing.
* The latency metric that matters is not end-to-end duration. It is
  **time to first audio** -- how long the human sat in silence before hearing
  a reply. A turn can have excellent total duration and still feel broken.
* The dominant failure mode has no representation at all: **barge-in**, where
  the user interrupts the agent. How often it happens, and *how far into* the
  agent's utterance, is the single strongest signal of conversational quality.
* Cost accounting breaks. Input is not a countable prompt; it is an open
  microphone billed per second, plus optional video frames.

``cadence`` therefore defines a ``voice.*`` namespace that composes with,
rather than replaces, ``gen_ai.*``. Inference spans keep their standard
``gen_ai.*`` attributes; the conversational structure wrapped around them is
described in ``voice.*``.

This module is the normative reference for that namespace. See
``docs/SEMCONV.md`` for the rendered specification and rationale.
"""

from __future__ import annotations

from typing import Final

# --------------------------------------------------------------------------
# Span names
# --------------------------------------------------------------------------
# Named to sit alongside the GenAI convention's fixed span names
# (`chat`, `invoke_agent`, `execute_tool`) rather than collide with them.

SPAN_CONVERSATION: Final = "voice.conversation"
"""Root span. Covers one connected session, from socket open to socket close."""

SPAN_TURN: Final = "voice.turn"
"""One conversational exchange: user speaks, agent responds. Child of conversation."""

SPAN_USER_UTTERANCE: Final = "voice.user_utterance"
"""The span of time the human was speaking. Child of turn."""

SPAN_AGENT_UTTERANCE: Final = "voice.agent_utterance"
"""First audio byte out to playback completion (or interruption). Child of turn."""

# For model inference and tool execution we deliberately reuse the GenAI
# convention's own span names so existing backends and processors light up.
SPAN_CHAT: Final = "chat"
SPAN_EXECUTE_TOOL: Final = "execute_tool"


# --------------------------------------------------------------------------
# Session-scoped attributes
# --------------------------------------------------------------------------

VOICE_SESSION_ID: Final = "voice.session.id"
"""Stable identifier for one connected session. Correlates every turn within it."""

VOICE_SESSION_TURN_COUNT: Final = "voice.session.turn_count"
"""Total turns completed in the session. Set on the conversation span at close."""

VOICE_PROVIDER: Final = "voice.provider"
"""Realtime provider: `gemini_live`, `openai_realtime`, ... Distinct from
`gen_ai.provider.name` because the transport and the model may differ."""

VOICE_TRANSPORT: Final = "voice.transport"
"""Underlying transport, e.g. `websocket`, `webrtc`."""


# --------------------------------------------------------------------------
# Turn-scoped attributes
# --------------------------------------------------------------------------

VOICE_TURN_ID: Final = "voice.turn.id"
VOICE_TURN_INDEX: Final = "voice.turn.index"
"""Zero-based ordinal of this turn within its session."""

VOICE_TURN_TTFA_MS: Final = "voice.turn.time_to_first_audio_ms"
"""**The** voice latency metric. Milliseconds from the last inbound frame of
user audio to the first outbound frame of agent audio -- i.e. the duration of
the silence the human actually experienced.

Measured at the audio boundary rather than at the model boundary on purpose:
it captures VAD dwell, network, model queueing, and time-to-first-token
together, which is what the listener perceives as "did it hear me?"."""

VOICE_TURN_INTERRUPTED: Final = "voice.turn.interrupted"
"""True when the user barged in over the agent's reply during this turn."""

VOICE_TURN_END_REASON: Final = "voice.turn.end_reason"
"""One of `completed`, `interrupted`, `tool_handoff`, `session_closed`, `error`."""

VOICE_TURN_TOOL_CALL_COUNT: Final = "voice.turn.tool_call_count"
"""Tool invocations issued during this turn, including mid-stream ones."""


# --------------------------------------------------------------------------
# Utterance-scoped attributes
# --------------------------------------------------------------------------

VOICE_UTTERANCE_DURATION_MS: Final = "voice.utterance.duration_ms"
VOICE_UTTERANCE_AUDIO_MS: Final = "voice.utterance.audio_ms"
"""Decoded audio actually carried, which may be less than wall-clock duration."""

VOICE_UTTERANCE_ROLE: Final = "voice.utterance.role"
"""`user` or `agent`."""

VOICE_UTTERANCE_TRANSCRIPT: Final = "voice.utterance.transcript"
"""Recorded only when content capture is explicitly enabled, mirroring the
GenAI convention's opt-in stance on prompt and completion content."""

VOICE_UTTERANCE_TRUNCATED: Final = "voice.utterance.truncated"
"""True when playback was cut short -- the agent had more to say."""


# --------------------------------------------------------------------------
# Barge-in
# --------------------------------------------------------------------------
# Modelled as a span event on the agent utterance that got cut off, so the
# interruption is visible at the exact point in the waterfall where it landed.

EVENT_BARGE_IN: Final = "voice.barge_in"

VOICE_BARGE_IN_OFFSET_MS: Final = "voice.barge_in.offset_ms"
"""How far into the agent's utterance the interruption arrived. Near-zero
offsets cluster when the agent is misfiring on VAD noise; large offsets mean
the agent is being too verbose. The distribution is diagnostic; the raw count
is not."""

VOICE_BARGE_IN_SOURCE: Final = "voice.barge_in.source"
"""`client_vad` when detected locally from inbound audio during playback, or
`server_signal` when the provider reported the interruption."""


# --------------------------------------------------------------------------
# Modality and cost
# --------------------------------------------------------------------------
# Realtime input is a continuously open microphone, not a countable prompt,
# so seconds-of-audio is the unit that actually maps to spend.

VOICE_MODALITY_INPUT: Final = "voice.modality.input"
"""Comma-joined active input modalities, e.g. `audio` or `audio,video`."""

VOICE_MODALITY_OUTPUT: Final = "voice.modality.output"

VOICE_AUDIO_INPUT_SECONDS: Final = "voice.audio.input_seconds"
VOICE_AUDIO_OUTPUT_SECONDS: Final = "voice.audio.output_seconds"

VOICE_VIDEO_FRAMES: Final = "voice.video.frames"
"""Video frames forwarded to the model. Easy to overlook and expensive: an
agent streaming 1fps for a ten-minute session ships 600 images."""


# --------------------------------------------------------------------------
# Metric instrument names
# --------------------------------------------------------------------------

METRIC_TTFA: Final = "voice.turn.time_to_first_audio"
METRIC_TURN_DURATION: Final = "voice.turn.duration"
METRIC_TURN_COUNT: Final = "voice.turn.count"
METRIC_BARGE_IN_COUNT: Final = "voice.barge_in.count"
METRIC_BARGE_IN_OFFSET: Final = "voice.barge_in.offset"
METRIC_AUDIO_INPUT_SECONDS: Final = "voice.audio.input.seconds"
METRIC_AUDIO_OUTPUT_SECONDS: Final = "voice.audio.output.seconds"
METRIC_VIDEO_FRAMES: Final = "voice.video.frames"
METRIC_SESSION_COUNT: Final = "voice.session.count"

# Reused verbatim from the GenAI conventions so token spend aggregates
# alongside every other instrumented model call in the backend.
METRIC_TOKEN_USAGE: Final = "gen_ai.client.token.usage"


# --------------------------------------------------------------------------
# GenAI attributes we set on inference spans
# --------------------------------------------------------------------------
# Mirrored here as constants so the package has no hard dependency on the
# semconv package's own release cadence, which is still moving.

GEN_AI_PROVIDER_NAME: Final = "gen_ai.provider.name"
GEN_AI_OPERATION_NAME: Final = "gen_ai.operation.name"
GEN_AI_REQUEST_MODEL: Final = "gen_ai.request.model"
GEN_AI_RESPONSE_MODEL: Final = "gen_ai.response.model"
GEN_AI_RESPONSE_FINISH_REASONS: Final = "gen_ai.response.finish_reasons"
GEN_AI_USAGE_INPUT_TOKENS: Final = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS: Final = "gen_ai.usage.output_tokens"
GEN_AI_TOKEN_TYPE: Final = "gen_ai.token.type"
GEN_AI_TOOL_NAME: Final = "gen_ai.tool.name"
GEN_AI_CONVERSATION_ID: Final = "gen_ai.conversation.id"


# --------------------------------------------------------------------------
# Enumerated values
# --------------------------------------------------------------------------


class EndReason:
    COMPLETED: Final = "completed"
    INTERRUPTED: Final = "interrupted"
    TOOL_HANDOFF: Final = "tool_handoff"
    SESSION_CLOSED: Final = "session_closed"
    ERROR: Final = "error"


class Role:
    USER: Final = "user"
    AGENT: Final = "agent"


class BargeInSource:
    CLIENT_VAD: Final = "client_vad"
    SERVER_SIGNAL: Final = "server_signal"
