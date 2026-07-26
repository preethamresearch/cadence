"""Generate the cadence SigNoz dashboards.

Written as a generator rather than hand-authored JSON: the widget schema is
repetitive, and layout coordinates have to stay consistent with widget ids —
both easy to desynchronise by hand and tedious to debug inside SigNoz.

Schema matches SigNoz's own published dashboards
(github.com/SigNoz/dashboards): histogram percentiles query the ``.bucket``
series with ``spaceAggregation: pNN``; counters use ``timeAggregation: rate``.

Produces two dashboards, because they answer different questions:

``cadence-slo-dashboard.json``
    Are we meeting the Conversation SLO? One row per objective, thresholds
    drawn at the target. This is the one you put on a wall.

``cadence-voice-agent-dashboard.json``
    Why not? Breakdowns by prompt version and transport — the two dimensions
    that actually explain a regression.

    python dashboards/build_dashboard.py
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

NAMESPACE = uuid.UUID("6f1d2c4a-8b3e-4a17-9c55-0d4e2a7b1f90")

# Instrument names — must track cadence.semconv exactly.
TTFA = "realtime.turn.time_to_first_audio"
TURN_DURATION = "realtime.turn.duration"
TURN_COUNT = "realtime.turn.count"
BARGE_IN_COUNT = "realtime.barge_in.count"
BARGE_IN_OFFSET = "realtime.barge_in.offset"
OVERLAP = "realtime.overlap.duration"
REPAIR = "realtime.repair.count"
FALLBACK = "realtime.fallback.count"
HANDOFF = "realtime.handoff.count"
OUTCOME = "realtime.session.outcome.count"
SESSIONS = "realtime.session.count"
SILENCE = "realtime.silence.seconds"
AUDIO_IN = "realtime.audio.input.seconds"
AUDIO_OUT = "realtime.audio.output.seconds"
STREAM_GAP = "realtime.audio.stream.gap"
TOOL_DURATION = "realtime.tool.duration"
TOKENS = "gen_ai.client.token.usage"

PROMPT_VERSION = "realtime.prompt.version"
TRANSPORT = "realtime.transport"
END_REASON = "realtime.turn.end_reason"
OUTCOME_ATTR = "realtime.session.outcome"


def wid(slug: str) -> str:
    return str(uuid.uuid5(NAMESPACE, slug))


def query(
    name: str,
    metric: str,
    *,
    legend: str,
    space: str = "sum",
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
                "id": f"{k}--string--tag--false",
                "isColumn": False,
                "isJSON": False,
                "key": k,
                "type": "tag",
            }
            for k in (group_by or [])
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
                "spaceAggregation": space,
                "reduceTo": "avg",
            }
        ],
        "filter": {"expression": filter_expr},
    }


def pctl(name: str, metric: str, p: str, *, legend: str, group_by=None, filter_expr="") -> dict:
    """Percentile over a histogram: query the .bucket series."""
    return query(
        name, f"{metric}.bucket", legend=legend, space=p, time_agg="",
        group_by=group_by, filter_expr=filter_expr,
    )


def widget(
    slug: str,
    title: str,
    description: str,
    queries: list[dict],
    *,
    panel: str = "graph",
    unit: str = "",
    stacked: bool = False,
    thresholds: list[dict] | None = None,
) -> dict:
    return {
        "description": description,
        "id": wid(slug),
        "isStacked": stacked,
        "nullZeroValues": "zero",
        "opacity": "1",
        "panelTypes": panel,
        "query": {
            "builder": {"queryData": queries, "queryFormulas": []},
            "clickhouse_sql": [{"disabled": False, "legend": "", "name": "A", "query": ""}],
            "id": wid(slug + ":query"),
            "promql": [{"disabled": False, "legend": "", "name": "A", "query": ""}],
            "queryType": "builder",
        },
        "thresholds": thresholds or [],
        "timePreferance": "GLOBAL_TIME",
        "title": title,
        "yAxisUnit": unit,
    }


def threshold(label: str, value: float, colour: str = "#F5B13D") -> dict:
    """A target line drawn on the panel. An SLO chart without its target
    drawn on it makes the reader do the comparison in their head."""
    return {
        "index": str(uuid.uuid5(NAMESPACE, f"th:{label}:{value}")),
        "keyIndex": 0,
        "moveThreshold": False,
        "selectedGraph": "graph",
        "thresholdColor": colour,
        "thresholdFormat": "Text",
        "thresholdLabel": label,
        "thresholdOperator": ">",
        "thresholdTableOptions": "",
        "thresholdUnit": "",
        "thresholdValue": value,
    }


def dashboard(name: str, description: str, tags: list[str], widgets: list[dict]) -> dict:
    layout = [
        {
            "h": 6,
            "i": w["id"],
            "moved": False,
            "static": False,
            "w": 6,
            "x": 0 if i % 2 == 0 else 6,
            "y": (i // 2) * 6,
        }
        for i, w in enumerate(widgets)
    ]
    return {
        "description": description,
        "layout": layout,
        "name": name,
        "tags": tags,
        "title": name,
        "variables": {},
        "version": "v4",
        "widgets": widgets,
    }


# ==========================================================================
# 1. The SLO dashboard
# ==========================================================================

slo_widgets = [
    widget(
        "slo-ttfa",
        "SLO · Time to first audio p95  (target < 350ms)",
        "The silence the user sat through. Turn-taking between humans runs to "
        "roughly 200ms; past 350ms the pause registers as hesitation, and past "
        "a second users assume they were not heard and repeat themselves.",
        [
            pctl("A", TTFA, "p95", legend="p95"),
            pctl("B", TTFA, "p50", legend="p50"),
        ],
        unit="ms",
        thresholds=[threshold("SLO 350ms", 350, "#FB7185")],
    ),
    widget(
        "slo-interruptions",
        "SLO · Interruptions per session  (target < 0.8)",
        "Under one per session is normal turn-taking. Above it, users are "
        "fighting the agent for the floor rather than talking to it.",
        [query("A", BARGE_IN_COUNT, legend="barge-ins")],
        thresholds=[threshold("SLO 0.8", 0.8, "#FB7185")],
    ),
    widget(
        "slo-overlap",
        "SLO · Mean speech overlap  (target < 150ms)",
        "How long the agent keeps talking after the user starts. Beyond ~150ms "
        "the agent is talking over people, which users find ruder than slowness.",
        [
            pctl("A", OVERLAP, "p50", legend="p50"),
            pctl("B", OVERLAP, "p90", legend="p90"),
        ],
        unit="ms",
        thresholds=[threshold("SLO 150ms", 150, "#FB7185")],
    ),
    widget(
        "slo-containment",
        "SLO · Session outcomes  (containment target > 72%)",
        "Share of sessions resolved without a human. The number the deployment "
        "is funded on: below target the agent adds a step rather than removing one.",
        [query("A", OUTCOME, legend="{{" + OUTCOME_ATTR + "}}", group_by=[OUTCOME_ATTR])],
        stacked=True,
    ),
    widget(
        "slo-repair",
        "SLO · Repair turns  (target < 9% of turns)",
        "Turns where the user had to repeat, correct, or ask again — the closest "
        "available proxy for whether the conversation actually worked.",
        [
            query("A", REPAIR, legend="repairs"),
            query("B", TURN_COUNT, legend="turns"),
        ],
    ),
    widget(
        "slo-transfer",
        "SLO · Human transfer  (target < 11%)",
        "Escalations to a human. Distinct from containment: a session can fail "
        "without transferring, if the user simply hangs up.",
        [
            query("A", HANDOFF, legend="handoffs"),
            query("B", SESSIONS, legend="sessions"),
        ],
    ),
]

# ==========================================================================
# 2. The diagnostic dashboard
# ==========================================================================

diag_widgets = [
    widget(
        "diag-ttfa-by-prompt",
        "TTFA p95 by prompt version",
        "The panel that turns 'latency got worse' into 'latency got worse when "
        "v17 shipped'. Prompt changes cause more realtime regressions than "
        "infrastructure does, and without this split the cause is invisible.",
        [pctl("A", TTFA, "p95", legend="{{" + PROMPT_VERSION + "}}",
              group_by=[PROMPT_VERSION])],
        unit="ms",
        thresholds=[threshold("SLO 350ms", 350, "#FB7185")],
    ),
    widget(
        "diag-barge-by-transport",
        "Interruptions by transport",
        "PSTN and WebRTC behave differently: line noise trips voice activity "
        "detection, so an aggregate that mixes channels hides both. If only one "
        "channel regressed, the fix is not in the prompt.",
        [query("A", BARGE_IN_COUNT, legend="{{" + TRANSPORT + "}}", group_by=[TRANSPORT])],
        stacked=True,
    ),
    widget(
        "diag-barge-offset",
        "Barge-in offset distribution",
        "Where interruptions land. Clustering under ~400ms means detection is "
        "firing on noise rather than intent; consistently late means the agent "
        "is rambling. Identical counts, opposite fixes.",
        [
            pctl("A", BARGE_IN_OFFSET, "p50", legend="p50"),
            pctl("B", BARGE_IN_OFFSET, "p90", legend="p90"),
        ],
        unit="ms",
    ),
    widget(
        "diag-turns-by-reason",
        "Turns by end reason",
        "Completed vs interrupted vs abandoned. The interrupted share is the "
        "conversational-quality SLI.",
        [query("A", TURN_COUNT, legend="{{" + END_REASON + "}}", group_by=[END_REASON])],
        stacked=True,
    ),
    widget(
        "diag-stream-gap",
        "Mid-utterance stream gaps p95",
        "Stutter *during* speech, as distinct from a slow start. Listeners find "
        "this worse than latency because it sounds like malfunction rather than "
        "thought.",
        [pctl("A", STREAM_GAP, "p95", legend="p95")],
        unit="ms",
    ),
    widget(
        "diag-tool-latency",
        "Tool latency p95 by prompt version",
        "Tools firing mid-stream are a realtime-only source of confusing latency. "
        "Split by prompt version because prompt changes alter which tools fire.",
        [pctl("A", TOOL_DURATION, "p95", legend="{{" + PROMPT_VERSION + "}}",
              group_by=[PROMPT_VERSION])],
        unit="ms",
    ),
    widget(
        "diag-tokens",
        "Token spend by modality",
        "Realtime sessions bill an open microphone, not a countable prompt. Audio "
        "dominates, and video frames are the usual source of a surprise bill.",
        [query("A", TOKENS, legend="{{gen_ai.token.type}} / {{gen_ai.token.modality}}",
               group_by=["gen_ai.token.type", "gen_ai.token.modality"])],
        stacked=True,
    ),
    widget(
        "diag-audio-seconds",
        "Audio streamed",
        "Seconds in each direction. The input series is the meter that runs "
        "while nobody is talking.",
        [
            query("A", AUDIO_IN, legend="user -> model"),
            query("B", AUDIO_OUT, legend="model -> user"),
        ],
        unit="s",
    ),
    widget(
        "diag-silence",
        "Dead air",
        "Seconds in which neither party was active. A conversation that is 70% "
        "silence is failing however good its p95 looks.",
        [query("A", SILENCE, legend="silence")],
        unit="s",
    ),
    widget(
        "diag-turn-duration",
        "Turn duration p95",
        "Read alongside TTFA: a slow turn with fast first audio is a long answer, "
        "not a slow system.",
        [pctl("A", TURN_DURATION, "p95", legend="p95")],
        unit="ms",
    ),
]


out_dir = Path(__file__).parent
for filename, name, desc, tags, widgets in (
    (
        "cadence-slo-dashboard.json",
        "Cadence — Conversation SLO",
        "The six objectives that define whether a realtime agent is acceptable: "
        "time to first audio, interruptions, overlap, containment, repair rate, "
        "and human transfer. Targets drawn on each panel.",
        ["cadence", "slo", "voice", "realtime"],
        slo_widgets,
    ),
    (
        "cadence-voice-agent-dashboard.json",
        "Cadence — Realtime Agent Diagnostics",
        "Why the SLO moved. Breakdowns by prompt version and transport — the two "
        "dimensions that actually explain a realtime regression.",
        ["cadence", "voice", "realtime", "genai", "opentelemetry"],
        diag_widgets,
    ),
):
    path = out_dir / filename
    path.write_text(json.dumps(dashboard(name, desc, tags, widgets), indent=2) + "\n")
    print(f"wrote {path.name} ({len(widgets)} widgets)")
