"""Regression detection and root-cause attribution.

An alert that says *"TTFA p95 crossed 350ms"* tells an engineer something they
could have seen on the chart. The useful alert says *"TTFA p95 crossed 350ms,
and it is entirely prompt v17 — v16 traffic is unchanged."* That second
sentence is the difference between a page and a fix.

This module does that attribution. It is deliberately **backend-agnostic**: it
operates on plain series data, so the same logic works against SigNoz, or any
other OTLP store, or a test fixture. The SigNoz query wiring lives in the
application layer.

The method is intentionally simple and explainable:

1. Split each metric into a **baseline** window and a **current** window.
2. For every candidate dimension (prompt version, transport, model…), compute
   the change within each of its values.
3. A dimension **explains** the regression when the change is concentrated in
   a subset of its values while the others stayed flat. If every value moved
   together, that dimension is not the cause — something upstream of it is.

No statistical modelling, deliberately. With hackathon-to-medium traffic
volumes a t-test on a handful of buckets projects false confidence, and an
operator at 3am needs a claim they can verify on the chart in ten seconds, not
a p-value they have to take on faith. What is reported instead is effect size
and concentration, both of which are directly checkable.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Sequence


class Verdict(Enum):
    NO_CHANGE = "no_change"
    IMPROVED = "improved"
    REGRESSED = "regressed"
    INSUFFICIENT_DATA = "insufficient_data"


@dataclass(frozen=True, slots=True)
class Series:
    """One labelled time series."""

    labels: dict[str, str]
    points: Sequence[tuple[int, float]]  # (timestamp_ms, value)

    def mean_between(self, start_ms: int, end_ms: int) -> float | None:
        vals = [v for ts, v in self.points if start_ms <= ts < end_ms]
        return sum(vals) / len(vals) if vals else None

    def label(self, key: str) -> str:
        return self.labels.get(key, "unknown")


@dataclass(frozen=True, slots=True)
class SegmentChange:
    """How one value of one dimension moved."""

    dimension: str
    value: str
    baseline: float | None
    current: float | None

    @property
    def delta(self) -> float | None:
        if self.baseline is None or self.current is None:
            return None
        return self.current - self.baseline

    @property
    def pct_change(self) -> float | None:
        if self.baseline in (None, 0) or self.current is None:
            return None
        return (self.current - self.baseline) / self.baseline * 100.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "value": self.value,
            "baseline": round(self.baseline, 2) if self.baseline is not None else None,
            "current": round(self.current, 2) if self.current is not None else None,
            "pct_change": round(self.pct_change, 1) if self.pct_change is not None else None,
        }


@dataclass(frozen=True, slots=True)
class Attribution:
    """A candidate explanation for a change."""

    dimension: str
    culprits: tuple[SegmentChange, ...]
    unaffected: tuple[SegmentChange, ...]
    concentration: float
    """0–1. How much of the movement sits in the culprit segments. Near 1.0
    means this dimension explains it; near 0.5 means everything moved together
    and the cause is elsewhere."""

    def explain(self, metric_label: str = "the metric") -> str:
        if not self.culprits:
            return f"No single {self.dimension} explains {metric_label}."
        worst = self.culprits[0]
        pct = worst.pct_change
        movement = f"{pct:+.0f}%" if pct is not None else "changed"
        sentence = (
            f"{metric_label} {movement} for {self.dimension}={worst.value}"
        )
        if self.unaffected:
            flat = ", ".join(s.value for s in self.unaffected[:3])
            sentence += f", while {self.dimension}={flat} stayed flat"
        return sentence + "."

    def as_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "concentration": round(self.concentration, 3),
            "culprits": [c.as_dict() for c in self.culprits],
            "unaffected": [c.as_dict() for c in self.unaffected],
            "explanation": self.explain(),
        }


@dataclass(frozen=True, slots=True)
class RegressionReport:
    metric: str
    verdict: Verdict
    baseline: float | None
    current: float | None
    attributions: tuple[Attribution, ...]

    @property
    def pct_change(self) -> float | None:
        if self.baseline in (None, 0) or self.current is None:
            return None
        return (self.current - self.baseline) / self.baseline * 100.0

    @property
    def best_attribution(self) -> Attribution | None:
        return self.attributions[0] if self.attributions else None

    def headline(self) -> str:
        """One sentence an on-call engineer can act on."""
        if self.verdict is Verdict.INSUFFICIENT_DATA:
            return f"Not enough data to judge {self.metric}."
        if self.verdict is Verdict.NO_CHANGE:
            return f"{self.metric} is unchanged."

        direction = "regressed" if self.verdict is Verdict.REGRESSED else "improved"
        pct = self.pct_change
        change = f"{pct:+.0f}%" if pct is not None else ""
        head = (
            f"{self.metric} {direction} {change} "
            f"({self.baseline:.0f} -> {self.current:.0f})."
        )
        best = self.best_attribution
        # Only volunteer a cause when the movement is genuinely concentrated.
        # A confident wrong attribution is worse than none, because people act
        # on it.
        if best and best.concentration >= 0.7:
            head += " " + best.explain(self.metric)
        elif best:
            head += (
                f" No single {best.dimension} explains it — the change is spread "
                "across all segments, so look upstream."
            )
        return head

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "verdict": self.verdict.value,
            "baseline": round(self.baseline, 2) if self.baseline is not None else None,
            "current": round(self.current, 2) if self.current is not None else None,
            "pct_change": round(self.pct_change, 1) if self.pct_change is not None else None,
            "headline": self.headline(),
            "attributions": [a.as_dict() for a in self.attributions],
        }


# --------------------------------------------------------------------------

MIN_RELATIVE_CHANGE = 0.10
"""Ignore movement under 10%. Realtime metrics are noisy and alerting on
every wobble is how teams learn to ignore alerts."""


def detect_regression(
    metric: str,
    series: Sequence[Series],
    *,
    baseline_start_ms: int,
    baseline_end_ms: int,
    current_start_ms: int,
    current_end_ms: int,
    dimensions: Sequence[str] = (),
    lower_is_better: bool = True,
    min_relative_change: float = MIN_RELATIVE_CHANGE,
) -> RegressionReport:
    """Compare two windows and attribute any change to a dimension."""

    def window_mean(subset: Sequence[Series], start: int, end: int) -> float | None:
        vals = [m for s in subset if (m := s.mean_between(start, end)) is not None]
        return sum(vals) / len(vals) if vals else None

    overall_baseline = window_mean(series, baseline_start_ms, baseline_end_ms)
    overall_current = window_mean(series, current_start_ms, current_end_ms)

    if overall_baseline is None or overall_current is None:
        return RegressionReport(metric, Verdict.INSUFFICIENT_DATA, overall_baseline,
                                overall_current, ())

    relative = (
        (overall_current - overall_baseline) / overall_baseline
        if overall_baseline
        else 0.0
    )
    if abs(relative) < min_relative_change:
        verdict = Verdict.NO_CHANGE
    elif (relative > 0) == lower_is_better:
        verdict = Verdict.REGRESSED
    else:
        verdict = Verdict.IMPROVED

    attributions = []
    for dimension in dimensions:
        attribution = _attribute(
            series, dimension,
            baseline_start_ms, baseline_end_ms,
            current_start_ms, current_end_ms,
            lower_is_better, min_relative_change,
        )
        if attribution is not None:
            attributions.append(attribution)

    attributions.sort(key=lambda a: a.concentration, reverse=True)
    return RegressionReport(
        metric, verdict, overall_baseline, overall_current, tuple(attributions)
    )


def _attribute(
    series: Sequence[Series],
    dimension: str,
    b_start: int,
    b_end: int,
    c_start: int,
    c_end: int,
    lower_is_better: bool,
    min_relative_change: float,
) -> Attribution | None:
    """Group by one dimension and see whether the change concentrates."""
    groups: dict[str, list[Series]] = {}
    for s in series:
        if dimension in s.labels:
            groups.setdefault(s.label(dimension), []).append(s)

    # A dimension with one value cannot discriminate anything.
    if len(groups) < 2:
        return None

    changes: list[SegmentChange] = []
    for value, subset in groups.items():
        b_vals = [m for s in subset if (m := s.mean_between(b_start, b_end)) is not None]
        c_vals = [m for s in subset if (m := s.mean_between(c_start, c_end)) is not None]
        changes.append(
            SegmentChange(
                dimension=dimension,
                value=value,
                baseline=sum(b_vals) / len(b_vals) if b_vals else None,
                current=sum(c_vals) / len(c_vals) if c_vals else None,
            )
        )

    def moved_badly(change: SegmentChange) -> bool:
        pct = change.pct_change
        if pct is None:
            return False
        return (pct / 100.0 > min_relative_change) == lower_is_better and abs(
            pct / 100.0
        ) > min_relative_change

    culprits = sorted(
        (c for c in changes if moved_badly(c)),
        key=lambda c: abs(c.pct_change or 0),
        reverse=True,
    )
    unaffected = [c for c in changes if c not in culprits and c.pct_change is not None]

    if not culprits:
        return None

    # Concentration: how much of the total absolute movement sits in the
    # culprits. If every segment moved by the same amount this lands near
    # len(culprits)/len(changes) and the dimension explains nothing.
    total_movement = sum(abs(c.delta or 0) for c in changes)
    culprit_movement = sum(abs(c.delta or 0) for c in culprits)
    concentration = culprit_movement / total_movement if total_movement else 0.0

    # Penalise attributions where the culprits are simply *most* of the
    # segments — "everything got worse" is not an attribution.
    breadth_penalty = len(culprits) / len(changes)
    concentration *= 1.0 - (breadth_penalty * 0.5)

    return Attribution(
        dimension=dimension,
        culprits=tuple(culprits),
        unaffected=tuple(unaffected),
        concentration=max(0.0, min(1.0, concentration)),
    )


def compare_segments(
    metric: str,
    series: Sequence[Series],
    dimension: str,
    *,
    start_ms: int,
    end_ms: int,
) -> list[SegmentChange]:
    """Side-by-side values for each segment over one window.

    Used for deployment comparison: what does TTFA look like on v16 versus v17
    *right now*, without reference to a baseline.
    """
    groups: dict[str, list[Series]] = {}
    for s in series:
        if dimension in s.labels:
            groups.setdefault(s.label(dimension), []).append(s)

    out = []
    for value, subset in groups.items():
        vals = [m for s in subset if (m := s.mean_between(start_ms, end_ms)) is not None]
        out.append(
            SegmentChange(
                dimension=dimension,
                value=value,
                baseline=None,
                current=sum(vals) / len(vals) if vals else None,
            )
        )
    return sorted(out, key=lambda c: c.current if c.current is not None else -1, reverse=True)
