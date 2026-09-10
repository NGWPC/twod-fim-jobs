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
    _acceptance_window,
    _crossing,
    _curves,
    _propose,
    _monotone,
    _reuse_finished_runs,
    compare_scenario_changes,
)
from twod_fim_jobs.models.run_nd_scenarios import (
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
    assert reuse.position is done[150], "150 becomes the position and the hotstart"


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
    assert reuse.position is ref


### CURVES ###


def test_a_curve_never_goes_down():
    """More water cannot mean less flooding, so a dip holds the earlier value."""
    assert _monotone([1.0, 1.5, 1.4, 1.8]) == [1.0, 1.5, 1.5, 1.8]


def test_a_criterion_that_falls_becomes_flat():
    """Median depth drops as water spreads over shallow ground. Flattening it is
    the honest answer: a falling criterion can never satisfy an increase band."""
    assert _monotone([0.5, 0.3, 0.2, 0.4, 0.9]) == [0.5, 0.5, 0.5, 0.5, 0.9]


def test_a_crossing_is_read_off_the_straight_segment():
    """Exact at every simulated point, linear in between."""
    assert _crossing([100, 200], [1.0, 3.0], 2.0) == pytest.approx(150.0)
    assert _crossing([100, 200], [1.0, 3.0], 1.0) == pytest.approx(100.0)


def test_a_crossing_above_the_last_point_carries_the_last_slope_on():
    """No segment exists up there, so extend the final one. The curves bend
    downward, so this promises slightly more than the reach delivers."""
    assert _crossing([100, 200], [1.0, 3.0], 4.0) == pytest.approx(250.0)


def test_a_flat_top_never_reaches_the_target():
    """Nothing left to extrapolate from -- the sweep is done below the maximum."""
    assert _crossing([100, 200, 300], [1.0, 2.0, 2.0], 3.0) is None


def test_one_point_is_not_a_curve():
    assert _crossing([100], [1.0], 2.0) is None


def _window(done, ref_q):
    qs, measures = _curves(done, RUN_ND_DEFAULTS)
    return _acceptance_window(qs, measures, ref_q)


def test_the_window_opens_at_the_earliest_floor_and_shuts_at_the_earliest_ceiling():
    """Acceptance needs ONE criterion at its floor but ALL under their ceilings,
    so both ends are a minimum -- over different criteria."""
    done = {
        100: _completed(100, 2.00, 0.50, 1.000),
        200: _completed(200, 2.20, 0.55, 2.000),
    }
    opens, closes = _window(done, 100)
    # area moves 100% over 100 cms, so +10% lands at 110 and +15% at 115;
    # the depths would need far more discharge than that.
    assert opens == pytest.approx(110.0)
    assert closes == pytest.approx(115.0)


def test_the_window_is_never_empty():
    """The criterion that reaches a floor first does so before ANY criterion
    reaches a ceiling, because its own ceiling comes later still."""
    for top in ([2.20, 0.55, 2.0], [9.00, 5.00, 9.0], [2.01, 0.51, 1.01]):
        done = {100: _completed(100, 2.0, 0.5, 1.0), 200: _completed(200, *top)}
        opens, closes = _window(done, 100)
        assert opens < closes


def test_flat_curves_leave_no_window():
    """Nothing reaches a floor at any discharge, so there is nothing left to
    place below the top of the range."""
    done = {
        100: _completed(100, 2.0, 0.5, 1.0),
        200: _completed(200, 2.0, 0.5, 1.0),
        300: _completed(300, 2.0, 0.5, 1.0),
    }
    assert _window(done, 100) is None


### THE DISCHARGE GRID ###


def _grid_inputs(grid: int):
    return RUN_ND_DEFAULTS.model_copy(
        update={"q_grid_resolution": grid, "max_upstream_inflow": 100000}
    )


def test_a_proposal_lands_on_a_grid_line():
    """Every scenario sits on the axis, so the window's answer is rounded to a
    line rather than taken as the raw number."""
    done = {
        100: _completed(100, 2.00, 0.50, 1.00),
        200: _completed(200, 3.00, 0.90, 1.12),
    }
    q, at_max, _ = _propose(done, _grid_inputs(10), 100, 200)
    assert not at_max and q % 10 == 0


def test_a_proposal_is_never_the_reference_itself():
    """The nearest line can round back onto the reference. The next line up is
    the closest a trial may be placed."""
    done = {
        100: _completed(100, 2.00, 0.50, 1.00),
        110: _completed(110, 2.01, 0.51, 1.001),
    }
    q, _, _ = _propose(done, _grid_inputs(10), 100, 110)
    assert q >= 110, "must move at least one grid line above the reference"


def test_being_one_grid_line_from_the_reference_is_measured_from_the_reference():
    """The bands are reference-to-trial, so the question 'is anything finer
    available' has to be asked the same way. Measuring from the position let a
    library gap exceed the step that produced it, which is what put out-of-band
    entries into published libraries."""
    done = {
        100: _completed(100, 2.00, 0.50, 1.00),
        110: _completed(110, 2.02, 0.51, 1.002),
    }
    # position is well ahead of the reference; the answer must not depend on it
    q, _, below = _propose(done, _grid_inputs(10), 100, 110)
    if q == 110:
        assert below, "one line from the reference means nothing finer exists"
    else:
        assert q - 100 > 10 and not below


def test_a_window_between_two_grid_lines_takes_the_next_line_up():
    """Reach 1269869556169965 as it actually ran: reference 40 on a 10 cms grid,
    window 50.9 to 56.3. Fifty is below it, sixty above, and no line lands
    inside. The sweep must take the next line up and keep it whatever it says --
    proposing the window's own answer again is what looped forever."""
    done = {
        40: _completed(40, 2.00, 0.50, 1.00),
        50: _completed(50, 2.21, 0.55, 1.09),
        80: _completed(80, 2.60, 0.65, 1.37),
    }
    q, at_max, forced = _propose(done, _grid_inputs(10), ref_q=40, position_q=50)
    assert not at_max
    assert forced, "no line fits, so the verdict is taken as it comes"
    assert q == 50, "the next line above the REFERENCE, not above the position"


def test_a_forced_proposal_never_climbs_past_the_reference():
    """The forced line is measured from the reference even when the position has
    run ahead of it, because the bands are measured from the reference too."""
    done = {100: _completed(100, 2.0, 0.5, 1.0), 110: _completed(110, 2.02, 0.51, 1.002),
            120: _completed(120, 2.04, 0.52, 1.004)}
    q, _, forced = _propose(done, _grid_inputs(10), ref_q=100, position_q=120)
    if forced:
        assert q == 110, "one line above the reference, whatever the position did"
