import pytest
from pathlib import Path

from twod_fim_jobs.jobs import WORKFLOWS
from twod_fim_jobs.models.build_model import BuildModelInputs, BuildModelResult
from twod_fim_jobs.models.run_kwse_scenarios import (
    RunKWSEScenariosInputs,
    KWSEScenario,
    HotStart,
    RunKWSEScenariosResult,
)
from twod_fim_jobs.models.run_nd_scenarios import (
    RunNDScenariosInputs,
    RunNDScenariosResult,
)
from twod_fim_jobs.models.solvers import RunScenarioManifest
from twod_fim_jobs.consts import MANIFEST_FILENAME
from twod_fim_jobs.utils.storage import read_json

NETWORK_GPKG = Path(__file__).parent / "build_model" / "data" / "reach_network.gpkg"
OUT_DIR = Path(__file__).parent / "end_to_end_data"
MODEL_ROOT = OUT_DIR / "models"
RESULTS_ROOT = OUT_DIR / "results"


REACH_1 = 1257410937935512
REACH_2 = 1257410962372414
REACH_3 = 1257411073114277
REACHES = [REACH_1, REACH_2, REACH_3]

Q_LOW = 18500  # 5-yr
Q_HIGH = 24000  # 500-yr

OUTLET_ACCEPTABLE_OUTFLOW_AREA = (
    Path(__file__).parent / "run_scenarios" / "data" / "outflow_area.geojson"
)


@pytest.mark.e2e
def test_end_to_end():
    # Run build model for all three reaches
    build_model_results = {}
    for i in REACHES:
        inputs = BuildModelInputs(
            reach_id=i,
            db_uri=f"sqlite:///{NETWORK_GPKG.resolve()}",
            base_output_path=str((MODEL_ROOT / str(i)).resolve()),
            domain_buffer=1000,
        )
        results = WORKFLOWS["build_model"]().run(inputs)
        build_model_results[i] = results

    # Run ND for most downstream reach
    build_results: BuildModelResult = build_model_results[REACH_1]
    manifest_path = (
        MODEL_ROOT / str(REACH_1) / build_results.model_id / MANIFEST_FILENAME
    )
    inputs = RunNDScenariosInputs(
        model_manifest_path=str(manifest_path),
        model_results_base_path=str(RESULTS_ROOT),
        min_upstream_inflow=Q_LOW,
        max_upstream_inflow=Q_HIGH,
        delta_upstream_inflow=1000,
        outflow_area_polygon_path=str(OUTLET_ACCEPTABLE_OUTFLOW_AREA),
        volume_convergence_tolerance=0.1,
        allow_water_on_edges=True,
        adaptive_step_algorithm_max_stage_min_acceptable=0.5,
        adaptive_step_algorithm_max_stage_max_acceptable=3,
        adaptive_step_algorithm_median_stage_min_acceptable=0.5,
        adaptive_step_algorithm_median_stage_max_acceptable=3,
        adaptive_step_algorithm_extent_min_acceptable=0.075,
        adaptive_step_algorithm_extent_max_acceptable=0.2,
    )
    results: RunNDScenariosResult = WORKFLOWS["run_nd_scenarios"]().run(inputs)

    # Build KWSE scenarios
    scenarios = []
    last_q = None
    last_wse = None
    for i in results.scenario_comparison_results:
        manifest: RunScenarioManifest = RunScenarioManifest.model_validate_json(
            read_json(i.trial_scenario_manifest)
        )
        if last_q is not None:
            hot_start = HotStart(upstream_discharge=last_q, bc_value=last_wse)
        else:
            hot_start = None
        scenario = KWSEScenario(
            upstream_discharge=manifest.us_discharge,
            bc_value=manifest.properties.nominal_wse,
            downstream_Scenario=i.trial_scenario_manifest,
            hotstart=hot_start,
        )
        last_q = manifest.us_discharge
        last_wse = manifest.properties.nominal_wse
        scenarios.append(scenario)

    # Run KWSE for next reach
    build_results: BuildModelResult = build_model_results[REACH_2]
    manifest_path = (
        MODEL_ROOT / str(REACH_2) / build_results.model_id / MANIFEST_FILENAME
    )
    inputs = RunKWSEScenariosInputs(
        model_manifest_path=str(manifest_path),
        model_results_base_path=str(RESULTS_ROOT),
        scenarios=scenarios,
        volume_convergence_tolerance=0.1,
        allow_water_on_edges=True,
    )
    results: RunKWSEScenariosResult = WORKFLOWS["run_kwse_scenarios"]().run(inputs)

    # Build KWSE scenarios
    scenarios = []
    last_q = None
    last_wse = None
    for i in results.manifests:
        manifest: RunScenarioManifest = RunScenarioManifest.model_validate_json(
            read_json(i)
        )
        if last_q is not None:
            hot_start = HotStart(upstream_discharge=last_q, bc_value=last_wse)
        else:
            hot_start = None
        scenario = KWSEScenario(
            upstream_discharge=manifest.us_discharge,
            bc_value=manifest.properties.nominal_wse,
            downstream_Scenario=i,
            hotstart=hot_start,
        )
        last_q = manifest.us_discharge
        last_wse = manifest.properties.nominal_wse
        scenarios.append(scenario)

    # Run KWSE for final reach.
    build_results: BuildModelResult = build_model_results[REACH_3]
    manifest_path = (
        MODEL_ROOT / str(REACH_3) / build_results.model_id / MANIFEST_FILENAME
    )
    inputs = RunKWSEScenariosInputs(
        model_manifest_path=str(manifest_path),
        model_results_base_path=str(RESULTS_ROOT),
        scenarios=scenarios,
        volume_convergence_tolerance=0.1,
        allow_water_on_edges=True,
    )
    results: RunKWSEScenariosResult = WORKFLOWS["run_kwse_scenarios"]().run(inputs)
