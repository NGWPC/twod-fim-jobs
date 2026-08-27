import json
import os
import subprocess
from pathlib import Path

import pytest

from twod_fim_jobs.consts import MANIFEST_FILENAME
from twod_fim_jobs.models.solvers import RunScenarioManifest
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

BUILD_MODEL_IMAGE = "twod-fim-jobs:build_model"
RUN_ND_IMAGE = "twod-fim-jobs:run_nd_scenarios"
RUN_KWSE_IMAGE = "twod-fim-jobs:run_kwse_scenarios"


def run_docker_job(image: str, payload: dict, gpu: bool = False) -> dict:
    """Execute a Docker container and return parsed JSON result."""
    repo_root = Path(__file__).parent.parent
    cmd = [
        "docker",
        "run",
        "--rm",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "-v",
        f"{repo_root}:{repo_root}",
        "--env-file",
        str(repo_root / ".env"),
    ]

    if gpu:
        cmd.extend(["--gpus", "all"])

    cmd.append(image)
    cmd.append(json.dumps(payload))

    result = subprocess.run(cmd, capture_output=True, text=True, check=False)

    if result.returncode != 0:
        print(f"\n{'=' * 60}")
        print(f"Docker command failed with exit code {result.returncode}")
        print(f"Image: {image}")
        print(f"Payload: {json.dumps(payload, indent=2)}")
        print(f"STDOUT:\n{result.stdout}")
        print(f"STDERR:\n{result.stderr}")
        print("=" * 60)
        raise subprocess.CalledProcessError(
            result.returncode, cmd, result.stdout, result.stderr
        )

    output = result.stdout.strip()
    print(f"\n{image}: {output}")
    return json.loads(output)


@pytest.mark.e2e
def test_end_to_end_docker():
    """End-to-end test using Docker containers."""
    # Ensure output directories exist
    MODEL_ROOT.mkdir(parents=True, exist_ok=True)
    RESULTS_ROOT.mkdir(parents=True, exist_ok=True)

    # Run build model for all three reaches
    build_model_results = {}
    for i in REACHES:
        output_path = MODEL_ROOT / str(i)
        output_path.mkdir(parents=True, exist_ok=True)

        payload = {
            "reach_id": i,
            "db_uri": f"sqlite:///{NETWORK_GPKG.resolve()}",
            "base_output_path": str(output_path.resolve()),
            "domain_buffer": 1000,
        }
        build_model_results[i] = run_docker_job(BUILD_MODEL_IMAGE, payload)[
            "plugin_results"
        ]

    # Run ND for most downstream reach
    build_results = build_model_results[REACH_1]
    manifest_path = (
        MODEL_ROOT / str(REACH_1) / build_results["model_id"] / MANIFEST_FILENAME
    )
    payload = {
        "model_manifest_path": str(manifest_path.resolve()),
        "model_results_base_path": str(RESULTS_ROOT.resolve()),
        "min_upstream_inflow": Q_LOW,
        "max_upstream_inflow": Q_HIGH,
        "delta_upstream_inflow": 1000,
        "outflow_area_polygon_path": str(OUTLET_ACCEPTABLE_OUTFLOW_AREA.resolve()),
        "volume_convergence_tolerance": 0.1,
        "allow_water_on_edges": True,
        "adaptive_step_algorithm_max_stage_min_acceptable": 0.5,
        "adaptive_step_algorithm_max_stage_max_acceptable": 3,
        "adaptive_step_algorithm_median_stage_min_acceptable": 0.5,
        "adaptive_step_algorithm_median_stage_max_acceptable": 3,
        "adaptive_step_algorithm_extent_min_acceptable": 0.075,
        "adaptive_step_algorithm_extent_max_acceptable": 0.2,
    }
    results = run_docker_job(RUN_ND_IMAGE, payload, gpu=True)["plugin_results"]

    # Build KWSE scenarios
    scenarios = []
    last_q = None
    last_wse = None
    for i in results.get("scenario_comparison_results", []):
        manifest = RunScenarioManifest.model_validate_json(
            read_json(i["trial_scenario_manifest"])
        )
        if last_q is not None:
            hot_start = {"upstream_discharge": last_q, "bc_value": last_wse}
        else:
            hot_start = None
        scenario = {
            "upstream_discharge": manifest.us_discharge,
            "bc_value": manifest.properties.nominal_wse,
            "downstream_Scenario": i["trial_scenario_manifest"],
            "hotstart": hot_start,
        }
        last_q = manifest.us_discharge
        last_wse = manifest.properties.nominal_wse
        scenarios.append(scenario)

    # Run KWSE for next reach
    build_results = build_model_results[REACH_2]
    manifest_path = (
        MODEL_ROOT / str(REACH_2) / build_results["model_id"] / MANIFEST_FILENAME
    )
    payload = {
        "model_manifest_path": str(manifest_path.resolve()),
        "model_results_base_path": str(RESULTS_ROOT.resolve()),
        "scenarios": scenarios,
        "volume_convergence_tolerance": 0.1,
        "allow_water_on_edges": True,
    }
    results = run_docker_job(RUN_KWSE_IMAGE, payload, gpu=True)["plugin_results"]

    # Build KWSE scenarios
    scenarios = []
    last_q = None
    last_wse = None
    for i in results.get("manifests", []):
        manifest = RunScenarioManifest.model_validate_json(read_json(i))
        if last_q is not None:
            hot_start = {"upstream_discharge": last_q, "bc_value": last_wse}
        else:
            hot_start = None
        scenario = {
            "upstream_discharge": manifest.us_discharge,
            "bc_value": manifest.properties.nominal_wse,
            "downstream_Scenario": i,
            "hotstart": hot_start,
        }
        last_q = manifest.us_discharge
        last_wse = manifest.properties.nominal_wse
        scenarios.append(scenario)

    # Run KWSE for final reach.
    build_results = build_model_results[REACH_3]
    manifest_path = (
        MODEL_ROOT / str(REACH_3) / build_results["model_id"] / MANIFEST_FILENAME
    )
    payload = {
        "model_manifest_path": str(manifest_path.resolve()),
        "model_results_base_path": str(RESULTS_ROOT.resolve()),
        "scenarios": scenarios,
        "volume_convergence_tolerance": 0.1,
        "allow_water_on_edges": True,
    }
    results = run_docker_job(RUN_KWSE_IMAGE, payload, gpu=True)["plugin_results"]
