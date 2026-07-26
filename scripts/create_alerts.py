"""Provision the Conversation SLO alerts in SigNoz, via its MCP server.

The alerts live here rather than in someone's browser history because an alert
threshold is a production decision: it should be reviewable, diffable, and
reproducible on a fresh instance, sitting next to the reasoning for it. Each
rule carries its objective's rationale in the annotations, so whoever gets
paged at 3am can see *why* the threshold is where it is without going hunting.

Why MCP rather than the REST API
--------------------------------
Both were tried. ``POST /api/v1/rules`` rejects a malformed payload with a flat
``"alert rule is not valid"`` and no indication of which field is wrong, which
turns reverse-engineering the v2alpha1 schema into a guessing game. The MCP
server validates the same payload and returns specifics:

    condition.compositeQuery.queryType: is required (builder, promql, or clickhouse_sql)
    condition.thresholds: is required (v2alpha1 schema); use condition.thresholds
                          with kind and spec array

That is the difference between ten minutes and an afternoon, and it is a fair
argument for the MCP server being the better provisioning surface even for
automation that has nothing to do with AI.

Two non-obvious requirements, both learned the hard way:

* Notification channels attach **per threshold**
  (``condition.thresholds.spec[].channels``), not at the top level. A rule with
  only ``preferredChannels`` set is rejected with "at least one channel is
  required".
* Threshold rules use schema v2alpha1, where the threshold block is
  ``{kind, spec[]}`` rather than the flat ``op``/``target`` pair the older docs
  show.

    export SIGNOZ_API_KEY=...
    python scripts/create_alerts.py
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from cadence import slo  # noqa: E402
from mcp_client import MCPClient, MCPError, text_of  # noqa: E402

CHANNEL_NAME = "cadence-local-webhook"


def threshold_rule(
    name: str,
    metric: str,
    target: float,
    severity: str,
    description: str,
    *,
    channels: list[str],
    space_aggregation: str = "p95",
    unit: str = "ms",
    group_by: list[str] | None = None,
) -> dict:
    spec: dict = {
        "name": "A",
        "signal": "metrics",
        "stepInterval": 60,
        "disabled": False,
        # An empty timeAggregation is correct for histogram percentiles: the
        # percentile *is* the aggregation.
        "aggregations": [
            {
                "metricName": metric,
                "timeAggregation": "",
                "spaceAggregation": space_aggregation,
            }
        ],
    }
    if group_by:
        spec["groupBy"] = [{"name": key} for key in group_by]

    return {
        "alert": name,
        "alertType": "METRIC_BASED_ALERT",
        "ruleType": "threshold_rule",
        "description": description,
        "preferredChannels": channels,
        "notificationSettings": {"usePolicy": False},
        "condition": {
            "compositeQuery": {
                "queryType": "builder",
                "panelType": "graph",
                "queries": [{"type": "builder_query", "spec": spec}],
            },
            "selectedQueryName": "A",
            "thresholds": {
                "kind": "basic",
                "spec": [
                    {
                        "name": severity,
                        "target": target,
                        "matchType": "1",   # fire if breached at least once in the window
                        "op": "1",          # above
                        "targetUnit": unit,
                        "channels": channels,
                    }
                ],
            },
        },
        "labels": {"severity": severity, "source": "cadence", "slo": "conversation"},
        "annotations": {
            "summary": name,
            "description": f"{description} Current value: {{{{$value}}}}.",
        },
    }


def build_rules(channels: list[str]) -> list[dict]:
    ttfa = slo.objective_by_key("ttfa_p95")
    overlap = slo.objective_by_key("mean_overlap")

    return [
        threshold_rule(
            "Conversation SLO · TTFA p95 above 350ms",
            "realtime.turn.time_to_first_audio.bucket",
            ttfa.target,
            "critical",
            ttfa.rationale
            + " Grouped by prompt version so the responsible deploy is visible "
            "in the alert itself.",
            group_by=["realtime.prompt.version"],
            channels=channels,
        ),
        threshold_rule(
            "Conversation SLO · speech overlap above 150ms",
            "realtime.overlap.duration.bucket",
            overlap.target,
            "warning",
            overlap.rationale,
            space_aggregation="p90",
            channels=channels,
        ),
        threshold_rule(
            "Realtime · mid-utterance stream gap above 1s",
            "realtime.audio.stream.gap.bucket",
            1000.0,
            "warning",
            "Stutter during speech, as distinct from a slow start. Listeners read "
            "this as malfunction rather than thought, so it warrants paging "
            "separately from TTFA.",
            channels=channels,
        ),
    ]


def ensure_channel(client: MCPClient, name: str) -> bool:
    """Create the local webhook sink if it is missing.

    Points at localhost on purpose: alerts fire and are recorded, and nothing
    leaves the machine. Swap the URL for Slack or PagerDuty in a deployment
    that should actually page someone.
    """
    try:
        existing = text_of(client.call("signoz_list_notification_channels", {}))
    except MCPError:
        existing = ""
    if name in existing:
        print(f"  · channel already present: {name}")
        return True

    result = client.call(
        "signoz_create_notification_channel",
        {
            "type": "webhook",
            "name": name,
            "webhook_url": "http://localhost:9099/alerts",
            "send_resolved": True,
            "searchContext": "Local webhook sink for cadence Conversation SLO alerts.",
        },
    )
    if result.get("isError"):
        print(f"  ✗ channel: {text_of(result)[:200]}")
        return False
    print(f"  ✓ channel created: {name}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mcp-url", default=os.getenv("SIGNOZ_MCP_URL", "http://localhost:8000/mcp")
    )
    parser.add_argument("--api-key", default=os.getenv("SIGNOZ_API_KEY"))
    parser.add_argument("--channel", default=CHANNEL_NAME)
    args = parser.parse_args()

    if not args.api_key:
        print("error: --api-key or SIGNOZ_API_KEY required", file=sys.stderr)
        return 2

    client = MCPClient(args.mcp_url, args.api_key)
    try:
        info = client.initialize()
    except MCPError as exc:
        print(f"error: cannot reach SigNoz MCP at {args.mcp_url}\n  {exc}", file=sys.stderr)
        return 1

    server = (info.get("serverInfo") or {}).get("name", "unknown")
    print(f"connected to {server} at {args.mcp_url}")

    if not ensure_channel(client, args.channel):
        return 1

    rules = build_rules([args.channel])
    failures = 0
    for rule in rules:
        result = client.call("signoz_create_alert", rule)
        if result.get("isError"):
            failures += 1
            print(f"  ✗ {rule['alert']}\n      {text_of(result)[:300]}")
        else:
            print(f"  ✓ {rule['alert']}")

    print(f"\n{len(rules) - failures}/{len(rules)} alert(s) provisioned")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
