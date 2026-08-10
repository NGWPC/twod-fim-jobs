"""
End-to-end tests for the run_nd_scenarios workflow.

Follows the pattern of build_model tests: focuses on meaningful integration tests
that verify actual workflow execution with real test data.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from pyogrio.errors import DataSourceError

from twod_fim_jobs.jobs.run_nd_scenarios import RunNDScenariosJob, publish_scenario
from twod_fim_jobs.models.run_nd_scenarios import (
    RunNDScenariosInputs,
    RunNDScenariosResult,
)


ROOT = Path(__file__).parent
TEST_MODEL_DATA = ROOT / "data" / "1257410937935512" / "fceb20c6_N164S214E230W107"
TEST_OUTFLOW_GEOJSON = ROOT / "data" / "outflow_area.geojson"


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
        ds_slope=0.01,
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


def test_end_to_end(run_nd_inputs_small_range: RunNDScenariosInputs) -> None:
    """End-to-end test that executes workflow and validates results and output location."""
    job = RunNDScenariosJob()
    result = job.run(run_nd_inputs_small_range.model_dump())

    # Validate result type
    assert isinstance(result, RunNDScenariosResult)

    # Validate result structure
    assert len(result.scenario_manifest_paths) > 0, "Expected at least one scenario"
    assert len(result.scenario_comparison_results) == len(
        result.scenario_manifest_paths
    )
    assert isinstance(result.warnings, list)

    # Validate all manifest files exist
    for manifest_path in result.scenario_manifest_paths:
        assert Path(manifest_path).exists(), (
            f"Scenario manifest not found: {manifest_path}"
        )

    # Validate outputs are in correct location
    results_base = Path(run_nd_inputs_small_range.model_results_base_path)
    assert results_base.exists(), f"Results directory not created at {results_base}"

    # All manifest paths should be under results_base
    for manifest_path in result.scenario_manifest_paths:
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
    """Missing outflow polygon raises DataSourceError."""
    job = RunNDScenariosJob()
    with pytest.raises(DataSourceError):
        job.run(run_nd_inputs_bad_outflow.model_dump())


def test_invalid_solver_rejected(run_nd_base_inputs: RunNDScenariosInputs) -> None:
    """Invalid solver value is rejected during validation."""
    inputs_dict = run_nd_base_inputs.model_dump()
    inputs_dict["solver"] = "invalid_solver"

    with pytest.raises(ValueError, match="Solver must be one of"):
        RunNDScenariosInputs.model_validate(inputs_dict)


def test_publish_scenario_preserves_s3_double_slash(tmp_path: Path) -> None:
    """s3:// scheme must not be collapsed to s3:/ by path joining."""
    from twod_fim_jobs.models.common import RunConfig, ScenarioWorkerManifest
    from twod_fim_jobs.models.build_model import ModelManifest
    from twod_fim_jobs.utils.storage import read_json

    model_manifest = ModelManifest.model_validate_json(
        read_json(str(TEST_MODEL_DATA / "model_manifest.json"))
    )

    # Create placeholder local files so publish_scenario can copy them
    depth = tmp_path / "depth.tif"
    inun = tmp_path / "inundation.geojson"
    stl = tmp_path / "stl.geojson"
    for f in (depth, inun, stl):
        f.touch()

    worker_manifest = ScenarioWorkerManifest(
        nominal_wse=1.0,
        ds_wse=None,
        ds_slope=0.001,
        us_discharge=1000.0,
        allow_water_on_edges=False,
        dir_name="nd=1.0E03/q=1000",
        depth_path=depth,
        inundation_polygon_path=inun,
        stl_path=stl,
        zarr_path=None,
        scenario_diagnostics=[],
        termination_condition="volume_convergence",
        run_config=RunConfig(
            sim_time_seconds=360,
            save_interval_seconds=60,
            mass_interval_seconds=10,
            initial_tstep_seconds=1,
            use_cuda=False,
        ),
    )

    job_inputs = RunNDScenariosInputs(
        model_manifest_path="s3://bucket/model_manifest.json",
        model_results_base_path="s3://bucket/results",
        min_upstream_inflow=1000.0,
        max_upstream_inflow=2000.0,
        delta_upstream_inflow=500.0,
        ds_slope=0.001,
        outflow_area_polygon_path="s3://bucket/outflow.geojson",
    )

    with (
        patch("twod_fim_jobs.jobs.run_nd_scenarios.copy_file"),
        patch("twod_fim_jobs.jobs.run_nd_scenarios.copy_dir"),
        patch("twod_fim_jobs.jobs.run_nd_scenarios.write_json") as mock_write,
    ):
        manifest_path = publish_scenario(worker_manifest, job_inputs, model_manifest)

    assert manifest_path.startswith("s3://"), (
        f"Expected s3:// prefix, got: {manifest_path}"
    )
    mock_write.assert_called_once()
