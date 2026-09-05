"""
End-to-end tests for the run_nd_scenarios workflow.

Follows the pattern of build_model tests: focuses on meaningful integration tests
that verify actual workflow execution with real test data.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

import twod_fim_jobs.jobs.run_nd_scenarios as run_nd
from twod_fim_jobs.jobs.run_nd_scenarios import (
    RunNDScenariosJob,
    _next_trial_q,
    _reuse_finished_runs,
    _step_scale,
    compare_scenario_changes,
)
from twod_fim_jobs.models.run_nd_scenarios import (
    AdaptiveStepComparisonResults,
    RunNDScenariosInputs,
    RunNDScenariosResult,
)


ROOT = Path(__file__).parent
TEST_MODEL_DATA = (
    ROOT / "test_data" / "models" / "reach=1257410937935512" / "10850311_N48S45E47W42"
)
TEST_OUTFLOW_GEOJSON = ROOT / "test_data" / "shared" / "outflow_area.geojson"

RUN_ND_DEFAULTS = RunNDScenariosInputs(
    model_manifest_path="s3://bucket/model_manifest.json",
    model_results_base_path="s3://bucket/results",
    min_upstream_inflow=10,
    max_upstream_inflow=100,
    delta_upstream_inflow=5,
)


### FIXTURES ###


@pytest.fixture
def run_nd_base_inputs() -> RunNDScenariosInputs:
    """Base fixture with valid inputs using test data."""
    return RunNDScenariosInputs(
        model_manifest_path=str(TEST_MODEL_DATA / "model_manifest.json"),
        model_results_base_path="/tmp/test-nd-output",
        min_upstream_inflow=1000.0,
        max_upstream_inflow=2000.0,
        delta_upstream_inflow=500.0,
        outflow_area_polygon_path=str(TEST_OUTFLOW_GEOJSON),
        max_simulation_length_seconds=360,
        volume_convergence_tolerance=0.1,
    )


@pytest.fixture
def run_nd_inputs_small_range(
    run_nd_base_inputs: RunNDScenariosInputs, tmp_path: Path
) -> RunNDScenariosInputs:
    """Fixture with small discharge range for faster testing."""
    inputs_dict = run_nd_base_inputs.model_dump()
    inputs_dict["model_results_base_path"] = str(tmp_path / "results_small")
    inputs_dict["max_upstream_inflow"] = 1500.0
    inputs_dict["max_simulation_length_seconds"] = 1800.0  # 30 min
    return RunNDScenariosInputs.model_validate(inputs_dict)


@pytest.fixture
def run_nd_inputs_bad_manifest(
    run_nd_base_inputs: RunNDScenariosInputs, tmp_path: Path
) -> RunNDScenariosInputs:
    """Fixture with non-existent manifest path."""
    inputs_dict = run_nd_base_inputs.model_dump()
    inputs_dict["model_manifest_path"] = str(tmp_path / "nonexistent_manifest.json")
    inputs_dict["model_results_base_path"] = str(tmp_path / "results_bad")
    return RunNDScenariosInputs.model_validate(inputs_dict)


@pytest.fixture
def run_nd_inputs_bad_outflow(
    run_nd_base_inputs: RunNDScenariosInputs, tmp_path: Path
) -> RunNDScenariosInputs:
    """Fixture with non-existent outflow polygon path."""
    inputs_dict = run_nd_base_inputs.model_dump()
    inputs_dict["outflow_area_polygon_path"] = str(
        tmp_path / "nonexistent_outflow.geojson"
    )
    inputs_dict["model_results_base_path"] = str(tmp_path / "results_bad_outflow")
    return RunNDScenariosInputs.model_validate(inputs_dict)


### TESTS ###


def test_end_to_end(
    run_nd_inputs_small_range: RunNDScenariosInputs, mock_run_lisflood
) -> None:
    """End-to-end test that executes workflow and validates results and output location."""
    job = RunNDScenariosJob()
    result = job.run(run_nd_inputs_small_range.model_dump())

    # Validate result type
    assert isinstance(result, RunNDScenariosResult)

    # Validate result structure
    assert len(result.scenario_comparison_results) > 0, "Expected at least one scenario"
    assert isinstance(result.warnings, list)

    # Extract scenario manifest paths from comparison results
    scenario_manifest_paths = [
        comparison.trial_scenario_manifest
        for comparison in result.scenario_comparison_results
        if comparison is not None
    ]
    assert len(scenario_manifest_paths) > 0, "Expected at least one scenario manifest"

    # Validate all manifest files exist
    for manifest_path in scenario_manifest_paths:
        assert Path(manifest_path).exists(), (
            f"Scenario manifest not found: {manifest_path}"
        )

    # Validate outputs are in correct location
    results_base = Path(run_nd_inputs_small_range.model_results_base_path)
    assert results_base.exists(), f"Results directory not created at {results_base}"

    # All manifest paths should be under results_base
    for manifest_path in scenario_manifest_paths:
        assert (
            Path(manifest_path).parent.resolve().is_relative_to(results_base.resolve())
        )


def test_missing_manifest_raises(
    run_nd_inputs_bad_manifest: RunNDScenariosInputs,
) -> None:
    """Missing model manifest raises FileNotFoundError."""
    job = RunNDScenariosJob()
    with pytest.raises(FileNotFoundError):
        job.run(run_nd_inputs_bad_manifest.model_dump())


def test_missing_outflow_polygon_raises(
    run_nd_inputs_bad_outflow: RunNDScenariosInputs,
) -> None:
    """Missing outflow polygon raises FileNotFoundError."""
    job = RunNDScenariosJob()
    with pytest.raises(FileNotFoundError):
        job.run(run_nd_inputs_bad_outflow.model_dump())


# TODO: add test that both supplied and non supplied outflow area polygons are supported.


### ADAPTIVE STEP COMPARISON ###


def _scenario(max_depth: float, median_depth: float, flooded_area: float):
    scenario = MagicMock()
    scenario.self_href = "s3://bucket/scenario_manifest.json"
    scenario.properties.max_depth = max_depth
    scenario.properties.median_depth = median_depth
    scenario.properties.flooded_area = flooded_area
    return scenario


REF = _scenario(2.0, 0.5, 1.0)
# Against REF and the default ranges (0.75-1.25 m, 0.25-0.5 m, 10-15%).
TRIALS = {
    "reject_low": _scenario(2.02, 0.52, 1.005),
    "accept": _scenario(2.90, 0.85, 1.120),
    "reject_high": _scenario(3.90, 1.60, 1.400),
}


@pytest.mark.parametrize("expected", list(TRIALS))
def test_comparison_judges_each_criterion_against_its_range(expected):
    result = compare_scenario_changes(
        TRIALS[expected], RUN_ND_DEFAULTS, REF, log_results=False
    )
    assert result.result == expected


@pytest.mark.parametrize(
    "current_q, delta, ceiling_q, expected",
    [
        (100, 50, None, 150),  # no ceiling, the step stands
        (100, 30, 150, 130),  # proposal is already inside the bracket
        (100, 50, 150, 125),  # proposal reaches the ceiling, so bisect instead
        (100, 90, 150, 125),  # and it does not matter by how much it overshoots
        (100, 50, 121, 110),  # a narrow bracket still yields a midpoint
        (100, 50, 115, 110),  # narrower than 2x min_delta_q: clamp, do not give up
    ],
)
def test_next_trial_q_never_proposes_at_or_above_the_ceiling(
    current_q, delta, ceiling_q, expected
):
    """A reject_high proves every larger discharge is also too high, so proposing
    one would spend a simulation learning what monotonicity already gives."""
    assert _next_trial_q(current_q, delta, ceiling_q, min_delta_q=10) == expected


@pytest.mark.parametrize("ceiling_q", [101, 105, 110])
def test_next_trial_q_reports_a_closed_bracket(ceiling_q):
    """No discharge is both min_delta_q above current and below the ceiling, so
    there is nothing left to try. The caller takes the ceiling run instead."""
    assert _next_trial_q(100, 50, ceiling_q, min_delta_q=10) is None


def test_the_bracket_closes_at_min_delta_q_not_twice_it():
    """Width 11 still holds one usable discharge and must not be abandoned;
    bisecting alone would aim at 105 and give up while 110 was available."""
    assert _next_trial_q(100, 50, 111, min_delta_q=10) == 110
    assert _next_trial_q(100, 50, 110, min_delta_q=10) is None


def test_force_accept_overrides_every_outcome():
    """The final max-discharge run must be in the library whatever it measures."""
    for trial in TRIALS.values():
        result = compare_scenario_changes(
            trial, RUN_ND_DEFAULTS, REF, force_accept=True, log_results=False
        )
        assert result.result == "accept"


def test_a_baseline_with_no_reference_is_accepted():
    result = compare_scenario_changes(
        TRIALS["accept"], RUN_ND_DEFAULTS, None, log_results=False
    )
    assert result.result == "accept"
    assert result.ref_scenario_manifest is None
    assert result.max_depth_increase == 0


### REUSING FINISHED RUNS ###


def _completed(q: int, max_depth: float, median_depth: float, flooded_area: float):
    completed = MagicMock()
    completed.manifest.properties.us_discharge = q
    completed.manifest.properties.max_depth = max_depth
    completed.manifest.properties.median_depth = median_depth
    completed.manifest.properties.flooded_area = flooded_area
    completed.manifest.self_href = f"s3://bucket/q={q}/scenario_manifest.json"
    return completed


def test_a_run_rejected_as_too_high_is_reconsidered_once_the_reference_moves():
    """Figure B: 150 was too big a step from 100, but from 136 it is too small.
    That verdict costs no simulation, and it bounds the next proposal."""
    ref = _completed(100, 2.00, 0.50, 1.000)
    done = {
        100: ref,
        136: _completed(136, 2.92, 0.80, 1.090),
        150: _completed(150, 3.40, 0.95, 1.140),
    }
    reuse = _reuse_finished_runs(done[136], done, RUN_ND_DEFAULTS)

    assert reuse.ref is done[136], "150 is too small a step to advance the reference"
    assert reuse.accepted == []
    assert reuse.ceiling_q is None
    assert reuse.current is done[150], "150 becomes the position and the hotstart"


def test_the_free_pass_advances_the_reference_without_simulating():
    """A finished run that lands in band against the new reference is a library
    point already paid for."""
    ref = _completed(100, 2.00, 0.50, 1.00)
    in_band = _completed(200, 3.00, 0.90, 1.12)
    done = {100: ref, 200: in_band}

    reuse = _reuse_finished_runs(ref, done, RUN_ND_DEFAULTS)
    assert reuse.ref is in_band
    assert reuse.accepted == [in_band]


def test_the_free_pass_keeps_advancing_while_finished_runs_allow():
    """Each advance re-judges what is left, so one accept can unlock the next."""
    ref = _completed(100, 2.00, 0.50, 1.00)
    first = _completed(200, 3.00, 0.90, 1.12)
    second = _completed(300, 4.00, 1.30, 1.25)
    done = {100: ref, 200: first, 300: second}

    reuse = _reuse_finished_runs(ref, done, RUN_ND_DEFAULTS)
    assert reuse.accepted == [first, second]
    assert reuse.ref is second


def test_the_free_pass_reports_the_lowest_run_still_too_high():
    ref = _completed(100, 2.00, 0.50, 1.00)
    done = {
        100: ref,
        400: _completed(400, 6.00, 3.00, 2.00),
        500: _completed(500, 7.00, 4.00, 3.00),
    }
    reuse = _reuse_finished_runs(ref, done, RUN_ND_DEFAULTS)
    assert reuse.ceiling_q == 400, "the lowest proven-too-high run bounds proposals"
    assert reuse.accepted == []


def test_the_free_pass_reports_the_width_of_the_last_accepted_step():
    """The step that earned the verdict is reference to trial, and it is the
    only measured evidence of what fits at the new reference. Chained advances
    report the LAST gap, not the total distance travelled: 100 -> 300 was never
    judged in band, and using it would oversize every step after a free pass."""
    ref = _completed(100, 2.00, 0.50, 1.00)
    first = _completed(200, 3.00, 0.90, 1.12)
    second = _completed(300, 4.00, 1.30, 1.25)
    done = {100: ref, 200: first, 300: second}

    reuse = _reuse_finished_runs(ref, done, RUN_ND_DEFAULTS)
    assert reuse.accepted == [first, second]
    assert reuse.last_gap == 100, "300 was accepted against 200, not against 100"


def test_the_free_pass_reports_no_gap_when_it_accepts_nothing():
    ref = _completed(100, 2.00, 0.50, 1.00)
    done = {100: ref, 400: _completed(400, 6.00, 3.00, 2.00)}
    assert _reuse_finished_runs(ref, done, RUN_ND_DEFAULTS).last_gap is None


def test_the_free_pass_stops_at_the_highest_run_it_can_accept(monkeypatch):
    """Scanning down, an accept at the top makes everything below it irrelevant:
    it is already the furthest advance, so those comparisons are never made."""
    ref = _completed(100, 2.00, 0.50, 1.00)
    done = {
        100: ref,
        200: _completed(200, 2.80, 0.85, 1.10),
        300: _completed(300, 3.00, 0.90, 1.12),
    }
    compared: list[int] = []
    real = run_nd.compare_scenario_changes

    def spy(trial, inputs, ref_manifest=None, **kwargs):
        compared.append(trial.properties.us_discharge)
        return real(trial, inputs, ref_manifest, **kwargs)

    monkeypatch.setattr(run_nd, "compare_scenario_changes", spy)
    reuse = _reuse_finished_runs(ref, done, RUN_ND_DEFAULTS)

    assert reuse.accepted == [done[300]]
    assert 200 not in compared, "200 is below an accepted run and never judged"


def test_runs_at_or_below_the_reference_are_ignored():
    ref = _completed(200, 3.00, 0.90, 1.12)
    done = {100: _completed(100, 2.00, 0.50, 1.00), 200: ref}
    reuse = _reuse_finished_runs(ref, done, RUN_ND_DEFAULTS)
    assert reuse.ref is ref
    assert reuse.current is ref
    assert reuse.ceiling_q is None


### STEP SIZING ###


def _comparison(max_depth: float, median_depth: float, area_prcnt: float):
    return AdaptiveStepComparisonResults(
        ref_scenario_manifest="s3://bucket/ref.json",
        trial_scenario_manifest="s3://bucket/trial.json",
        max_depth_increase=max_depth,
        median_depth_increase=median_depth,
        flooded_area_prcnt_increase=area_prcnt,
        result="accept",
    )


def test_an_oversized_step_is_scaled_towards_the_band_midpoint():
    """max depth came in at 1.40 against a band of 0.75-1.25, midpoint 1.00.
    A blind halving would go to 0.50; the measurement asks for 0.71."""
    scale = _step_scale(_comparison(1.40, 0.30, 11.0), RUN_ND_DEFAULTS, True)
    assert scale == pytest.approx(1.00 / 1.40, rel=1e-6)


def test_an_undersized_step_grows_by_the_least_a_criterion_needs():
    """Accept needs only one criterion above its floor, so the smallest growth
    that reaches a band is enough. Area needs 12.5/10.0; depth would need 5x."""
    scale = _step_scale(_comparison(0.20, 0.05, 10.0), RUN_ND_DEFAULTS, False)
    assert scale == pytest.approx(12.5 / 10.0, rel=1e-6)


def test_the_binding_criterion_is_whichever_asks_for_least():
    """Scaling by the minimum moves that criterion to its midpoint and leaves
    every other at or below its own, so fixing one cannot break another."""
    # depth asks for 1.11x, median for 1.25x, area for 0.625x. Area binds.
    over_on_area = _step_scale(_comparison(0.90, 0.30, 20.0), RUN_ND_DEFAULTS, True)
    assert over_on_area == pytest.approx(12.5 / 20.0, rel=1e-6)


@pytest.mark.parametrize("bracketed", [True, False])
def test_shrinking_is_always_bounded_by_the_shrink_factor(bracketed):
    """Nothing below the shrink factor, with or without a ceiling."""
    scale = _step_scale(_comparison(99.0, 99.0, 99.0), RUN_ND_DEFAULTS, bracketed)
    assert scale == pytest.approx(0.5)


def test_growth_is_capped_while_a_ceiling_bounds_the_search():
    """With a ceiling the proposal is bisected into the bracket anyway, so a
    large factor would be discarded; the grow factor still caps it."""
    scale = _step_scale(_comparison(0.001, 0.001, 0.001), RUN_ND_DEFAULTS, True)
    assert scale == pytest.approx(1.5)


def test_growth_is_uncapped_once_nothing_bounds_the_search():
    """With no ceiling the step IS the search. The reach barely moved, so the
    measurement asks for a large jump and gets it -- capping it at 1.5 is what
    made the sweep walk 647 -> 954 in five simulations instead of one."""
    scale = _step_scale(_comparison(0.001, 0.001, 0.001), RUN_ND_DEFAULTS, False)
    assert scale == pytest.approx(0.375 / 0.001), "median binds, and is not clamped"
    assert scale > RUN_ND_DEFAULTS.adaptive_step_algorithm_grow_factor


def test_a_step_that_moved_nothing_grows():
    """No positive increase means no ratio to compute; the only useful move is up."""
    assert _step_scale(
        _comparison(0.0, 0.0, 0.0), RUN_ND_DEFAULTS, False
    ) == pytest.approx(1.5)
