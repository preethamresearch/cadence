<div align="center">

# cadence

**OpenTelemetry instrumentation for real-time, full-duplex voice agents.**

Turn boundaries, time-to-first-audio, and barge-in — traced from a streaming
audio socket and shipped to [SigNoz](https://signoz.io).

*Built for the [Agents of SigNoz](https://www.wemakedevs.org/hackathons/signoz)
hackathon · Track 1: AI & Agent Observability*

</div>

---

## The problem

Every LLM observability tool assumes **request → response**. You send a prompt,
a span opens; the completion returns, the span closes. The OpenTelemetry GenAI
semantic conventions are built on that shape, and for chat completions it works
fine.

Real-time voice agents do not work that way.

Gemini Live and OpenAI Realtime speak over a **persistent bidirectional
WebSocket**. Audio flows both directions simultaneously. There is no request,
no response, and no moment at which either side is definitively finished —
the user can and does talk over the model mid-sentence.

So when a voice agent feels broken in production, the existing tooling cannot
tell you why:

- **There is no span boundary.** Instrument it naively and you get one span per
  session covering twenty exchanges.
- **Duration is the wrong metric.** What matters is how long the human sat in
  *silence* before hearing anything — not how long the exchange took in total.
- **Barge-in is invisible.** The most common failure mode of voice agents has
  no representation in any convention, because in a request/response world it
  cannot happen.
- **Cost accounting breaks.** Input is an open microphone billed per second,
  not a countable prompt.

cadence fills that gap: a `voice.*` semantic convention that composes with
`gen_ai.*`, a state machine that reconstructs conversational structure from
the raw signal stream, and a SigNoz dashboard of the SLIs that result.

📄 **[Read the full semantic convention spec →](docs/SEMCONV.md)**

---

## What it produces

```
voice.conversation                    one connected session
├── voice.turn                        one exchange
│   ├── voice.user_utterance          VAD speech-start → speech-end
│   ├── chat                          speech-end → generation done   [gen_ai.*]
│   │   └── execute_tool              tools, including mid-stream
│   └── voice.agent_utterance         first audio out → playback done
│       └── (event) voice.barge_in    offset_ms into the reply
└── voice.turn …
```

Plus metrics purpose-built for voice: a **time-to-first-audio** histogram with
buckets that actually resolve (50ms–5s, not OpenTelemetry's HTTP-shaped
defaults), barge-in **offset distribution**, and token spend split by
**modality** — because in a realtime session, audio is the bill.

---

## The demo: an agent that reads its own telemetry

The demo app is a voice agent wired into a loop:

> **cadence** writes its turns into SigNoz → the agent **queries SigNoz** for
> its own traces → it tells you how it has been performing, out loud.

Ask it *"how fast have you been responding?"* and it runs a p95 query against
the very histogram its own turns populated seconds earlier. Ask *"how often do
I cut you off?"* and it reads back its own barge-in distribution — then
interprets it ("interruptions landing under 400ms usually means VAD is firing
on background noise").

Because metric export has ingestion lag, the tools fall back to live
in-process stats for the first minute of a session **and say which source they
used**. An agent that confidently reports a p95 it does not have is worse than
one that admits the data has not landed yet.

### The console

A live scrolling ribbon of who holds the floor. Your voice in sky blue, the
agent in violet, and between them **the silence drawn as literal empty space**
with the milliseconds ticking up while you wait.

That gap is the whole argument. In a conventional trace view it is invisible —
it is the space *between* two spans. Here it is the largest thing on screen,
because to the person talking to the agent it is the only part of the turn
they actually experience.

Interrupt the agent mid-sentence and a marker lands at the exact offset, on
screen and in the span.

> **No API key?** Visit `/?replay=1` for a scripted session using numbers from
> a real traced run.

---

## Quick start

```bash
git clone <this repo> && cd cadence
python3 -m venv .venv && .venv/bin/pip install -e ".[app,dev]"
cp .env.example .env      # then fill it in
.venv/bin/uvicorn app.main:app --reload --port 8080
```

Open <http://localhost:8080>, press **start**, and talk.

### Configuration

| Variable | Purpose |
|---|---|
| `GEMINI_API_KEY` | [AI Studio key](https://aistudio.google.com/apikey) for Gemini Live. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `https://ingest.<region>.signoz.cloud:443` |
| `SIGNOZ_INGESTION_KEY` | SigNoz Cloud → Settings → Ingestion. **Writes** telemetry. |
| `SIGNOZ_REGION` / `SIGNOZ_API_KEY` | SigNoz Cloud → Settings → API Keys. **Reads** it back for the self-observation tools. |
| `CADENCE_CAPTURE_CONTENT` | Record transcripts as span attributes. Off by default — utterance text is user content. |

cadence works against any OTLP backend; SigNoz is simply where this was built
and dashboarded.

---

## Using the library

Two lines around an existing session:

```python
import cadence
from google import genai

cadence.configure(service_name="my-voice-agent")

client = genai.Client(api_key=...)
async with client.aio.live.connect(model=MODEL, config=config) as raw:
    async with cadence.CadenceSession(raw, model=MODEL) as session:
        await session.send_realtime_input(audio=chunk)
        async for message in session.receive():
            ...   # unchanged: cadence yields every message through untouched
```

`CadenceSession` **wraps** rather than monkey-patches. The Live API surface is
still moving, and patching a library mid-flight is how instrumentation gets
silently broken by a minor release. Anything cadence does not explicitly wrap
passes straight through via `__getattr__`.

Instrumentation never raises into the audio path — a bug in cadence must not
take down the agent it is watching. There is a test for exactly that.

### Adding a provider

The turn state machine is provider-neutral. Providers ship a translator from
their wire format into a small event vocabulary
(`src/cadence/providers/gemini.py`, ~200 lines), and inherit the entire span
model. OpenAI Realtime is an adapter, not a second implementation.

---

## SigNoz dashboard

```
dashboards/cadence-voice-agent-dashboard.json
```

Import via **Dashboards → Import JSON**. Eight panels: TTFA percentiles, barge-in
rate and offset distribution, turns by end reason, turn duration, token spend
by modality, and audio seconds in both directions.

Regenerate with `python dashboards/build_dashboard.py`.

### The alert worth having

**Alerts → New Alert → Metrics**, on `voice.turn.time_to_first_audio.bucket`
with `spaceAggregation: p95`:

| Setting | Value | Why |
|---|---|---|
| Threshold | `> 800` ms | Past ~800ms the pause is unmistakable. Past ~1.5s users assume they were not heard and repeat themselves — which shows up as a barge-in spike, not a latency alert. |
| Evaluation | 5 min | Long enough to survive one slow turn. |

---

## Tests

```bash
.venv/bin/pytest -q
```

Nine scenarios drive the recorder with synthetic events and assert on the spans
that come out — no API key, microphone, or network required. Each is a case I
got wrong at least once while building it:

- clean turn produces the full span tree, correctly parented
- TTFA measured from end-of-speech, **not** including the user's own speech
- agent-initiated turns carry **no** TTFA rather than a fabricated one
- barge-in closes the interrupted turn and opens a new one
- server interrupt after client detection is **not** double-counted
- mid-stream tool calls flagged
- unclosed spans reaped when a socket dies mid-turn
- metrics recorded
- a throwing UI hook never propagates into the audio loop

---

## Layout

```
src/cadence/
  semconv.py       the voice.* namespace — normative reference
  recorder.py      the duplex turn state machine
  events.py        provider-neutral event vocabulary
  session.py       the wrapper around a live session
  tracing.py       OTLP wiring
  metrics.py       instruments
  providers/
    gemini.py      Gemini Live → neutral events
app/
  main.py          FastAPI bridge: browser ↔ Gemini ↔ cadence
  tools/           SigNoz query client + self-observation tools
  static/          the console
dashboards/        SigNoz dashboard + generator
docs/SEMCONV.md    the specification
```

---

## Honest limitations

- The `voice.*` conventions are a **proposal**, not a standard. See the open
  questions at the end of [SEMCONV.md](docs/SEMCONV.md).
- Only the Gemini Live adapter is implemented. The OpenAI Realtime adapter is
  designed for but not written.
- SigNoz stores OTel histograms as `.bucket` series; the query client tries
  both spellings because this has varied across versions.
- Multi-party audio is out of scope — these conventions assume one human and
  one agent.

---

## License

Apache-2.0
