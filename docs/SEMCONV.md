# The `voice.*` semantic conventions

**Status:** experimental · **Version:** 0.1.0

A proposed extension to the OpenTelemetry GenAI semantic conventions covering
real-time, full-duplex voice agents.

---

## Why the existing conventions do not cover this

The GenAI semantic conventions (pre-1.0 as of mid-2026) define spans named
`chat`, `invoke_agent`, and `execute_tool`, with attributes like
`gen_ai.request.model` and `gen_ai.usage.input_tokens`. The model underneath is
**request/response**: a span opens when you send a prompt and closes when the
completion returns.

Real-time voice agents — Gemini Live, OpenAI Realtime, and the rest — do not
work that way. They hold a persistent bidirectional socket over which audio
flows in both directions at once. Four things break:

**1. There is no span boundary.**
Nothing marks a "request". Audio simply streams. An instrumentor that opens a
span on connect and closes it on disconnect produces one span per session,
which tells you nothing about the twenty exchanges inside it.

**2. Duration is the wrong latency metric.**
What determines whether a voice agent feels alive is not how long the whole
exchange took. It is how long the human sat in silence after they stopped
talking, before they heard anything back. A turn can have an excellent total
duration and still feel broken, because all the delay landed at the front.

**3. The dominant failure mode is unrepresented.**
Users talk over voice agents constantly. That event — barge-in — has no
representation in the GenAI conventions at all, because in a request/response
world it cannot happen. Yet how often it occurs, and *how far into* the agent's
reply, is the single most diagnostic signal of conversational quality.

**4. Cost accounting does not apply.**
Input is not a countable prompt. It is an open microphone billed per second,
plus optional video frames. `gen_ai.usage.input_tokens` is still emitted, but
without a modality split it hides which stream is actually spending the money.

`voice.*` addresses exactly these four gaps. It **composes with** `gen_ai.*`
rather than replacing it: model inference inside a turn is still a `chat` span
carrying standard GenAI attributes.

---

## Span model

```
voice.conversation                    one connected session
├── voice.turn                        one exchange
│   ├── voice.user_utterance          VAD speech-start → speech-end
│   ├── chat                          speech-end → generation complete   [gen_ai.*]
│   │   └── execute_tool              tools, including mid-stream ones   [gen_ai.*]
│   └── voice.agent_utterance         first audio out → playback done
│       └── (event) voice.barge_in    if the user cut in
└── voice.turn …
```

### Three decisions worth stating explicitly

**Time to first audio is measured at the audio boundary, not the model
boundary.** From the last inbound frame of user audio to the first outbound
frame of agent audio. That interval deliberately includes VAD dwell, network
transit, model queueing, and time-to-first-token, because all of them are
silence to the person waiting. Measuring from the model call would produce a
number that looks good while the agent feels unresponsive.

**Barge-in ends the current turn and opens a new one.** An interruption is not
an error inside a turn — it is the user taking the floor, which is what a new
turn *is*. Modelling it as an attribute on a continuing turn produces
incoherent nesting as soon as a conversation gets lively.

**Turns with no preceding user speech carry no TTFA.** Proactive greetings and
tool-triggered utterances have nothing to measure from. The attribute is
omitted rather than filled in from session start; a fabricated value silently
corrupts the p95, and a missing metric is more honest than a wrong one.

---

## Attributes

### Session scope — on `voice.conversation`

| Attribute | Type | Description |
|---|---|---|
| `voice.session.id` | string | Stable id for one connected session. |
| `voice.session.turn_count` | int | Turns completed, set at close. |
| `voice.provider` | string | `gemini_live`, `openai_realtime`, … Distinct from `gen_ai.provider.name`: transport and model can differ. |
| `voice.transport` | string | `websocket`, `webrtc`. |

### Turn scope — on `voice.turn`

| Attribute | Type | Description |
|---|---|---|
| `voice.turn.id` | string | Unique per turn. |
| `voice.turn.index` | int | Zero-based ordinal within the session. |
| `voice.turn.time_to_first_audio_ms` | double | **The** voice latency metric. Omitted for agent-initiated turns. |
| `voice.turn.interrupted` | boolean | User barged in during this turn. |
| `voice.turn.end_reason` | string | `completed` \| `interrupted` \| `tool_handoff` \| `session_closed` \| `error`. |
| `voice.turn.tool_call_count` | int | Tools invoked, including mid-stream. |
| `voice.audio.input_seconds` | double | User audio carried in this turn. |
| `voice.audio.output_seconds` | double | Agent audio carried in this turn. |

### Utterance scope — on `voice.user_utterance` / `voice.agent_utterance`

| Attribute | Type | Description |
|---|---|---|
| `voice.utterance.role` | string | `user` \| `agent`. |
| `voice.utterance.duration_ms` | double | Wall-clock span of the utterance. |
| `voice.utterance.audio_ms` | double | Decoded audio carried, which may be less than wall-clock. |
| `voice.utterance.truncated` | boolean | Playback cut short — the agent had more to say. |
| `voice.utterance.transcript` | string | Opt-in only, mirroring the GenAI conventions' stance on content capture. |

### Barge-in — span **event** `voice.barge_in`

Recorded as an event on the agent utterance it interrupted, so it lands at the
exact point in the waterfall where the user cut in.

| Attribute | Type | Description |
|---|---|---|
| `voice.barge_in.offset_ms` | double | How far into the agent's utterance the interruption arrived. |
| `voice.barge_in.source` | string | `client_vad` (detected locally from inbound audio during playback) or `server_signal` (provider reported it). |

The **distribution** of `offset_ms` is the diagnostic, not the count. Offsets
clustering near zero indicate VAD triggering on background noise; consistently
large offsets indicate replies are too long.

> Providers frequently report an interruption *after* local detection has
> already fired. Implementations must deduplicate, or every barge-in dashboard
> reads double.

### Tools

`execute_tool` spans carry standard `gen_ai.*` attributes plus:

| Attribute | Type | Description |
|---|---|---|
| `voice.tool.mid_stream` | boolean | Tool fired while agent audio was still playing — a realtime-only phenomenon and a common source of confusing latency. |

---

## Metrics

| Instrument | Type | Unit | Notes |
|---|---|---|---|
| `voice.turn.time_to_first_audio` | histogram | ms | Buckets tuned 50ms–5s, where voice latency actually lives. |
| `voice.turn.duration` | histogram | ms | |
| `voice.turn.count` | counter | `{turn}` | Attributed by `voice.turn.end_reason`. |
| `voice.barge_in.count` | counter | `{event}` | Attributed by `voice.barge_in.source`. |
| `voice.barge_in.offset` | histogram | ms | |
| `voice.audio.input.seconds` | counter | s | The meter that runs while nobody speaks. |
| `voice.audio.output.seconds` | counter | s | |
| `voice.video.frames` | counter | `{frame}` | 1fps for ten minutes is 600 images. |
| `voice.session.count` | counter | `{session}` | |
| `gen_ai.client.token.usage` | counter | `{token}` | Reused verbatim from the GenAI conventions, with an added `gen_ai.token.modality` dimension. |

**Cardinality note.** `voice.session.id` is deliberately *not* a metric
attribute. Unique ids on metric dimensions are the standard way to melt a
time-series backend; session correlation belongs on spans, where it is free.

---

## Default histogram buckets

OpenTelemetry's default buckets are tuned for HTTP request durations in
seconds and are useless here — nearly every voice measurement lands in the
same bucket. cadence registers explicit views:

- **TTFA:** 50, 100, 150, 200, 300, 400, 500, 650, 800, 1000, 1250, 1500, 2000, 3000, 5000 ms
- **Barge-in offset:** 100, 250, 500, 1000, 2000, 3000, 5000, 8000, 12000 ms
- **Turn duration:** 250, 500, 1000, 2000, 3000, 5000, 8000, 12000, 20000, 30000 ms

---

## Open questions

Genuinely unresolved, and where feedback would be most useful:

1. **Should `voice.turn` be a distinct span name, or `invoke_agent` with a
   `voice.*` attribute set?** The latter reuses existing tooling; the former is
   clearer about what the span represents. cadence chose the former.
2. **Multi-party audio.** These conventions assume one human and one agent.
   Conference-style sessions need a speaker dimension.
3. **Video-first agents.** `voice.video.frames` is a placeholder. A screen-
   sharing agent probably wants resolution and change-rate attributes too.
4. **Is TTFA the right primary metric, or should the standard also define
   time-to-first-*word* from output transcription?** They diverge when
   the model emits filler audio before content.
