<div align="center">

# cadence

**Telemetry for real-time multimodal agents.**

OpenTelemetry semantic conventions, an instrumentation SDK, and a Conversation
SLO for agents that talk, see, and act over a live stream — shipped to
[SigNoz](https://signoz.io).

*Built for the [Agents of SigNoz](https://www.wemakedevs.org/hackathons/signoz)
hackathon · Track 1: AI & Agent Observability*

**[▶ Live demo](https://preethamresearch.github.io/cadence/)** · no signup, no API key

</div>

---

## The gap

Every LLM observability tool assumes **request → response**. You send a prompt,
a span opens; the completion returns, the span closes. The OpenTelemetry GenAI
semantic conventions are built on that shape, and for chat completions they
work.

Real-time agents do not work that way. Voice agents, screen-sharing agents, and
computer-use agents hold a **persistent bidirectional stream** where signal
flows both directions at once. There is no request, no response, and no moment
where either side is finished — the human can and does interrupt mid-action.

Four things break:

1. **There is no span boundary.** Nothing marks a request. Instrument it
   naively and you get one span per session covering twenty exchanges.
2. **Duration is the wrong latency metric.** What matters is how long the human
   sat in *silence* before anything came back — not how long the exchange took.
3. **Interruption has no representation.** The most common failure mode of
   realtime agents cannot occur in a request/response world, so no convention
   describes it.
4. **Cost accounting breaks.** Input is an open microphone billed per second,
   not a countable prompt.

📄 **[Read the semantic convention spec →](docs/SEMCONV.md)**

---

## What it gives you

### 1. A versioned schema — `realtime.*`

A single root with domain sub-namespaces, mirroring how `gen_ai.*` covers all
of LLM work in OpenTelemetry. One root because a convention is only useful if
people can remember and cite it, and `realtime.*` extends to modalities that
don't exist yet without forcing a rename.

| Namespace | Covers |
|---|---|
| `realtime.session.*` | the connected session, transport, prompt version |
| `realtime.turn.*` | turn structure, interruption, repair, outcome |
| `realtime.audio.*` | TTFA, stream gaps, voice identity, seconds streamed |
| `realtime.vision.*` | camera and screen frames |
| `realtime.tool.*` | tool execution, including mid-stream and interrupted |
| `realtime.browser.*` | computer-use actions *(specified, no adapter yet)* |

The schema is treated as a **public API**: `SCHEMA_VERSION` is semver, names
are never renamed within a major version, and the version is stamped on every
exported resource.

### 2. Signals that don't exist elsewhere

```
realtime.session                          one connected session
├── realtime.turn                         one exchange
│   ├── realtime.audio.user_utterance     input start → input end
│   ├── chat                              input end → generation done  [gen_ai.*]
│   │   └── execute_tool                  tools, including mid-stream
│   └── realtime.audio.agent_utterance    first output → playback done
│       └── (event) realtime.barge_in     offset_ms into the reply
└── realtime.turn …
```

- **Time to first audio** — the silence the user actually sat through
- **Barge-in offset distribution** — *where* interruptions land, not just how many
- **Speech overlap** — how long both parties talked at once before the agent yielded
- **Repair rate** — how often the user had to repeat, correct, or ask again
- **Containment / transfer** — did the agent finish the job, or did a human
- **Stream gaps** — stutter *during* speech, distinct from a slow start
- **Prompt version** on every span and metric — so a regression is attributable
  to the deploy that caused it

### 3. The Conversation SLO

Metrics say what happened. An SLO says whether it was acceptable.

| Objective | Target | Why this number |
|---|---|---|
| TTFA p95 | < 350 ms | Human turn-taking runs ~200ms; past 350ms reads as hesitation |
| Interruptions / session | < 0.8 | Above this users are fighting for the floor |
| Mean overlap | < 150 ms | Above this the agent is talking over people |
| Containment | > 72% | The number the deployment is funded on |
| Repair turns | < 9% | The closest proxy for "did it actually work" |
| Human transfer | < 11% | The commercial ceiling on escalation |

Each carries an **error budget** rather than a hard threshold: one slow turn is
not an incident, and treating it as one teaches people to ignore alerts.
Targets are defaults — `ConversationSLO.custom()` exists because a drive-through
and a medical triage line have different tolerances.

### 4. Regression attribution

An alert saying *"TTFA crossed 350ms"* tells you what the chart already showed.
`cadence.analysis` says which dimension explains it:

> `realtime.turn.time_to_first_audio regressed +97% (300 → 592). TTFA +97% for
> realtime.prompt.version=v17, while realtime.prompt.version=v16 stayed flat.`

It stays quiet when nothing explains the change — if every segment moved
together, it says *"look upstream"* rather than inventing a culprit. A
confident wrong attribution is worse than none, because people act on it.

---

## Verified against a live SigNoz

Not a claim — this was run end to end, and every failure along the way is
documented in the commit history:

| Signal | Verified |
|---|---|
| Spans ingested | **16,596** across ~1,600 simulated conversations |
| Metric series | **45** distinct `realtime.*` series |
| Trace shape | `realtime.session` → `realtime.turn` → utterances → `chat` → `execute_tool`, 46 spans in a 1.35-minute session |
| TTFA p95 | **487ms** on prompt v16, **720ms** on v17 — against a 350ms objective |
| Dashboards | 2, provisioned over the API, populated, targets drawn |
| Alerts | 3 rules live, provisioned **through the SigNoz MCP server** |
| MCP | `signoz: ✔ Connected` in Claude Code; 41 tools |
| Overhead | **1.5 µs/event** ≈ 4.5ms CPU per minute of conversation |
| **Real Gemini Live** | **17 turns across 3 live sessions** — all 9 adapter events verified against real `LiveServerMessage`s |

### Real vs simulated, side by side

Both live in the same dashboards, separated by `realtime.prompt.version`:

| Source | Turns | Mean TTFA | p95 |
|---|---|---|---|
| simulated baseline (`v16`) | 1,887 | 248 ms | 442 ms |
| simulated regression (`v17`) | 2,128 | 331 ms | 719 ms |
| **real Gemini Live** (`real-text-driven`) | **17** | **1,120 ms** | **1,567 ms** |

Real Gemini Live is three to four times slower than the baseline the simulator
was tuned to. The 350ms objective is right for a *spoken* turn — it comes from
turn-taking research — but it is not close to achievable on text-driven turns
through this model today. That gap is the point: the instrument was built,
pointed at reality, and reality disagreed.

`scripts/real_session.py` drives real sessions with text input and audio output,
so it needs no microphone, and prints which normalized events the adapter
actually produced. Before it existed, `providers/gemini.py` — the one component
touching the vendor API — had never processed a real message.

### Six silent failures, and what they cost

Realtime observability fails quietly — nothing crashes, and the application
reports success throughout. Each of these was found only by looking at real
data, and each is why `scripts/doctor.py` exists:

1. **Collector serving `nop` pipelines.** SigNoz will not hand a collector its
   config until an organisation exists. The OTLP port accepted TCP and never
   answered; the exporter retried and dropped everything.
2. **Exporter queue overflow.** The SDK's default 2048-span queue silently lost
   60% of a realtime workload — 1,201 turns delivered as 497.
3. **Spans stamped at wall-clock.** A replayed session produced traces whose
   attributes said 400ms while the waterfall said 0.06ms.
4. **Monotonic time used as epoch.** Every span landed near 1970.
5. **Session span opened eagerly**, closed in the simulated past — negative
   duration, wrapped to nonsense.
6. **Cumulative instead of delta temporality.** SigNoz scanned 36,000 rows and
   returned "No Data" on every percentile and rate query.

Not one of these raised an exception.

---

## Quick start

### SigNoz, via Foundry

Foundry brings up SigNoz **and its MCP server** in one step:

```bash
curl -fsSL https://signoz.io/foundry.sh | bash
foundryctl cast -f deploy/casting.yaml
```

Then open <http://localhost:8080> and **complete the first-run signup**. This is
required: until an organisation exists, the config server refuses to register
the collector and every pipeline stays `nop` — the OTLP port accepts TCP but
never answers. (That failure mode cost me an hour; it is in the docs so it
doesn't cost you one.)

Ports: `8080` UI · `4317/4318` OTLP ingest · `8000` MCP server.

### cadence

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[app,dev]"
cp .env.example .env      # then fill it in
.venv/bin/python scripts/simulate.py --sessions 140   # populate the dashboards
.venv/bin/uvicorn app.main:app --reload --port 8080
```

### Instrumenting your own agent

Two lines around an existing session:

```python
import cadence
cadence.configure(service_name="my-voice-agent")

async with client.aio.live.connect(model=MODEL, config=config) as raw:
    async with cadence.CadenceSession(raw, model=MODEL, prompt_version="v17") as session:
        async for message in session.receive():
            ...   # every message passes through untouched
```

`CadenceSession` **wraps** rather than monkey-patches. The Live API surface is
still moving, and patching a library mid-flight is how instrumentation gets
silently broken by a minor release. Anything cadence doesn't wrap passes
through via `__getattr__`.

---

## Design commitments

**Provider neutrality.** The turn state machine consumes a universal event
vocabulary — speech start/end, interruption, tool execution, playback — and
never sees a vendor type. `providers/gemini.py` is a ~250-line translator.
Adding OpenAI Realtime is an adapter, not a second implementation.

**Low overhead, measured.** 1.5 µs per event — about **4.5 ms of CPU per minute
of conversation**. `tests/test_overhead.py` prints the number; it is a test, not
a claim.

**Privacy by design.** Content capture is off by default. Repair and fallback
detection runs **in-process** on transcripts and exports only the resulting
classification — the text never leaves unless you explicitly opt in. Enterprises
can collect every high-value signal here without raw audio or transcripts
leaving their environment.

**Instrumentation never raises into the agent.** A bug in cadence must not take
down the thing it is watching. There is a test for exactly that.

**Cardinality discipline.** Session and turn ids are *not* metric attributes.
Unique ids on metric dimensions are the standard way to melt a time-series
backend; correlation belongs on spans, where it is free.

---

## SigNoz integration

| What | Where |
|---|---|
| OTLP export (traces + metrics) | `src/cadence/tracing.py` |
| Foundry install incl. MCP server | `deploy/casting.yaml` |
| SLO dashboard | `dashboards/cadence-slo-dashboard.json` |
| Diagnostics dashboard | `dashboards/cadence-voice-agent-dashboard.json` |
| Query API client (v5 `query_range`) | `app/tools/signoz.py` |
| Agent tools that read SigNoz back | `app/tools/observability.py` |

Import dashboards via **Dashboards → Import JSON**. Regenerate with
`python dashboards/build_dashboard.py`.

**Alert worth having:** on `realtime.turn.time_to_first_audio.bucket`,
`spaceAggregation: p95`, threshold `> 350ms` over 5 minutes.

### The demo agent reads its own telemetry

cadence writes the agent's turns into SigNoz; the agent then queries SigNoz for
*its own* traces. Ask it how fast it's been responding and it runs a p95 against
the histogram its own turns populated seconds earlier, then answers out loud.

Because metric export has ingestion lag, the tools fall back to live in-process
stats for the first minute **and say which source they used**. An agent that
confidently reports a p95 it doesn't have is worse than one that admits the data
hasn't landed.

---

## Tests

```bash
.venv/bin/pytest -q            # 33 tests
.venv/bin/pytest tests/test_overhead.py -s   # prints the overhead number
```

Nearly every case is one I got wrong at least once:

- TTFA measured from end-of-speech, **not** including the user's own speech
- agent-initiated turns carry **no** TTFA rather than a fabricated one
- barge-in closes the interrupted turn and opens a new one
- server interrupt after client detection is **not** double-counted
- overlap measured separately from barge-in
- unclosed spans reaped when a socket dies mid-turn
- containment direction not inverted (the one "higher is better" objective)
- a broad regression is **not** attributed to any single segment
- a throwing UI hook never propagates into the audio loop

---

## Honest limitations

- The `realtime.*` conventions are a **proposal**, not a standard. Open
  questions are listed at the end of [SEMCONV.md](docs/SEMCONV.md).
- Only the Gemini Live adapter is implemented. `realtime.browser.*` is specified
  but has no adapter; `realtime.vision.*` is frame counting only.
- Repair and fallback detection is **heuristic phrase matching**. It will miss
  politely-worded repairs and occasionally fire on a quotation. It is a trend
  instrument, not a verdict on any single turn — see `src/cadence/dialogue.py`,
  which says so at length.
- `scripts/simulate.py` synthesises **audio timing** to populate dashboards. The
  recorder, SDK, OTLP export and SigNoz storage it drives are all real, but the
  conversations are generated, and the repo says so rather than presenting them
  as production traffic.
- Multi-party audio is out of scope.

---

## Layout

```
src/cadence/
  semconv.py       the realtime.* schema — versioned public API
  recorder.py      the duplex turn state machine
  events.py        provider-neutral event vocabulary
  dialogue.py      repair/fallback heuristics, and their limits
  slo.py           the Conversation SLO
  analysis.py      regression detection and attribution
  session.py       the wrapper around a live session
  providers/gemini.py
app/               FastAPI bridge, console, landing page
dashboards/        SigNoz dashboards + generator
deploy/            Foundry casting
scripts/simulate.py
docs/SEMCONV.md    the specification
```

## License

Apache-2.0
