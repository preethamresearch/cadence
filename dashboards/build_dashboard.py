"""Generate the cadence SigNoz dashboard.

Written as a generator rather than hand-authored JSON because the widget
schema is repetitive and the layout coordinates have to stay consistent with
the widget ids -- both are easy to desynchronise by hand and annoying to debug
inside SigNoz.

The schema matches SigNoz's own published dashboards (github.com/SigNoz/dashboards):
histogram percentiles query the `.bucket` series with a `spaceAggregation` of
`pNN`, counters use an `expression` aggregation.

    python dashboards/build_dashboard.py
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

# Deterministic ids so regenerating produces a clean diff rather than a
# wholesale replacement.
NAMESPACE = uuid.UUID("6f1d2c4a-8b3e-4a17-9c55-0d4e2a7b1f90")


def wid(slug: str) -> str:
    return str(uuid.uuid5(NAMESPACE, slug))


def metric_query(
    name: str,
    metric: str,
    *,
    legend: str,
    space_agg: str = "sum",
    time_agg: str = "rate",
    group_by: list[str] | None = None,
    filter_expr: str = "",
) -> dict:
    return {
        "dataSource": "metrics",
        "disabled": False,
        "expression": name,
        "functions": [],
        "groupBy": [
            {
                "dataType": "string",
                "id": f"{key}--string--tag--false",
                "isColumn": False,
                "isJSON": False,
                "key": key,
                "type": "tag",
            }
            for key in (group_by or [])
        ],
        "having": {"expression": ""},
        "legend": legend,
        "limit": None,
        "orderBy": [],
        "queryName": name,
        "stepInterval": 60,
        "aggregations": [
            {
                "metricName": metric,
                "temporality": None,
                "timeAggregation": time_agg,
                "spaceAggregation": space_agg,
                "reduceTo": "avg",
            }
        ],
        "filter": {"expression": filter_expr},
    }


def widget(
    slug: str,
    title: str,
    description: str,
    queries: list[dict],
    *,
    panel: str = "graph",
    unit: str = "",
    stacked: bool = False,
) -> dict:
    return {
        "description": description,
        "id": wid(slug),
        "isStacked": stacked,
        "nullZeroValues": "zero",
        "opacity": "1",
        "panelTypes": panel,
        "query": {
            "builder": {
                "queryData": queries,
                "queryFormulas": [],
            },
            "clickhouse_sql": [{"disabled": False, "legend": "", "name": "A", "query": ""}],
            "id": wid(slug + ":query"),
            "promql": [{"disabled": False, "legend": "", "name": "A", "query": ""}],
            "queryType": "builder",
        },
        "thresholds": [],
        "timePreferance": "GLOBAL_TIME",
        "title": title,
        "yAxisUnit": unit,
    }


TTFA = "voice.turn.time_to_first_audio"
BARGE_OFFSET = "voice.barge_in.offset"
TURN_DURATION = "voice.turn.duration"

widgets = [
    # ── the headline: is the agent responsive? ────────────────────────
    widget(
        "ttfa-percentiles",
        "Time to first audio",
        "Silence the user sat through between finishing speaking and hearing a "
        "reply. Below ~500ms feels immediate; past ~1s users assume they were "
        "not heard and start repeating themselves.",
        [
            metric_query("A", f"{TTFA}.bucket", legend="p50", space_agg="p50", time_agg=""),
            metric_query("B", f"{TTFA}.bucket", legend="p95", space_agg="p95", time_agg=""),
            metric_query("C", f"{TTFA}.bucket", legend="p99", space_agg="p99", time_agg=""),
        ],
        unit="ms",
    ),
    widget(
        "ttfa-p95-now",
        "TTFA p95 (current)",
        "Single number for the health check. This is the value the alert fires on.",
        [metric_query("A", f"{TTFA}.bucket", legend="p95", space_agg="p95", time_agg="")],
        panel="value",
        unit="ms",
    ),
    # ── conversational quality ────────────────────────────────────────
    widget(
        "barge-in-rate",
        "Barge-ins per minute",
        "How often users talk over the agent. A rising rate with no change in "
        "traffic is the earliest signal that replies have become too long or "
        "that voice activity detection is misfiring.",
        [
            metric_query(
                "A", "voice.barge_in.count", legend="{{voice.barge_in.source}}",
                group_by=["voice.barge_in.source"],
            )
        ],
        unit="",
    ),
    widget(
        "barge-in-offset",
        "Barge-in offset distribution",
        "How far into the agent's utterance interruptions land. Clustering below "
        "~400ms points at VAD firing on background noise rather than speech; "
        "consistently late interruptions mean the agent is rambling.",
        [
            metric_query("A", f"{BARGE_OFFSET}.bucket", legend="p50", space_agg="p50", time_agg=""),
            metric_query("B", f"{BARGE_OFFSET}.bucket", legend="p90", space_agg="p90", time_agg=""),
        ],
        unit="ms",
    ),
    widget(
        "turns-by-end-reason",
        "Turns by end reason",
        "Completed vs interrupted vs abandoned. The interrupted share is the "
        "conversational-quality SLI.",
        [
            metric_query(
                "A", "voice.turn.count", legend="{{voice.turn.end_reason}}",
                group_by=["voice.turn.end_reason"],
            )
        ],
        stacked=True,
    ),
    widget(
        "turn-duration",
        "Turn duration p95",
        "Wall-clock length of a full exchange. Read alongside TTFA: a slow turn "
        "with fast first audio is a long answer, not a slow system.",
        [metric_query("A", f"{TURN_DURATION}.bucket", legend="p95", space_agg="p95", time_agg="")],
        unit="ms",
    ),
    # ── cost ──────────────────────────────────────────────────────────
    widget(
        "tokens-by-modality",
        "Token spend by modality",
        "Realtime sessions bill an open microphone, not a countable prompt. "
        "Audio almost always dominates, and video frames are the usual source "
        "of a surprise bill.",
        [
            metric_query(
                "A", "gen_ai.client.token.usage",
                legend="{{gen_ai.token.type}} / {{gen_ai.token.modality}}",
                group_by=["gen_ai.token.type", "gen_ai.token.modality"],
            )
        ],
        stacked=True,
    ),
    widget(
        "audio-seconds",
        "Audio streamed",
        "Seconds of audio in each direction. The input series is the meter that "
        "actually runs while nobody is talking.",
        [
            metric_query("A", "voice.audio.input.seconds", legend="user -> model"),
            metric_query("B", "voice.audio.output.seconds", legend="model -> user"),
        ],
        unit="s",
    ),
]

# Two columns, 6 units wide each, in declaration order.
layout = []
for index, w in enumerate(widgets):
    layout.append(
        {
            "h": 6,
            "i": w["id"],
            "moved": False,
            "static": False,
            "w": 6,
            "x": 0 if index % 2 == 0 else 6,
            "y": (index // 2) * 6,
        }
    )

dashboard = {
    "description": (
        "Voice-agent SLIs emitted by cadence (github.com/…/cadence): turn "
        "boundaries, time to first audio, barge-in, and modality-split token "
        "spend for real-time full-duplex agents."
    ),
    "layout": layout,
    "name": "Cadence — Real-time Voice Agent",
    "tags": ["cadence", "voice", "genai", "opentelemetry"],
    "title": "Cadence — Real-time Voice Agent",
    "variables": {},
    "version": "v4",
    "widgets": widgets,
}

out = Path(__file__).parent / "cadence-voice-agent-dashboard.json"
out.write_text(json.dumps(dashboard, indent=2) + "\n")
print(f"wrote {out} ({len(widgets)} widgets)")
