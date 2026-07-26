"""SigNoz query client -- how the agent reads its own telemetry.

cadence writes telemetry into SigNoz. This module reads it back out, which is
what lets the demo agent answer questions about its own behaviour: you ask how
fast it has been responding, and it queries the p95 of the very histogram its
own turns populated moments earlier.

Uses the v5 ``/api/v5/query_range`` endpoint. Response parsing is deliberately
tolerant -- the payload nests differently between ``time_series`` and
``scalar`` request types, and a monitoring tool that throws because a series
came back empty is worse than one that says "no data yet".
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# OTel histograms land in SigNoz as a family of series. Which suffix carries
# the buckets has varied across SigNoz versions, so we try the known spellings
# and use whichever actually returns points.
_HISTOGRAM_SUFFIXES = ("", ".bucket")


@dataclass(slots=True)
class SigNozConfig:
    region: str
    api_key: str
    service_name: str = "cadence-voice-agent"

    @property
    def base_url(self) -> str:
        return f"https://{self.region}.signoz.cloud"

    @classmethod
    def from_env(cls) -> SigNozConfig | None:
        region = os.getenv("SIGNOZ_REGION")
        api_key = os.getenv("SIGNOZ_API_KEY")
        if not region or not api_key:
            return None
        return cls(
            region=region,
            api_key=api_key,
            service_name=os.getenv("OTEL_SERVICE_NAME", "cadence-voice-agent"),
        )


class SigNozClient:
    def __init__(self, config: SigNozConfig, timeout: float = 12.0) -> None:
        self.config = config
        self._client = httpx.AsyncClient(
            base_url=config.base_url,
            headers={
                "SIGNOZ-API-KEY": config.api_key,
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------

    async def query_metric(
        self,
        metric_name: str,
        *,
        window_minutes: int = 15,
        time_aggregation: str = "avg",
        space_aggregation: str = "sum",
        group_by: list[str] | None = None,
        filter_expression: str | None = None,
        step_seconds: int = 60,
    ) -> list[dict[str, Any]]:
        """Run one builder query and return flattened series.

        Each returned dict is ``{"labels": {...}, "points": [(ts_ms, value)]}``.
        """
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - window_minutes * 60 * 1000

        spec: dict[str, Any] = {
            "name": "A",
            "signal": "metrics",
            "stepInterval": step_seconds,
            "aggregations": [
                {
                    "metricName": metric_name,
                    "temporality": "Delta",
                    "timeAggregation": time_aggregation,
                    "spaceAggregation": space_aggregation,
                }
            ],
            "disabled": False,
        }
        if group_by:
            spec["groupBy"] = [{"name": key} for key in group_by]
        if filter_expression:
            spec["filter"] = {"expression": filter_expression}

        payload = {
            "start": start_ms,
            "end": end_ms,
            "requestType": "time_series",
            "compositeQuery": {"queries": [{"type": "builder_query", "spec": spec}]},
        }

        try:
            response = await self._client.post("/api/v5/query_range", json=payload)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "signoz query failed for %s: %s %s",
                metric_name, exc.response.status_code, exc.response.text[:300],
            )
            return []
        except httpx.HTTPError as exc:
            logger.warning("signoz query error for %s: %s", metric_name, exc)
            return []

        return _flatten_series(response.json())

    async def query_histogram_percentile(
        self,
        metric_name: str,
        percentile: str = "p95",
        *,
        window_minutes: int = 15,
        group_by: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Percentile over a histogram, trying each known bucket spelling."""
        for suffix in _HISTOGRAM_SUFFIXES:
            series = await self.query_metric(
                metric_name + suffix,
                window_minutes=window_minutes,
                time_aggregation="rate",
                space_aggregation=percentile,
                group_by=group_by,
            )
            if series and any(s["points"] for s in series):
                return series
        return []


# ----------------------------------------------------------------------
# Response flattening
# ----------------------------------------------------------------------


def _flatten_series(body: Any) -> list[dict[str, Any]]:
    """Pull series out of a query_range response.

    The v5 response nests as data -> results[] -> series[] -> values[], but
    intermediate keys differ between request types and versions. Rather than
    hard-code one path, walk for dicts that look like a series.
    """
    out: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            values = node.get("values")
            if isinstance(values, list) and values and isinstance(values[0], (dict, list)):
                points = _extract_points(values)
                if points:
                    out.append({"labels": _extract_labels(node), "points": points})
                    return
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(body)
    return out


def _extract_labels(node: dict[str, Any]) -> dict[str, Any]:
    for key in ("labels", "labelsArray", "metric", "tags"):
        labels = node.get(key)
        if isinstance(labels, dict):
            return labels
        if isinstance(labels, list):
            merged: dict[str, Any] = {}
            for entry in labels:
                if isinstance(entry, dict):
                    merged.update(entry)
            if merged:
                return merged
    return {}


def _extract_points(values: list[Any]) -> list[tuple[int, float]]:
    points: list[tuple[int, float]] = []
    for entry in values:
        ts: Any = None
        val: Any = None
        if isinstance(entry, dict):
            ts = entry.get("timestamp") or entry.get("time") or entry.get("ts")
            val = entry.get("value")
            if val is None:
                val = entry.get("val")
        elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
            ts, val = entry[0], entry[1]
        if ts is None or val is None:
            continue
        try:
            points.append((int(ts), float(val)))
        except (TypeError, ValueError):
            continue
    return points


def latest_value(series: list[dict[str, Any]]) -> float | None:
    """Most recent point across all series, which is what a spoken answer wants."""
    best_ts, best_val = -1, None
    for entry in series:
        for ts, val in entry["points"]:
            if ts > best_ts:
                best_ts, best_val = ts, val
    return best_val


def sum_latest(series: list[dict[str, Any]]) -> float:
    total = 0.0
    for entry in series:
        if entry["points"]:
            total += max(entry["points"], key=lambda p: p[0])[1]
    return total


def total_over_window(series: list[dict[str, Any]]) -> float:
    return sum(val for entry in series for _, val in entry["points"])
