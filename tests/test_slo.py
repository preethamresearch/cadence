"""Tests for the Conversation SLO and regression attribution.

The SLO is a public contract — people write alerts against these thresholds —
so the arithmetic is worth pinning down, particularly the direction handling,
which is easy to invert for "higher is better" objectives.
"""

from __future__ import annotations

import pytest

from cadence.analysis import Series, Verdict, compare_segments, detect_regression
from cadence.slo import (
    DEFAULT_SLO,
    ConversationSLO,
    Status,
    derive_measurements,
    objective_by_key,
)


# ---------------------------------------------------------------- objectives


def test_healthy_measurements_pass_every_objective():
    report = DEFAULT_SLO.evaluate(
        {
            "ttfa_p95": 300.0,
            "interruptions_per_session": 0.5,
            "mean_overlap": 120.0,
            "containment": 80.0,
            "repair_rate": 5.0,
            "transfer_rate": 8.0,
        }
    )
    assert report.status is Status.HEALTHY
    assert report.compliance == 1.0
    assert "All 6 conversation objectives met" in report.headline()


def test_lower_is_better_breach_is_detected():
    report = DEFAULT_SLO.evaluate({"ttfa_p95": 900.0})
    result = next(r for r in report.results if r.objective.key == "ttfa_p95")
    assert result.status is Status.BREACHED
    assert report.status is Status.BREACHED


def test_higher_is_better_direction_is_not_inverted():
    """Containment is the one objective where a *bigger* number is good.
    Getting this backwards would report a healthy agent as broken."""
    good = DEFAULT_SLO.evaluate({"containment": 90.0})
    bad = DEFAULT_SLO.evaluate({"containment": 40.0})

    assert next(r for r in good.results if r.objective.key == "containment").status is Status.HEALTHY
    assert next(r for r in bad.results if r.objective.key == "containment").status is Status.BREACHED


def test_missing_measurement_is_no_data_not_a_pass():
    """An objective with no data must never be reported as met — that would
    make an unmonitored system look healthy."""
    report = DEFAULT_SLO.evaluate({})
    assert report.status is Status.NO_DATA
    assert all(r.status is Status.NO_DATA for r in report.results)
    assert report.compliance == 0.0


def test_marginal_breach_is_at_risk_not_breached():
    """A hair over target should not page anyone."""
    objective = objective_by_key("ttfa_p95")
    result = objective.evaluate(objective.target * 1.05)
    assert result.status is Status.AT_RISK


def test_custom_targets_override_defaults():
    slo = ConversationSLO.custom("Drive-through", ttfa_p95=250.0, repair_rate=6.0)
    assert objective_by_key("ttfa_p95").target == 350.0  # default untouched
    assert next(o for o in slo.objectives if o.key == "ttfa_p95").target == 250.0

    report = slo.evaluate({"ttfa_p95": 300.0})
    assert next(r for r in report.results if r.objective.key == "ttfa_p95").status is not Status.HEALTHY


def test_custom_rejects_unknown_objective():
    with pytest.raises(ValueError, match="unknown objective"):
        ConversationSLO.custom("Nonsense", made_up_thing=1.0)


def test_derive_measurements_computes_ratios():
    m = derive_measurements(
        ttfa_p95_ms=310.0,
        barge_ins=40, sessions=100,
        mean_overlap_ms=130.0,
        contained_sessions=75,
        repair_turns=30, total_turns=500,
        transfers=9,
    )
    assert m["interruptions_per_session"] == pytest.approx(0.4)
    assert m["containment"] == pytest.approx(75.0)
    assert m["repair_rate"] == pytest.approx(6.0)
    assert m["transfer_rate"] == pytest.approx(9.0)


def test_derive_measurements_handles_zero_denominator():
    """No sessions yet must yield None, not a ZeroDivisionError and not 0%
    containment — which would read as a total failure."""
    m = derive_measurements(barge_ins=0, sessions=0, contained_sessions=0)
    assert m["interruptions_per_session"] is None
    assert m["containment"] is None


# ---------------------------------------------------------------- regression


def series(labels: dict[str, str], baseline: float, current: float) -> Series:
    """Two flat windows: 0–1000ms baseline, 2000–3000ms current."""
    return Series(
        labels=labels,
        points=[(0, baseline), (500, baseline), (2000, current), (2500, current)],
    )


WINDOWS = dict(
    baseline_start_ms=0, baseline_end_ms=1000,
    current_start_ms=2000, current_end_ms=3000,
)


def test_regression_attributed_to_the_prompt_version_that_moved():
    """The headline claim of the intelligence layer: not just that TTFA rose,
    but which deploy did it."""
    data = [
        series({"realtime.prompt.version": "v16"}, 300.0, 305.0),   # flat
        series({"realtime.prompt.version": "v17"}, 300.0, 600.0),   # doubled
    ]
    report = detect_regression(
        "realtime.turn.time_to_first_audio", data,
        dimensions=["realtime.prompt.version"], lower_is_better=True, **WINDOWS,
    )

    assert report.verdict is Verdict.REGRESSED
    best = report.best_attribution
    assert best is not None
    assert best.culprits[0].value == "v17"
    assert "v17" in report.headline()
    assert "v16" in best.explain()


def test_broad_change_is_not_attributed_to_any_segment():
    """When every segment moves together the dimension explains nothing, and
    claiming otherwise sends people to fix the wrong thing."""
    data = [
        series({"realtime.prompt.version": "v16"}, 300.0, 600.0),
        series({"realtime.prompt.version": "v17"}, 300.0, 600.0),
    ]
    report = detect_regression(
        "realtime.turn.time_to_first_audio", data,
        dimensions=["realtime.prompt.version"], lower_is_better=True, **WINDOWS,
    )
    assert report.verdict is Verdict.REGRESSED
    best = report.best_attribution
    assert best is None or best.concentration < 0.7
    assert "look upstream" in report.headline() or best is None


def test_small_movement_is_not_a_regression():
    data = [series({"realtime.prompt.version": "v16"}, 300.0, 312.0)]
    report = detect_regression(
        "realtime.turn.time_to_first_audio", data,
        dimensions=["realtime.prompt.version"], lower_is_better=True, **WINDOWS,
    )
    assert report.verdict is Verdict.NO_CHANGE


def test_improvement_is_not_reported_as_regression():
    data = [series({"realtime.prompt.version": "v18"}, 600.0, 300.0)]
    report = detect_regression(
        "realtime.turn.time_to_first_audio", data,
        dimensions=["realtime.prompt.version"], lower_is_better=True, **WINDOWS,
    )
    assert report.verdict is Verdict.IMPROVED


def test_transport_dimension_isolates_channel_specific_faults():
    """The other root cause an operator must be able to distinguish: only one
    channel broke, so the prompt is innocent."""
    data = [
        series({"realtime.transport": "websocket"}, 0.2, 0.21),
        series({"realtime.transport": "pstn"}, 0.2, 0.9),
    ]
    report = detect_regression(
        "realtime.barge_in.count", data,
        dimensions=["realtime.transport"], lower_is_better=True, **WINDOWS,
    )
    assert report.best_attribution.culprits[0].value == "pstn"


def test_insufficient_data_is_reported_honestly():
    report = detect_regression("realtime.turn.time_to_first_audio", [], **WINDOWS)
    assert report.verdict is Verdict.INSUFFICIENT_DATA
    assert "Not enough data" in report.headline()


def test_compare_segments_ranks_deployments():
    data = [
        series({"realtime.prompt.version": "v16"}, 300.0, 305.0),
        series({"realtime.prompt.version": "v17"}, 300.0, 600.0),
    ]
    rows = compare_segments(
        "realtime.turn.time_to_first_audio", data, "realtime.prompt.version",
        start_ms=2000, end_ms=3000,
    )
    assert [r.value for r in rows] == ["v17", "v16"]
    assert rows[0].current == pytest.approx(600.0)
