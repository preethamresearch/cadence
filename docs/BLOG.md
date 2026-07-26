# SigNoz scanned 36,000 rows and told me "No Data"

I spent an hour convinced my instrumentation was broken. It wasn't. The
telemetry was arriving, ClickHouse was reading it, and every dashboard panel
still said **No Data** — over a table that had 36,291 rows in it.

That was one of eight bugs I hit last week building OpenTelemetry
instrumentation for real-time voice agents. Not one of them threw an
exception. Not one showed up in a log. Six of them I found *after* the code
was already "working."

This post is the list, with the commands I used to find each one. If you are
instrumenting anything that streams — voice, video, an agent loop — you will
probably hit at least three of these.

---

## What I was building, briefly

Voice agents like Gemini Live hold a **persistent bidirectional socket**.
Audio goes both ways at once. There is no request and no response, so the
OpenTelemetry GenAI conventions — which define `chat` spans around a
request/response pair — have nothing to attach to.

The number that actually matters for a voice agent is **time to first audio**:
how long the human sat in silence after they stopped talking. That interval
lives *between* spans, so no waterfall draws it.

So I built `cadence`: a state machine that reconstructs turn structure from the
raw signal stream and emits it as ordinary OTLP.

```
realtime.session                        one connected session
├── realtime.turn                       one exchange
│   ├── realtime.audio.user_utterance   VAD start → end
│   ├── chat                            [standard gen_ai.* attributes]
│   │   └── execute_tool
│   └── realtime.audio.agent_utterance  first audio out → done
│       └── (event) realtime.barge_in   offset_ms into the reply
```

![Real Gemini Live session as an OpenTelemetry trace in SigNoz](./blog_assets/01-trace-waterfall.jpg)

That is a real Gemini session in SigNoz. Getting there took the eight bugs
below.

---

## 1. The collector was running, and doing nothing

I installed SigNoz with Foundry, which brings up the backend *and* its MCP
server in one step:

```bash
curl -fsSL https://signoz.io/foundry.sh | bash
foundryctl cast -f deploy/casting.yaml
```

Containers healthy. Port 4318 open. And the exporter kept logging
`RemoteDisconnected`. `curl` confirmed it — TCP connected, then the server hung
up without an HTTP response:

```
* Connected to localhost (127.0.0.1) port 4318
> POST /v1/traces HTTP/1.1
* Empty reply from server
```

The effective collector config explains it:

```bash
docker exec signoz-ingester-1 sh -lc \
  "awk '/^service:/{f=1} f' /var/tmp/collector-config.yaml"
```

```yaml
service:
  pipelines:
    traces:
      exporters: [nop]
      receivers: [nop]
```

Every pipeline was `nop`. The config *file* on disk was correct — the running
collector had never received it. The reason was in the SigNoz server log:

```
failed to find or create agent ... "cannot create agent without orgId"
```

**SigNoz will not hand a collector its configuration until an organisation
exists.** I had not completed the first-run signup at `localhost:8080`. Until
you do, the OTLP port accepts connections and answers nothing.

Do the signup, restart the ingester, and the pipelines populate.

---

## 2. The exporter silently dropped 60% of my spans

I generated 1,201 turns. ClickHouse had 497.

```sql
SELECT name, count() FROM signoz_traces.distributed_signoz_index_v3
GROUP BY name ORDER BY count() DESC
```

No error surfaced. The OpenTelemetry `BatchSpanProcessor` defaults to a
**2,048-span queue**, and a realtime workload overruns it instantly — each turn
produces four to six spans, so a few hundred concurrent turns is enough. When
the queue is full, spans are dropped, and the SDK does not raise.

```python
BatchSpanProcessor(
    OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces", headers=headers),
    max_queue_size=32_768,        # default 2048 — far too small for streaming
    max_export_batch_size=1_024,
    schedule_delay_millis=2_000,
)
```

Re-ran it: 1,201 turns generated, 1,201 delivered.

---

## 3, 4, 5. Three ways to get timestamps wrong

I replay recorded sessions to populate dashboards, and replayed spans came out
**0.06 ms long** while their attributes claimed 400 ms. Spans were being stamped
at wall-clock "now" rather than at the time the event carried, so a session
replayed in one second produced microsecond spans.

The OpenTelemetry API accepts explicit timestamps; use them:

```python
span = tracer.start_span(name, start_time=event.wall_ns, ...)
span.end(end_time=event.wall_ns)
```

Then every span landed near **1970** — because I passed `time.monotonic()` as
the wall clock. Monotonic is time since boot, not since epoch.

And the session span came out with a duration of several hours, because I
opened it eagerly at "now" and closed it in the replayed past. Negative
duration, wrapped unsigned.

The one worth stealing: **compare two views of the same quantity.** The span
duration and the `duration_ms` attribute should agree, so check:

```sql
SELECT attributes_string['realtime.prompt.version'] AS src,
       round(avg(durationNano)/1e6) AS span_ms,
       round(avg(attributes_number['realtime.turn.duration_ms'])) AS attr_ms
FROM signoz_traces.distributed_signoz_index_v3
WHERE name = 'realtime.turn' GROUP BY src
```

```
v16    4863    26135501
v17    5344    26095317
```

A 4.8-second span with a `duration_ms` attribute of **26,135,501** — seven
hours. Durations were computed from `time.monotonic()` while spans were stamped
from the event clock. In a live session the two coincide and this is invisible.

---

## 6. Cumulative vs delta: the "No Data" over 36,000 rows

This is the one from the title.

Metrics were arriving. `signoz_metrics.distributed_samples_v4` had them. Every
percentile and rate query returned nothing, and the response was quietly
explicit about how much work it had done:

```json
"meta": { "rowsScanned": 36291, "bytesScanned": 495851 },
"data": { "results": [ { "queryName": "A", "aggregations": null } ] }
```

Scanned 36,291 rows. Aggregated to `null`.

**SigNoz expects delta temporality for counters and histograms. The
OpenTelemetry Python SDK defaults to cumulative.** Nothing errors — the data is
stored, and every rate and percentile computes to nothing.

```python
from opentelemetry.sdk.metrics import Counter, Histogram
from opentelemetry.sdk.metrics.export import AggregationTemporality

OTLPMetricExporter(
    endpoint=f"{endpoint}/v1/metrics",
    headers=headers,
    preferred_temporality={
        Counter: AggregationTemporality.DELTA,
        Histogram: AggregationTemporality.DELTA,
    },
)
```

One dictionary. Every panel populated.

![Conversation SLO dashboard with target lines drawn](./blog_assets/02-slo-dashboard.jpg)

Worth checking with the MCP server, which auto-fetches the metric's metadata
and tells you what it thinks it is holding:

```
metricType: histogram (auto-fetched)
temporality: delta (auto-fetched)
```

---

## 7 and 8. The two bugs only the real API found

Everything above was found against a simulator. So I pointed it at the actual
Gemini Live API — and my adapter, the one component that touches the vendor,
had never processed a real message.

It found two more.

**Barge-in counted against finished turns.** Real sessions reported 22 barge-in
events and **0% interrupted turns**. Those numbers must agree. The flag marking
"the agent is speaking" was only cleared by a playback event my application
never emitted, so every turn after the first looked like an interruption — and
the barge-in was attributed to a span that had been exported seconds earlier.

The subtlety in the fix: clear that flag when a turn *completes*, but **not**
when it is interrupted. On an interruption the turn ends the instant the user
cuts in while the agent's audio keeps playing — and that lingering audio is
exactly the overlap being measured.

**And the latency was nothing like I assumed.**

| Source | Turns | Mean TTFA | p95 |
|---|---|---|---|
| simulated `v16` | 416 | 246 ms | 436 ms |
| simulated `v17` | 428 | 328 ms | 716 ms |
| **real Gemini Live** | **77** | **1,107 ms** | **1,658 ms** |

![TTFA split by prompt version, with the real series well above both](./blog_assets/03-diagnostics-v16-v17.jpg)

Real Gemini Live is three to four times slower than the baseline I had spent a
day designing against. My 350 ms objective comes from turn-taking research and
I still think it is right for a *spoken* turn — but it is nowhere near
achievable on text-driven turns through this model today.

I left that in the README as found rather than quietly retuning the simulator
to match. Instruments that agree with your assumptions are not instruments.

---

## The thing I actually took away

Every one of these eight failures was **silent**. The application reported
success throughout. That is the property you want in production — your voice
agent must not die because telemetry is unhappy — and it is precisely what
makes this miserable to debug.

So I wrote a preflight check that walks the chain and names the broken link:

```
library     ✓ cadence imports            v0.1.0, schema 0.1.0
            ✓ turn state machine         TTFA 400ms (expected 400ms)
signoz      ✓ SigNoz set up              v0.134.0
            ✓ collector pipelines        real receivers and exporters
transport   ✓ OTLP port reachable
            ✓ OTLP endpoint answers      HTTP 200
round trip  ✓ span exported end-to-end
```

Two details in there matter more than the rest. It checks **TCP reachability
separately from "does it speak HTTP"**, because a `nop` pipeline accepts the
connection and closes it — that distinction is bug #1 and it cost me an hour.
And it drives a synthetic turn through the real state machine and asserts TTFA
is **400 ms, not 2,400 ms**, because silently wrong telemetry is worse than an
outage: nothing breaks, you just make decisions on bad numbers.

If you take one thing from this: **compare two views of the same quantity.**
Span duration against the duration attribute. Barge-in events against
interrupted turns. That single habit found three of my eight bugs, and none of
them would have surfaced any other way.

---

*Code, the `realtime.*` semantic conventions, and importable SigNoz dashboards:
[github.com/preethamresearch/cadence](https://github.com/preethamresearch/cadence)
· [3-minute demo](https://youtu.be/CZ2TeXH-yFY)*

*Built for the Agents of SigNoz hackathon. Written by me; I used an AI
assistant for the code and for editing this post, which is declared in the
submission.*
