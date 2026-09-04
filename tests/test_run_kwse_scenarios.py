# TODO: Test that job accepts with no hot start.
# TODO: Test that job uses hot start when provided.
# TODO: Make sure it doesn't matter whether users submit paths with / or not / at the start
# TODO: Check that hot starts can be provided from a different run identity and will construct paths appropriately.


from pathlib import Path
import shutil

import pytest

from twod_fim_jobs.jobs.run_kwse_scenarios import RunKWSEScenariosJob
from twod_fim_jobs.models.run_kwse_scenarios import (
    HotStart,
    KWSEScenario,
    RunKWSEScenariosInputs,
    RunKWSEScenariosResult,
)

ROOT = Path(__file__).parent
US_MODEL_MANIFEST = (
    ROOT
    / "test_data"
    / "models"
    / "reach=1257410962372414"
    / "92075bd4_N66S58E73W66"
    / "model_manifest.json"
)
DS_SCENARIO = (
    ROOT
    / "test_data"
    / "results"
    / "reach=1257410937935512"
    / "10850311_N48S45E47W42"
    / "0c24be7a"
    / "nd=1.0E04"
    / "q=18500"
    / "scenario_manifest.json"
)
LOCAL_HOTSTART_ROOT = ROOT / "test_data" / "results"


### FIXTURES ###


@pytest.fixture
def run_kwse_base_inputs() -> RunKWSEScenariosInputs:
    """Base fixture with valid inputs using test data."""
    return RunKWSEScenariosInputs(
        model_manifest_path=str(US_MODEL_MANIFEST),
        model_results_base_path=str(LOCAL_HOTSTART_ROOT),
        scenarios=[],
        volume_convergence_tolerance=0.1,
        allow_water_on_edges=True,
    )


@pytest.fixture
def run_kwse_single_no_hot(
    run_kwse_base_inputs: RunKWSEScenariosInputs, tmp_path: Path
) -> RunKWSEScenariosInputs:
    """Fixture with small discharge range for faster testing."""
    scenarios = KWSEScenario(
        upstream_discharge=18500, bc_value=11.5, downstream_Scenario=str(DS_SCENARIO)
    )
    inputs_dict = run_kwse_base_inputs.model_dump()
    inputs_dict["scenarios"] = [scenarios.model_dump()]
    return RunKWSEScenariosInputs.model_validate(inputs_dict)


@pytest.fixture
def run_kwse_single_kwse_hot(
    run_kwse_base_inputs: RunKWSEScenariosInputs, tmp_path: Path
) -> RunKWSEScenariosInputs:
    """Fixture with small discharge range for faster testing."""
    hot_start = HotStart(
        upstream_discharge=18500,
        bc_type="KWSE",
        bc_value=11.4,
    )
    scenarios = KWSEScenario(
        upstream_discharge=18500,
        bc_value=11.5,
        downstream_Scenario=str(DS_SCENARIO),
        hotstart=hot_start,
    )
    inputs_dict = run_kwse_base_inputs.model_dump()
    inputs_dict["scenarios"] = [scenarios.model_dump()]
    return RunKWSEScenariosInputs.model_validate(inputs_dict)


@pytest.fixture
def run_kwse_single_nd_hot(
    run_kwse_base_inputs: RunKWSEScenariosInputs, tmp_path: Path
) -> RunKWSEScenariosInputs:
    """Fixture with small discharge range for faster testing."""
    hot_start = HotStart(
        upstream_discharge=18500,
        bc_type="ND",
        bc_value=1e-4,
    )
    scenarios = KWSEScenario(
        upstream_discharge=18500,
        bc_value=11.5,
        downstream_Scenario=str(DS_SCENARIO),
        hotstart=hot_start,
    )
    inputs_dict = run_kwse_base_inputs.model_dump()
    inputs_dict["scenarios"] = [scenarios.model_dump()]
    return RunKWSEScenariosInputs.model_validate(inputs_dict)


@pytest.fixture
def run_kwse_input(request: pytest.FixtureRequest) -> RunKWSEScenariosInputs:
    return request.getfixturevalue(request.param)


### TESTS ###


@pytest.mark.parametrize(
    "run_kwse_input",
    [
        "run_kwse_single_no_hot",
        "run_kwse_single_kwse_hot",
        "run_kwse_single_nd_hot",
    ],
    indirect=True,
)
def test_single_kwse(run_kwse_input: RunKWSEScenariosInputs, mock_run_lisflood) -> None:
    """End-to-end test that executes workflow and validates results and output location."""
    job = RunKWSEScenariosJob()
    result = job.run(run_kwse_input.model_dump())

    # Validate result type
    assert isinstance(result, RunKWSEScenariosResult)

    # Validate result structure
    assert len(result.manifests) == 1, "Expected one scenario manifest"

    # Validate all manifest files exist
    manifest_path = result.manifests[0]
    assert Path(manifest_path).exists(), f"Scenario manifest not found: {manifest_path}"

    # Cleanup
    shutil.rmtree(Path(manifest_path).parent)
