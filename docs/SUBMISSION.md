# Submission — Agents of SigNoz

Answers for <https://forms.gle/xv1TXSiC54MEWujRA>. Fill the bracketed bits.

---

**Track:** Track 1 — AI & Agent Observability

**Team name:** `[…]`

**Name of the person submitting:** Preetham

**Email:** sjp.preetham@gmail.com

**GitHub link:** https://github.com/preethamresearch/cadence

**Deployed link:** https://preethamresearch.github.io/cadence/

**YouTube demo:** https://youtu.be/CZ2TeXH-yFY

**Blog:** `[Dev.to URL]`

---

## Project description

> cadence is telemetry for real-time multimodal agents: a versioned
> OpenTelemetry semantic convention, an instrumentation SDK, and a Conversation
> SLO — shipped to SigNoz.
>
> Every LLM observability tool assumes request → response. Real-time voice
> agents hold a persistent duplex stream where signal flows both directions at
> once, so four things break: there is no span boundary, duration is the wrong
> latency metric, interruption has no representation in any convention, and
> cost accounting fails because input is an open microphone rather than a
> countable prompt.
>
> cadence reconstructs conversational turn structure from the raw signal
> stream. It measures the silence the user actually sat through (time to first
> audio), where interruptions land inside a reply, how long both parties spoke
> at once, how often users had to repeat themselves, and whether the agent
> finished the job or handed off to a human. All of it exports over OTLP as
> ordinary OpenTelemetry, composing with the existing `gen_ai.*` conventions
> rather than replacing them.
>
> On top of that sits a Conversation SLO — six objectives with targets and
> error budgets — and a regression analyser that does not just say "TTFA rose"
> but "TTFA rose 48% on prompt v17 while v16 stayed flat".

## How I used SigNoz

> SigNoz is the backend cadence was built and validated against, using six of
> its features:
>
> **1. Foundry.** The stack is provisioned as code — `deploy/casting.yaml`
> brings up SigNoz *and* its MCP server in one `foundryctl cast`.
>
> **2. OTLP ingestion.** cadence exports traces and metrics over OTLP
> http/protobuf. The instance currently holds ~16,600 spans and 45 distinct
> `realtime.*` metric series from ~1,600 simulated conversations.
>
> **3. Traces.** The duplex span tree renders as an ordinary SigNoz waterfall:
> `realtime.session` → `realtime.turn` → user utterance, `chat` (standard
> `gen_ai.*` attributes), `execute_tool`, agent utterance, with barge-in
> recorded as a span event at its true offset inside the utterance it
> interrupted.
>
> **4. Query Builder / v5 query API.** `app/tools/signoz.py` queries
> `/api/v5/query_range` so the demo agent can read its own telemetry back —
> ask it how fast it has been responding and it runs a p95 against the
> histogram its own turns populated seconds earlier.
>
> **5. Dashboards.** Two, provisioned over the API from version control
> (`scripts/import_dashboards.py`): a **Conversation SLO** board with each
> objective's target drawn on its panel, and a **Diagnostics** board that
> splits every signal by prompt version and transport — the two dimensions
> that actually explain a realtime regression.
>
> **6. Alerts + MCP server.** Three alert rules provisioned *through the SigNoz
> MCP server* (`scripts/create_alerts.py`), each carrying its objective's
> rationale in the annotation so an on-call engineer sees why the threshold is
> where it is. The MCP server is also registered with Claude Code, so an agent
> can query the telemetry conversationally.
>
> A note worth making: I first tried creating the alerts over the REST API,
> which rejects a bad payload with a flat `"alert rule is not valid"` and no
> indication of the offending field. The MCP server validated the same payload
> and named the problems exactly (`condition.compositeQuery.queryType` missing;
> `condition.thresholds` needs `{kind, spec[]}`). That turned an afternoon of
> guessing into ten minutes, which is a genuine argument for the MCP server as
> a provisioning surface even outside AI workflows.
>
> Finally, `scripts/doctor.py` verifies the whole chain — library, collector
> config, transport, and a real round trip — because in a realtime stack every
> link fails silently.

## Hackathon experience

> The interesting part was how much of realtime observability fails *quietly*.
> Over the build I hit six bugs where nothing crashed and the application
> reported success throughout: a collector serving `nop` pipelines because no
> org existed yet, an exporter queue silently dropping 60% of spans, spans
> stamped at wall-clock instead of event time, and — the best one — cumulative
> instead of delta temporality, which made SigNoz scan 36,000 rows and return
> "No Data".
>
> That shaped the project. It's why `doctor` exists, why the tests assert exact
> values rather than presence, and why the recorder omits a metric it cannot
> measure honestly instead of fabricating one.

---

---

## AI assistant disclosure  *(required by the rules — omitting this is disqualification)*

> This project was built with heavy use of an AI coding assistant (Claude, via
> Claude Code). It was used throughout: designing the `realtime.*` semantic
> conventions, implementing the duplex turn state machine, writing the tests,
> building the console and landing page, provisioning SigNoz dashboards and
> alerts, and diagnosing the eight silent failures documented in the README.
>
> Direction, judgement calls, and review were mine — including the schema
> naming decision, the SLO thresholds, and the decision to run against the real
> Gemini Live API rather than ship a simulator-only submission. All code was
> written during the hackathon window.

---

## Notes for judges (put in the README, not the form)

**What's real vs. generated.** The recorder, semantic conventions, SDK, OTLP
export, dashboards, alerts, and SigNoz storage are all production code paths.
`scripts/simulate.py` generates the *conversations* — audio timings and
transcripts — because dashboards and regression detection cannot be
demonstrated against three hand-held turns. The telemetry pipeline they drive
is entirely real, and the repo says so rather than implying production traffic.

**What's implemented vs. specified.** Gemini Live is implemented and tested.
`realtime.vision.*` is frame counting only. `realtime.browser.*` is specified
with no adapter — the constants exist so one has a fixed target instead of
inventing its own names.

**Repair/fallback detection is heuristic** phrase matching over transcripts. It
will miss politely-worded repairs and occasionally fire on a quotation. It is a
trend instrument, not a verdict on a single turn, and `src/cadence/dialogue.py`
says so at length.
