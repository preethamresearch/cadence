# Your voice agent's worst moment is invisible to your tracing

*Building `cadence`: OpenTelemetry instrumentation for real-time, full-duplex
voice agents — and shipping it to SigNoz.*

---

Here is a bug report you cannot act on:

> "The voice agent feels laggy."

You open your traces. The span says the exchange took 3.2 seconds. Is that bad?
You have no idea. The user spoke for two of those seconds. Was the agent slow to
start talking, or did it just give a long answer? Those are completely different
failures with completely different fixes, and your tracing cannot tell them
apart.

That is not a gap in your instrumentation. It is a gap in the model underneath
it.

## Everything assumes request → response

The OpenTelemetry GenAI semantic conventions — still pre-1.0 as of mid-2026 —
define spans named `chat`, `invoke_agent`, and `execute_tool`, with attributes
like `gen_ai.request.model` and `gen_ai.usage.input_tokens`. Every LLM
observability library in the ecosystem is built on the same premise: you send a
prompt, a span opens; the completion returns, the span closes.

For chat completions that is exactly right.

Real-time voice agents do not work that way. Gemini Live and OpenAI Realtime
hold a **persistent bidirectional WebSocket**. Audio flows in both directions
simultaneously. There is no request. There is no response. There is no moment
where either party is definitively finished — because the user can, and
constantly does, talk straight over the model.

Four things break at once.

**There is no span boundary.** Nothing on the wire says "a request started."
Audio just streams. Instrument it naively and you get one span per session,
spanning twenty exchanges, telling you nothing.

**Duration is the wrong metric.** What makes a voice agent feel alive is not how
long the exchange took. It is how long the human sat in *silence* after they
stopped talking, before they heard anything at all. A turn can have a perfectly
respectable total duration and still feel broken, because all the delay landed
at the front.

**Barge-in has no representation.** The single most common failure mode of voice
agents — the user interrupting — does not exist in any convention, because in a
request/response world it *cannot* exist. Yet how often it happens, and how far
into the agent's reply, is the strongest signal of conversational quality you
can measure.

**Cost accounting doesn't apply.** Input isn't a countable prompt. It's an open
microphone billed per second, plus optional video frames. `input_tokens` is
still emitted, but without a modality split it hides which stream is actually
spending your money.

So I built `cadence`.

## Reconstructing a turn from a stream

The core problem: **carve conversational structure out of a continuous duplex
signal.** cadence consumes the stream and emits this:

```
realtime.session                    one connected session
├── realtime.turn                        one exchange
│   ├── realtime.audio.user_utterance          VAD speech-start → speech-end
│   ├── chat                          speech-end → generation done   [gen_ai.*]
│   │   └── execute_tool              tools, including mid-stream
│   └── realtime.audio.agent_utterance         first audio out → playback done
│       └── (event) realtime.barge_in    offset_ms into the reply
└── realtime.turn …
```

Note that `chat` and `execute_tool` keep their standard GenAI names and
attributes. The `realtime.*` namespace **composes with** `gen_ai.*` rather than
replacing it — model inference inside a turn is still just a model call, and
existing backends should light up on it unchanged.

Three decisions in that model turned out to be load-bearing, and I got each of
them wrong first.

### 1. Measure latency at the audio boundary, not the model boundary

`realtime.turn.time_to_first_audio` runs from the **last inbound frame of user
audio** to the **first outbound frame of agent audio**.

That interval deliberately includes VAD dwell time, network transit, model
queueing, and time-to-first-token. All of them are silence to the person
waiting. If you measure from the model call instead, you get a number that looks
great on a dashboard while users complain the thing is unresponsive — because
you excluded the 400ms of voice-activity-detection hangover sitting in front of
it.

My first implementation measured from turn start. That included the user's own
speech, so a person who asked a long question looked like a slow agent. The
fix is one line and the test that catches it is worth more than the line:

```python
def test_ttfa_measured_from_end_of_user_speech(harness):
    rec.handle(ev(EventType.USER_SPEECH_START, 10.0))
    rec.handle(ev(EventType.USER_SPEECH_END, 12.0))          # spoke for 2s
    rec.handle(ev(EventType.AGENT_AUDIO_CHUNK, 12.42))       # replied 420ms later

    # 420ms, not 2420ms.
    assert turn.attributes[TURN_TTFA_MS] == approx(420.0)
```

### 2. A barge-in ends the turn and starts a new one

My first instinct was to record interruption as a flag on the ongoing turn. That
is wrong. An interruption is not an error *inside* a turn — it is the user
taking the floor, which is the definition of a new turn. Model it any other way
and turn nesting becomes incoherent the moment a conversation gets lively.

The interruption itself is recorded as a **span event on the agent utterance it
cut off**, carrying `realtime.turn.barge_in_offset_ms`. Putting it there rather than on
a separate span means it lands at the exact point in the waterfall where the
user cut in, which is what makes the trace readable at a glance.

The offset **distribution** is the real diagnostic, not the count:

- Offsets clustering under ~400ms → your VAD is firing on background noise, not
  speech. Users aren't interrupting; your agent thinks they are.
- Consistently large offsets → your agent is rambling and people are cutting it
  off out of impatience.

Same event count, opposite root causes, opposite fixes. You cannot distinguish
them without the distribution.

There is also a subtle double-counting trap. Gemini reports an `interrupted`
flag *after* its own VAD decides the user has taken the floor — but cadence may
have already detected the barge-in locally from inbound audio arriving during
playback. Count both and every barge-in dashboard in your org reads exactly
double. There's a test for that too.

### 3. A missing metric beats a wrong one

Agents speak first sometimes — proactive greetings, tool-triggered utterances.
Those turns have no preceding user speech, so there is nothing to measure TTFA
*from*.

The tempting move is to fall back to session start. Don't. That silently injects
garbage into your p95, and a corrupted latency percentile is worse than no
percentile, because you'll act on it. cadence omits the attribute entirely and
asserts on that:

```python
def test_agent_initiated_turn_has_no_ttfa(harness):
    assert TURN_TTFA_MS not in turn.attributes
```

## Making the silence visible

The console renders a live scrolling ribbon: your voice in sky blue, the agent
in violet, and between them **the silence drawn as literal empty space**, with
the milliseconds ticking upward while you wait.

That gap is the entire argument. In a conventional trace view it is invisible —
it's the space *between* two spans, and no waterfall draws the space between
spans. Here it's the largest thing on screen, because to the person talking to
the agent it is the only part of the turn they actually experience.

Watching the number climb while you sit in silence, then freeze and grade itself
green or red, communicates more about voice latency in three seconds than a
percentile chart does in a week.

## SigNoz: writing telemetry, then reading it back

cadence exports over OTLP to SigNoz, and the dashboard covers TTFA percentiles,
barge-in rate and offset distribution, turns by end reason, and token spend by
modality.

Two things needed care.

**Histogram buckets.** OpenTelemetry's defaults are tuned for HTTP request
durations in seconds. Applied to voice latency, essentially every measurement
lands in one bucket and your p95 is noise. cadence registers explicit views —
50, 100, 150, 200, 300, 400, 500, 650, 800, 1000, 1250, 1500, 2000, 3000, 5000
ms — chosen so the interesting region is actually resolved.

**Cardinality.** `realtime.session.id` is deliberately *not* a metric attribute.
Unique ids on metric dimensions are the classic way to melt a time-series
backend. Session correlation belongs on spans, where it's free.

Then the part that makes the demo: **the agent queries SigNoz for its own
traces.**

Ask it *"how fast have you been responding?"* and it runs a p95 query against
the very histogram its own turns populated seconds earlier, then answers out
loud — "about 340 milliseconds, fast enough to feel immediate." Ask *"how often
do I cut you off?"* and it reads back its own barge-in distribution and
interprets it.

cadence writes the telemetry. SigNoz stores it. The agent reads it back and
tells you about itself. The loop closes.

One honest detail: metric export has ingestion lag, so for the first minute of a
session SigNoz has nothing. The tools fall back to live in-process stats **and
say which source they used** — "SigNoz hasn't picked this session up yet, but
right now it's around 340 milliseconds." An agent that confidently reports a p95
it does not have is worse than one that admits the data hasn't landed.


## It works against the real API — and the numbers surprised me

Everything above was built against synthetic events and a traffic simulator,
which is fine for designing a state machine but proves nothing about the
adapter that actually touches Gemini. So the last thing I did was run real
sessions through it.

All nine normalized events fired from real `LiveServerMessage`s — VAD
boundaries, turn completion, tool calls, usage metadata, interruption. The
adapter's field mappings, written from SDK introspection, held up.

Then the numbers:

| source | turns | mean TTFA | p95 |
|---|---|---|---|
| simulated baseline | 1,887 | 248 ms | 442 ms |
| simulated regression | 2,128 | 331 ms | 719 ms |
| **real Gemini Live** | **17** | **1,120 ms** | **1,567 ms** |

Real Gemini Live is three to four times slower than the baseline I had been
designing against. My 350ms objective — which I picked from turn-taking
research, and still believe is the right target for a *spoken* turn — is not
close to achievable on text-driven turns through this model today.

That is exactly the kind of thing you only learn by measuring, and it is a
better argument for the project than any dashboard: I built the instrument,
pointed it at reality, and reality disagreed with my assumptions.

One more finding, from the same run. Barge-in fired twelve times across
fifteen turns — far more than a scripted run should produce. The cause is real:
sending the next prompt promptly after `turn_complete` arrives makes Gemini
report an interruption, because audio from the previous turn is still
streaming. Good evidence that barge-in detection works. Also a reminder that a
metric can be correct and still need context before you act on it.

## What I'd do differently

The `realtime.*` conventions are a **proposal**, not a standard, and there are
genuinely open questions. Should `realtime.turn` be its own span name, or
`invoke_agent` with a `realtime.*` attribute set? The latter reuses more existing
tooling; I chose the former for clarity, and I'm not certain that was right.
Multi-party audio is entirely out of scope. And I'm unsure whether TTFA is the
right primary metric or whether the standard should also define
time-to-first-*word* from output transcription — the two diverge when a model
emits filler audio before content.

Only the Gemini Live adapter exists today. But the turn state machine is
provider-neutral: a provider ships a ~200-line translator into a small event
vocabulary and inherits the whole span model. OpenAI Realtime is an adapter, not
a second implementation.

## Try it

The library is two lines around an existing session:

```python
import cadence
cadence.configure(service_name="my-voice-agent")

async with client.aio.live.connect(model=MODEL, config=config) as raw:
    async with cadence.CadenceSession(raw, model=MODEL) as session:
        async for message in session.receive():
            ...   # every message passes through untouched
```

`CadenceSession` wraps rather than monkey-patches — the Live API surface is
still moving, and patching a library mid-flight is how instrumentation gets
silently broken by a minor release. And instrumentation never raises into the
audio path: a bug in cadence must not take down the agent it's watching. There's
a test for exactly that.

Repo, semantic convention spec, and importable SigNoz dashboard: **[link]**

---

*Built for the [Agents of SigNoz](https://www.wemakedevs.org/hackathons/signoz)
hackathon.*
