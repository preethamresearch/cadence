"""Create the Conversation SLO alerts in SigNoz.

The alerts are defined here rather than clicked into the UI so they are
reviewable, diffable, and reproducible on a fresh instance — an alert
threshold is a production decision and deserves to live in version control
next to the reasoning for it.

Each rule maps to one objective from ``cadence.slo``, and carries that
objective's rationale in its annotations so whoever gets paged at 3am sees
*why* the threshold is where it is without going hunting.

    python scripts/create_alerts.py --api-key "$SIGNOZ_API_KEY"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cadence import slo  # noqa: E402


def builder_query(
    metric: str,
    *,
    space_aggregation: str = "p95",
    time_aggregation: str = "",
    group_by: list[str] | None = None,
) -> dict:
    return {
        "queryType": "builder",
        "builder": {
            "queryData": [
                {
                    "dataSource": "metrics",
                    "queryName": "A",
                    "expression": "A",
                    "disabled": False,
                    "stepInterval": 60,
                    "aggregations": [
                        {
                            "metricName": metric,
                            "temporality": "",
                            "timeAggregation": time_aggregation,
                            "spaceAggregation": space_aggregation,
                        }
                    ],
                    "filter": {"expression": ""},
                    "groupBy": [
                        {
                            "key": key,
                            "dataType": "string",
                            "type": "tag",
                            "isColumn": False,
                        }
                        for key in (group_by or [])
                    ],
                    "having": {"expression": ""},
                    "legend": "",
                    "limit": None,
                    "orderBy": [],
                    "functions": [],
                    "reduceTo": "avg",
                }
            ],
            "queryFormulas": [],
        },
        "promql": [],
        "clickhouse_sql": [],
    }


def rule(
    name: str,
    description: str,
    metric: str,
    threshold: float,
    *,
    op: str = "1",        # SigNoz expects the comparator as a string: "1" = above
    unit: str = "ms",
    severity: str = "warning",
    space_aggregation: str = "p95",
    time_aggregation: str = "",
    group_by: list[str] | None = None,
) -> dict:
    return {
        "alert": name,
        "alertType": "METRIC_BASED_ALERT",
        "ruleType": "threshold_rule",
        "frequency": "1m",
        "evalWindow": "5m",
        "condition": {
            "compositeQuery": {
                **builder_query(
                    metric,
                    space_aggregation=space_aggregation,
                    time_aggregation=time_aggregation,
                    group_by=group_by,
                ),
                "panelType": "graph",
                "unit": unit,
            },
            "op": op,
            "target": threshold,
            "matchType": "1",          # at least once in the window
            "targetUnit": unit,
        },
        "labels": {"severity": severity, "source": "cadence", "slo": "conversation"},
        "annotations": {
            "summary": name,
            "description": description,
        },
        "disabled": False,
    }


def post(base: str, key: str, payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"{base}/api/v1/rules",
        data=json.dumps(payload).encode(),
        headers={"SIGNOZ-API-KEY": key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            body = response.read().decode()
            return response.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"raw": body[:500]}


def build_rules() -> list[dict]:
    ttfa = slo.objective_by_key("ttfa_p95")
    overlap = slo.objective_by_key("mean_overlap")

    return [
        rule(
            "Conversation SLO · TTFA p95 above 350ms",
            # The rationale travels with the alert on purpose.
            f"{ttfa.rationale} Grouped by prompt version so the deploy "
            f"responsible is visible in the alert itself.",
            "realtime.turn.time_to_first_audio.bucket",
            ttfa.target,
            severity="critical",
            group_by=["realtime.prompt.version"],
        ),
        rule(
            "Conversation SLO · speech overlap above 150ms",
            overlap.rationale,
            "realtime.overlap.duration.bucket",
            overlap.target,
            space_aggregation="p90",
            severity="warning",
        ),
        rule(
            "Realtime · mid-utterance stream gap above 1s",
            "Stutter during speech, as distinct from a slow start. Listeners "
            "read this as malfunction rather than thought, so it is worth "
            "paging on separately from TTFA.",
            "realtime.audio.stream.gap.bucket",
            1000.0,
            severity="warning",
        ),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=os.getenv("SIGNOZ_UI", "http://localhost:8080"))
    parser.add_argument("--api-key", default=os.getenv("SIGNOZ_API_KEY"))
    args = parser.parse_args()

    if not args.api_key:
        print("error: --api-key or SIGNOZ_API_KEY required", file=sys.stderr)
        return 2

    failures = 0
    for payload in build_rules():
        status, body = post(args.base, args.api_key, payload)
        name = payload["alert"]
        if status in (200, 201):
            print(f"  ✓ created: {name}")
        else:
            failures += 1
            err = body.get("error") if isinstance(body, dict) else body
            message = err.get("message") if isinstance(err, dict) else (err or body)
            print(f"  ✗ failed ({status}): {name}\n      {str(message)[:400]}")

    if not failures:
        print(f"\n{args.base}/alerts")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
