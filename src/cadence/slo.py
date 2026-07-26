"""The Conversation SLO.

Metrics tell you what happened. An SLO tells you whether it was **acceptable**,
and that is the difference between a dashboard someone glances at and a number
someone is accountable for.

This module defines a named, versioned target set for realtime agents — the
conversational equivalent of "99.9% of requests under 300ms". Six indicators,
each with a threshold, a rationale, and an error budget:

======================  =========  ==============================================
TTFA p95                < 350 ms   Past this the pause is audible as hesitation
Interruptions/session   < 0.8      Above this users are routinely fighting to talk
Mean overlap            < 150 ms   Above this the agent is talking over people
Containment             > 72%      Below this the agent is not deflecting load
Repair turns            < 9%       Above this users keep having to repeat themselves
Human transfer          < 11%      The commercial ceiling on escalation
======================  =========  ==============================================

Why an *error budget* rather than a pass/fail threshold: a single slow turn is
not an incident, and treating it as one trains people to ignore alerts. The
budget makes the trade explicit — you may burn 5% of turns above 350ms before
anyone needs to care — and burn *rate* is what should page, not any single
breach.

The thresholds are defaults, not laws. A drive-through agent and a medical
triage agent have different tolerances, and ``ConversationSLO.custom()`` exists
for exactly that. What is worth standardising is the *shape*: these six
indicators, measured this way, so numbers from different teams mean the same
thing.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Iterable

SLO_VERSION = "0.1.0"


class Direction(Enum):
    LOWER_IS_BETTER = "lower_is_better"
    HIGHER_IS_BETTER = "higher_is_better"


class Status(Enum):
    HEALTHY = "healthy"
    AT_RISK = "at_risk"
    BREACHED = "breached"
    NO_DATA = "no_data"


@dataclass(frozen=True, slots=True)
class Objective:
    """One service level objective."""

    key: str
    label: str
    metric: str
    """The cadence instrument this is computed from."""

    target: float
    direction: Direction
    unit: str
    rationale: str
    """Why this number and not another. An SLO nobody can justify gets
    negotiated away the first time it is inconvenient."""

    budget: float = 0.05
    """Fraction of measurements permitted to violate the target before the
    objective is considered breached."""

    def evaluate(self, value: float | None) -> ObjectiveResult:
        if value is None:
            return ObjectiveResult(self, None, Status.NO_DATA, 0.0)

        if self.direction is Direction.LOWER_IS_BETTER:
            ratio = value / self.target if self.target else float("inf")
        else:
            ratio = self.target / value if value else float("inf")

        # ratio <= 1 means the target is met. How far past it decides whether
        # this is "watch it" or "it is broken".
        if ratio <= 1.0:
            status = Status.HEALTHY
        elif ratio <= 1.0 + self.budget * 2:
            status = Status.AT_RISK
        else:
            status = Status.BREACHED

        # Budget consumed, expressed as a fraction: 0 while healthy, 1.0 at
        # the point the objective is fully breached.
        consumed = max(0.0, min(1.0, (ratio - 1.0) / (self.budget * 4))) if ratio > 1 else 0.0
        return ObjectiveResult(self, value, status, consumed)


@dataclass(frozen=True, slots=True)
class ObjectiveResult:
    objective: Objective
    value: float | None
    status: Status
    budget_consumed: float

    @property
    def summary(self) -> str:
        if self.value is None:
            return f"{self.objective.label}: no data"
        comparator = "<" if self.objective.direction is Direction.LOWER_IS_BETTER else ">"
        return (
            f"{self.objective.label}: {self.value:.4g}{self.objective.unit} "
            f"(target {comparator} {self.objective.target:g}{self.objective.unit}) "
            f"— {self.status.value}"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.objective.key,
            "label": self.objective.label,
            "value": self.value,
            "target": self.objective.target,
            "unit": self.objective.unit,
            "direction": self.objective.direction.value,
            "status": self.status.value,
            "budget_consumed": round(self.budget_consumed, 3),
            "rationale": self.objective.rationale,
        }


# --------------------------------------------------------------------------
# The default objective set
# --------------------------------------------------------------------------

DEFAULT_OBJECTIVES: tuple[Objective, ...] = (
    Objective(
        key="ttfa_p95",
        label="Time to first audio (p95)",
        metric="realtime.turn.time_to_first_audio",
        target=350.0,
        direction=Direction.LOWER_IS_BETTER,
        unit="ms",
        rationale=(
            "Turn-taking research puts the natural gap between human speakers at "
            "roughly 200ms. Past about 350ms a listener registers hesitation; past "
            "a second they assume they were not heard and start repeating "
            "themselves — which then shows up as a repair, not a latency alert."
        ),
    ),
    Objective(
        key="interruptions_per_session",
        label="Interruptions per session",
        metric="realtime.barge_in.count",
        target=0.8,
        direction=Direction.LOWER_IS_BETTER,
        unit="",
        rationale=(
            "Under one interruption per session is normal conversational "
            "turn-taking. Consistently above it means users are fighting the agent "
            "for the floor rather than talking to it."
        ),
    ),
    Objective(
        key="mean_overlap",
        label="Mean speech overlap",
        metric="realtime.overlap.duration",
        target=150.0,
        direction=Direction.LOWER_IS_BETTER,
        unit="ms",
        rationale=(
            "How long the agent keeps talking after the user starts. Under ~150ms "
            "reads as a natural handover; beyond it the agent is talking over "
            "people, which users find markedly ruder than being slow to answer."
        ),
    ),
    Objective(
        key="containment",
        label="Containment rate",
        metric="realtime.session.outcome.count",
        target=72.0,
        direction=Direction.HIGHER_IS_BETTER,
        unit="%",
        rationale=(
            "Share of sessions resolved without a human. The number the deployment "
            "is funded on: below it the agent is adding a step rather than removing "
            "one."
        ),
    ),
    Objective(
        key="repair_rate",
        label="Repair turns",
        metric="realtime.repair.count",
        target=9.0,
        direction=Direction.LOWER_IS_BETTER,
        unit="%",
        rationale=(
            "Turns where the user had to repeat, correct, or ask again. The closest "
            "available proxy for whether the conversation actually worked, as "
            "opposed to whether it was fast."
        ),
    ),
    Objective(
        key="transfer_rate",
        label="Human transfer",
        metric="realtime.handoff.count",
        target=11.0,
        direction=Direction.LOWER_IS_BETTER,
        unit="%",
        rationale=(
            "Escalations to a human. Distinct from containment: a session can fail "
            "without transferring, if the user simply hangs up."
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class ConversationSLO:
    """A named, versioned set of objectives."""

    name: str = "Conversation SLO"
    version: str = SLO_VERSION
    objectives: tuple[Objective, ...] = DEFAULT_OBJECTIVES

    @classmethod
    def custom(cls, name: str, **overrides: float) -> ConversationSLO:
        """Clone the default set with different targets.

            ConversationSLO.custom("Drive-through", ttfa_p95=250, repair_rate=6)
        """
        adjusted = tuple(
            replace(o, target=overrides[o.key]) if o.key in overrides else o
            for o in DEFAULT_OBJECTIVES
        )
        unknown = set(overrides) - {o.key for o in DEFAULT_OBJECTIVES}
        if unknown:
            raise ValueError(f"unknown objective(s): {', '.join(sorted(unknown))}")
        return cls(name=name, objectives=adjusted)

    def evaluate(self, measurements: dict[str, float | None]) -> SLOReport:
        """Score a set of measurements keyed by objective key."""
        results = [o.evaluate(measurements.get(o.key)) for o in self.objectives]
        return SLOReport(slo=self, results=tuple(results))


@dataclass(frozen=True, slots=True)
class SLOReport:
    slo: ConversationSLO
    results: tuple[ObjectiveResult, ...]

    @property
    def status(self) -> Status:
        """Worst status across objectives — an SLO set is only as good as its
        weakest indicator."""
        if any(r.status is Status.BREACHED for r in self.results):
            return Status.BREACHED
        if any(r.status is Status.AT_RISK for r in self.results):
            return Status.AT_RISK
        if all(r.status is Status.NO_DATA for r in self.results):
            return Status.NO_DATA
        return Status.HEALTHY

    @property
    def breached(self) -> list[ObjectiveResult]:
        return [r for r in self.results if r.status is Status.BREACHED]

    @property
    def compliance(self) -> float:
        """Fraction of objectives currently met, ignoring those with no data."""
        scored = [r for r in self.results if r.status is not Status.NO_DATA]
        if not scored:
            return 0.0
        met = sum(1 for r in scored if r.status is Status.HEALTHY)
        return round(met / len(scored), 3)

    def headline(self) -> str:
        """One sentence a human can act on."""
        if self.status is Status.NO_DATA:
            return "No conversation data yet."
        if self.status is Status.HEALTHY:
            return f"All {len(self.results)} conversation objectives met."
        worst = sorted(
            (r for r in self.results if r.status is not Status.NO_DATA),
            key=lambda r: r.budget_consumed,
            reverse=True,
        )[0]
        return f"{worst.objective.label} is {worst.status.value}: {worst.summary}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.slo.name,
            "version": self.slo.version,
            "status": self.status.value,
            "compliance": self.compliance,
            "headline": self.headline(),
            "objectives": [r.as_dict() for r in self.results],
        }


DEFAULT_SLO = ConversationSLO()


def derive_measurements(
    *,
    ttfa_p95_ms: float | None = None,
    barge_ins: float | None = None,
    sessions: float | None = None,
    mean_overlap_ms: float | None = None,
    contained_sessions: float | None = None,
    repair_turns: float | None = None,
    total_turns: float | None = None,
    transfers: float | None = None,
) -> dict[str, float | None]:
    """Turn raw counters into the ratios the objectives are defined against.

    Kept separate from evaluation so the same arithmetic serves both the live
    in-process path and the SigNoz query path, rather than being implemented
    twice and drifting.
    """

    def ratio(numerator: float | None, denominator: float | None, scale: float = 1.0):
        if numerator is None or not denominator:
            return None
        return (numerator / denominator) * scale

    return {
        "ttfa_p95": ttfa_p95_ms,
        "interruptions_per_session": ratio(barge_ins, sessions),
        "mean_overlap": mean_overlap_ms,
        "containment": ratio(contained_sessions, sessions, 100.0),
        "repair_rate": ratio(repair_turns, total_turns, 100.0),
        "transfer_rate": ratio(transfers, sessions, 100.0),
    }


def objective_by_key(key: str) -> Objective | None:
    return next((o for o in DEFAULT_OBJECTIVES if o.key == key), None)


def iter_objectives() -> Iterable[Objective]:
    return DEFAULT_OBJECTIVES
