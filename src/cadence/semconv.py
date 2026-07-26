"""Semantic conventions for real-time multimodal agents.

**This module is a public API.** Metric and attribute names are the contract
other people's dashboards, alerts, and queries depend on. Renaming one is a
breaking change on the same order as removing a function — see
``SCHEMA_VERSION`` and the stability policy below.

---

The OpenTelemetry GenAI conventions model generative AI as a *request/response*
call: a ``chat`` span opens when you send a prompt and closes when the
completion returns. For chat completions that is right.

Real-time agents do not work that way. Voice agents, screen-sharing agents,
camera agents, and computer-use agents all hold a **persistent bidirectional
stream** where signal flows both directions at once. There is no request, no
response, and no moment where either side is definitively finished — the human
can and does interrupt mid-action. Four consequences:

1. **There is no span boundary.** Nothing marks a request. Instrument it
   naively and you get one span per session covering twenty exchanges.
2. **Duration is the wrong latency metric.** What matters is how long the human
   waited in silence before *anything* came back, not how long the whole
   exchange took.
3. **Interruption has no representation.** The most common failure mode of
   realtime agents cannot occur in a request/response world, so no convention
   describes it.
4. **Cost accounting breaks.** Input is an open microphone or a video stream
   billed continuously, not a countable prompt.

Everything here lives under a single ``realtime.*`` root, with domain
sub-namespaces, mirroring how ``gen_ai.*`` covers all of LLM work in
OpenTelemetry:

===========================  =================================================
``realtime.session.*``       the connected session and its transport
``realtime.turn.*``          turn structure, interruption, repair, outcome
``realtime.audio.*``         speech-specific measurement
``realtime.vision.*``        camera and screen input
``realtime.tool.*``          tool execution, including realtime-only cases
``realtime.browser.*``       browser and computer-use actions
===========================  =================================================

The single root is deliberate. A standard is only useful if people can
remember and cite it, and ``realtime.*`` extends to modalities that do not
exist yet without forcing a rename — which is the failure that kills
conventions.

``gen_ai.*`` is reused verbatim wherever it already fits: model inference
inside a turn is still a ``chat`` span with standard GenAI attributes.

Stability policy
----------------
* ``SCHEMA_VERSION`` follows semver.
* Within a major version, names are **never** renamed or repurposed. New
  attributes may be added; existing ones may be deprecated but keep emitting.
* Anything marked *experimental* below may change in a minor version.
* The version is stamped on every exported resource as
  ``realtime.schema.version`` so a backend can tell which shape it is holding.

Implementation status
---------------------
Voice (Gemini Live) is implemented and tested. ``realtime.vision.*`` is
partially implemented (frame counting). ``realtime.browser.*`` is specified but
has no adapter yet — the constants exist so one has a fixed target rather than
inventing its own names. ``docs/SEMCONV.md`` states what is real today.
"""

from __future__ import annotations

from typing import Final

SCHEMA_VERSION: Final = "0.1.0"
"""Semver for this convention set. Stamped on every exported resource."""

SCHEMA_URL: Final = "https://github.com/preethamresearch/cadence/schemas/0.1.0"

ATTR_SCHEMA_VERSION: Final = "realtime.schema.version"


# ==========================================================================
# Span names
# ==========================================================================

SPAN_SESSION: Final = "realtime.session"
"""Root span. One connected session, stream open to stream close."""

SPAN_TURN: Final = "realtime.turn"
"""One exchange. Child of the session."""

SPAN_USER_UTTERANCE: Final = "realtime.audio.user_utterance"
SPAN_AGENT_UTTERANCE: Final = "realtime.audio.agent_utterance"
SPAN_BROWSER_ACTION: Final = "realtime.browser.action"

# Reused from the GenAI conventions so existing backends light up unchanged.
SPAN_CHAT: Final = "chat"
SPAN_EXECUTE_TOOL: Final = "execute_tool"


# ==========================================================================
# realtime.session.*
# ==========================================================================

SESSION_ID: Final = "realtime.session.id"
SESSION_TURN_COUNT: Final = "realtime.session.turn_count"
SESSION_DURATION_MS: Final = "realtime.session.duration_ms"

PROVIDER: Final = "realtime.provider"
"""`gemini_live`, `openai_realtime`, … Distinct from `gen_ai.provider.name`
because the transport and the model need not come from the same vendor."""

TRANSPORT: Final = "realtime.transport"
"""`websocket`, `webrtc`, `pstn`, `sip`.

Worth carrying as a metric dimension: interruption rates on PSTN differ
sharply from WebRTC, and an aggregate that mixes them hides both."""

MODALITY_INPUT: Final = "realtime.modality.input"
"""Comma-joined active input modalities: `audio`, `audio,video`, `screen`…"""

MODALITY_OUTPUT: Final = "realtime.modality.output"

PROMPT_VERSION: Final = "realtime.prompt.version"
"""Version of the system prompt driving the agent.

The highest-leverage attribute in this file. Realtime quality regressions are
usually caused by prompt changes rather than infrastructure, and without this
you cannot attribute a latency or verbosity shift to the deploy that caused
it. Set it from your build, not by hand."""

AGENT_VERSION: Final = "realtime.agent.version"

SESSION_OUTCOME: Final = "realtime.session.outcome"
"""`contained` | `transferred` | `abandoned`. The outcome the business cares
about; everything else here is a leading indicator of it."""

SESSION_CONTAINED: Final = "realtime.session.contained"
"""True when the session ended without handing off to a human."""


# ==========================================================================
# realtime.turn.*  — modality-agnostic
# ==========================================================================

TURN_ID: Final = "realtime.turn.id"
TURN_INDEX: Final = "realtime.turn.index"
TURN_DURATION_MS: Final = "realtime.turn.duration_ms"
TURN_END_REASON: Final = "realtime.turn.end_reason"
"""`completed` | `interrupted` | `tool_handoff` | `session_closed` | `error`."""

TURN_AGENT_INITIATED: Final = "realtime.turn.agent_initiated"
TURN_TOOL_CALL_COUNT: Final = "realtime.turn.tool_call_count"

TURN_TTFA_MS: Final = "realtime.turn.time_to_first_audio"
"""**The** realtime latency metric, in milliseconds.

Measured from the last inbound frame of user audio to the first outbound frame
of agent audio — deliberately including VAD dwell, network transit, model
queueing and time-to-first-token, because every one of those is silence to the
person waiting. Measuring from the model call instead yields a number that
looks healthy while the agent feels unresponsive.

Omitted entirely on agent-initiated turns: there is no user input to measure
from, and a fabricated value silently corrupts the percentile."""

TURN_TTFR_MS: Final = "realtime.turn.time_to_first_response"
"""The modality-agnostic form of the above: time to the agent's first
observable output of any kind. For a voice agent it equals TTFA; for a
computer-use agent it is time to first visible action. *Experimental.*"""

TURN_INTERRUPTED: Final = "realtime.turn.interrupted"

# -- interruption ----------------------------------------------------------

EVENT_BARGE_IN: Final = "realtime.barge_in"

TURN_BARGE_IN_OFFSET_MS: Final = "realtime.turn.barge_in_offset_ms"
"""How far into the agent's output the interruption arrived.

The **distribution** is the diagnostic, not the count. Offsets clustering near
zero mean detection is firing on noise rather than intent; consistently large
offsets mean the agent is too verbose. Identical counts, opposite fixes."""

BARGE_IN_SOURCE: Final = "realtime.barge_in.source"
"""`client_vad` (detected locally during playback) or `server_signal`."""

# -- overlap ---------------------------------------------------------------
# Distinct from barge-in. Barge-in is the *event* of the user cutting in;
# overlap is *how long both parties continued at once* before the agent
# yielded. Long overlap is experienced as being talked over — a worse fault
# than being slow to start.

TURN_OVERLAP_MS: Final = "realtime.turn.overlap_ms"
OVERLAP_DURATION_MS: Final = "realtime.overlap.duration_ms"

# -- repair and fallback ---------------------------------------------------
# The signals closest to *semantic* quality. Everything else measures timing;
# these measure whether the conversation worked. Detected heuristically from
# transcripts — see `cadence.dialogue` for the method and its stated limits.
#
# Privacy: detection runs in-process on the transcript and exports only the
# resulting classification. The transcript text itself never leaves the
# process unless content capture is explicitly enabled.

EVENT_REPAIR: Final = "realtime.repair"
REPAIR_TYPE: Final = "realtime.repair.type"
TURN_REPAIR: Final = "realtime.turn.repair"
"""True when this turn contains a user repair — meaning the previous turn
failed to land."""

EVENT_FALLBACK: Final = "realtime.fallback"
FALLBACK_REASON: Final = "realtime.fallback.reason"
TURN_FALLBACK: Final = "realtime.turn.fallback"

EVENT_HANDOFF: Final = "realtime.handoff"
HANDOFF_REASON: Final = "realtime.handoff.reason"

# -- silence ---------------------------------------------------------------

TURN_SILENCE_MS: Final = "realtime.turn.silence_ms"
SESSION_SILENCE_SECONDS: Final = "realtime.session.silence_seconds"
SESSION_SILENCE_RATIO: Final = "realtime.session.silence_ratio"
"""Fraction of the session in which neither party was active. A conversation
that is 70% dead air is failing however good its p95 looks."""

GENERATION_CANCELLED: Final = "realtime.turn.generation_cancelled"
"""Model generation abandoned before completing, usually by a barge-in. Tokens
were billed for output nobody received."""


# ==========================================================================
# realtime.audio.*
# ==========================================================================

AUDIO_UTTERANCE_ROLE: Final = "realtime.audio.utterance.role"
AUDIO_UTTERANCE_DURATION_MS: Final = "realtime.audio.utterance.duration_ms"
AUDIO_UTTERANCE_AUDIO_MS: Final = "realtime.audio.utterance.audio_ms"
"""Decoded audio carried, which may be less than wall-clock duration."""

AUDIO_UTTERANCE_TRUNCATED: Final = "realtime.audio.utterance.truncated"
AUDIO_UTTERANCE_TRANSCRIPT: Final = "realtime.audio.utterance.transcript"
"""Opt-in only, mirroring the GenAI conventions' stance on content capture."""

AUDIO_INPUT_SECONDS: Final = "realtime.audio.input_seconds"
AUDIO_OUTPUT_SECONDS: Final = "realtime.audio.output_seconds"
AUDIO_SAMPLE_RATE: Final = "realtime.audio.sample_rate"

AUDIO_STREAM_GAP_MS: Final = "realtime.audio.stream.gap_ms"
"""Longest pause between consecutive agent audio chunks inside one utterance.

Distinct from time-to-first-audio, which measures the delay *before* speech
starts. This catches stutter *during* speech — the model or network stalling
mid-sentence — which listeners find worse than a slow start, because it sounds
like malfunction rather than thought."""

TURN_MAX_STREAM_GAP_MS: Final = "realtime.turn.max_stream_gap_ms"

AUDIO_VOICE_ID: Final = "realtime.audio.voice.id"
AUDIO_VOICE_SWITCHED: Final = "realtime.audio.voice.switched"
"""True when the synthesis voice changed mid-session. Almost always a bug, and
disconcerting to hear."""


# ==========================================================================
# realtime.vision.*
# ==========================================================================

VISION_FRAMES: Final = "realtime.vision.frames"
"""Frames forwarded to the model. Easy to overlook and expensive: an agent
streaming 1fps for ten minutes ships 600 images."""

VISION_FPS: Final = "realtime.vision.fps"
VISION_SOURCE: Final = "realtime.vision.source"
"""`camera` | `screen` | `window`."""
VISION_RESOLUTION: Final = "realtime.vision.resolution"


# ==========================================================================
# realtime.tool.*
# ==========================================================================

TOOL_NAME: Final = "realtime.tool.name"
TOOL_DURATION_MS: Final = "realtime.tool.duration_ms"

TOOL_MID_STREAM: Final = "realtime.tool.mid_stream"
"""Tool fired while the agent was still producing output — a realtime-only
phenomenon and a common source of confusing latency."""

TOOL_INTERRUPTED: Final = "realtime.tool.interrupted"
"""Tool was still running when the turn ended. Its result was discarded and
whatever it cost was wasted. A rising rate means tools are slower than users
are patient."""

TOOL_RETRY_COUNT: Final = "realtime.tool.retry_count"
TOOL_ERROR: Final = "realtime.tool.error"


# ==========================================================================
# realtime.browser.*   (specified; adapter not yet implemented)
# ==========================================================================

BROWSER_ACTION_TYPE: Final = "realtime.browser.action.type"
"""`click` | `type` | `navigate` | `scroll` | `screenshot` | `key`."""

BROWSER_ACTION_DURATION_MS: Final = "realtime.browser.action.duration_ms"
BROWSER_URL: Final = "realtime.browser.url"
BROWSER_TARGET: Final = "realtime.browser.target"
BROWSER_ACTION_FAILED: Final = "realtime.browser.action.failed"


# ==========================================================================
# Metric instruments
# ==========================================================================

METRIC_TTFA: Final = "realtime.turn.time_to_first_audio"
METRIC_TTFR: Final = "realtime.turn.time_to_first_response"
METRIC_TURN_DURATION: Final = "realtime.turn.duration"
METRIC_TURN_COUNT: Final = "realtime.turn.count"
METRIC_BARGE_IN_COUNT: Final = "realtime.barge_in.count"
METRIC_BARGE_IN_OFFSET: Final = "realtime.barge_in.offset"
METRIC_OVERLAP_DURATION: Final = "realtime.overlap.duration"
METRIC_REPAIR_COUNT: Final = "realtime.repair.count"
METRIC_FALLBACK_COUNT: Final = "realtime.fallback.count"
METRIC_HANDOFF_COUNT: Final = "realtime.handoff.count"
METRIC_SILENCE_SECONDS: Final = "realtime.silence.seconds"

METRIC_SESSION_COUNT: Final = "realtime.session.count"
METRIC_SESSION_OUTCOME: Final = "realtime.session.outcome.count"

METRIC_AUDIO_INPUT_SECONDS: Final = "realtime.audio.input.seconds"
METRIC_AUDIO_OUTPUT_SECONDS: Final = "realtime.audio.output.seconds"
METRIC_STREAM_GAP: Final = "realtime.audio.stream.gap"

METRIC_VISION_FRAMES: Final = "realtime.vision.frames"

METRIC_TOOL_DURATION: Final = "realtime.tool.duration"
METRIC_TOOL_RETRIES: Final = "realtime.tool.retries"

# Reused verbatim from the GenAI conventions so token spend aggregates
# alongside every other instrumented model call in the backend.
METRIC_TOKEN_USAGE: Final = "gen_ai.client.token.usage"


# ==========================================================================
# gen_ai.*  — reused as-is
# ==========================================================================

GEN_AI_PROVIDER_NAME: Final = "gen_ai.provider.name"
GEN_AI_OPERATION_NAME: Final = "gen_ai.operation.name"
GEN_AI_REQUEST_MODEL: Final = "gen_ai.request.model"
GEN_AI_RESPONSE_MODEL: Final = "gen_ai.response.model"
GEN_AI_RESPONSE_FINISH_REASONS: Final = "gen_ai.response.finish_reasons"
GEN_AI_USAGE_INPUT_TOKENS: Final = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS: Final = "gen_ai.usage.output_tokens"
GEN_AI_TOKEN_TYPE: Final = "gen_ai.token.type"
GEN_AI_TOKEN_MODALITY: Final = "gen_ai.token.modality"
GEN_AI_TOOL_NAME: Final = "gen_ai.tool.name"
GEN_AI_CONVERSATION_ID: Final = "gen_ai.conversation.id"


# ==========================================================================
# Enumerated values
# ==========================================================================


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


class RepairType:
    REPETITION: Final = "repetition"
    """User restated what they already said."""
    CORRECTION: Final = "correction"
    """User contradicted what the agent did or said."""
    CLARIFICATION_REQUEST: Final = "clarification_request"
    """User asked the agent to repeat or explain itself."""


class FallbackReason:
    NOT_UNDERSTOOD: Final = "not_understood"
    NO_CAPABILITY: Final = "no_capability"
    HANDOFF: Final = "handoff"


class Outcome:
    CONTAINED: Final = "contained"
    TRANSFERRED: Final = "transferred"
    ABANDONED: Final = "abandoned"


class Modality:
    AUDIO: Final = "audio"
    VIDEO: Final = "video"
    SCREEN: Final = "screen"
    TEXT: Final = "text"


class Transport:
    WEBSOCKET: Final = "websocket"
    WEBRTC: Final = "webrtc"
    PSTN: Final = "pstn"
    SIP: Final = "sip"
