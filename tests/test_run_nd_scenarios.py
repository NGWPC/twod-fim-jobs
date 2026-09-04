"""
End-to-end tests for the run_nd_scenarios workflow.

Follows the pattern of build_model tests: focuses on meaningful integration tests
that verify actual workflow execution with real test data.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from twod_fim_jobs.jobs.run_nd_scenarios import (
    RunNDScenariosJob,
    _next_trial_q,
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
