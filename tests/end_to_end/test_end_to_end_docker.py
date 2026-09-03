import json
import os
import subprocess
from pathlib import Path

import pytest

from twod_fim_jobs.consts import DEPTH_FILENAME, MANIFEST_FILENAME
from twod_fim_jobs.models.solvers import RunScenarioManifest
from twod_fim_jobs.utils.storage import read_json

NETWORK_PATH = (
    Path(__file__).parents[1] / "test_data" / "reference_data" / "reach_network.parquet"
)
LULC_PATH = (
    Path(__file__).parents[1]
    / "test_data"
    / "reference_data"
    / "e2e_lulc_subset_coarse.tif"
)
OUT_DIR = Path(__file__).parents[1] / "end_to_end_data"
MODEL_ROOT = OUT_DIR / "models"
RESULTS_ROOT = OUT_DIR / "results"

REACH_1 = 1257410937935512
REACH_2 = 1257410962372414
REACH_3 = 1257411073114277
REACHES = [REACH_1, REACH_2, REACH_3]

Q_LOW = 18500  # 5-yr
Q_HIGH = 24000  # 500-yr


BUILD_MODEL_IMAGE_ENV_VAR = "E2E_BUILD_MODEL_IMAGE"
RUN_ND_IMAGE_ENV_VAR = "E2E_ND_IMAGE"
RUN_KWSE_IMAGE_ENV_VAR = "E2E_KWSE_IMAGE"
USE_GPU_ENV_VAR = "E2E_USE_GPU"


@pytest.fixture()
def validate_e2e_image() -> dict[str, str]:
    """Validate and return the Docker images configured for the E2E test."""
    image_env_vars = {
        "build_model": BUILD_MODEL_IMAGE_ENV_VAR,
        "run_nd": RUN_ND_IMAGE_ENV_VAR,
        "run_kwse": RUN_KWSE_IMAGE_ENV_VAR,
    }
    images = {}

    for job_name, env_var in image_env_vars.items():
        image = os.environ.get(env_var)
        if not image:
            pytest.fail(f"{env_var} must be set before running Docker E2E tests")

        result = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            pytest.fail(
                f"Docker image configured by {env_var} is unavailable: {image}\n"
                f"{result.stderr.strip()}"
            )

        images[job_name] = image

    return images


def run_docker_job(image: str, payload: dict, gpu: bool = False) -> dict:
    """Execute a Docker container and return parsed JSON result."""
    repo_root = Path(__file__).parents[2]
    cmd = [
        "docker",
        "run",
        "--rm",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "-v",
        f"{repo_root}:{repo_root}",
    ]

    if os.environ.get(USE_GPU_ENV_VAR, "false").lower() == "true":
        cmd.extend(["--gpus", "all"])

    if os.path.exists(str(repo_root / ".env")):
        cmd.extend(["--env-file", str(repo_root / ".env")])

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


def export_discharge_vrts(results_root: Path) -> list[Path]:
    """Create one relative-path VRT combining each discharge across reaches."""
    depth_paths_by_discharge: dict[str, list[Path]] = {}
    for depth_path in sorted(results_root.rglob(DEPTH_FILENAME)):
        discharge = depth_path.parent.name
        if discharge.startswith("q="):
            depth_paths_by_discharge.setdefault(discharge, []).append(depth_path)

    vrt_paths = []
    for discharge, depth_paths in sorted(depth_paths_by_discharge.items()):
        vrt_path = results_root / f"{discharge}.vrt"
        relative_depth_paths = [
            depth_path.relative_to(results_root).as_posix()
            for depth_path in depth_paths
        ]
        subprocess.run(
            [
                "gdalbuildvrt",
                "-overwrite",
                "-pixel-function",
                "max",
                vrt_path.name,
                *relative_depth_paths,
            ],
            cwd=results_root,
            check=True,
        )
        vrt_paths.append(vrt_path)

    return vrt_paths


@pytest.mark.e2e
def test_end_to_end_docker(validate_e2e_image):
    """End-to-end test using Docker containers."""
    # Ensure output directories exist
    MODEL_ROOT.mkdir(parents=True, exist_ok=True)
    RESULTS_ROOT.mkdir(parents=True, exist_ok=True)

    # Run build model for all three reaches
    build_model_results = {}
    for ind, reach_id in enumerate(REACHES):
        output_path = MODEL_ROOT / str(reach_id)
        output_path.mkdir(parents=True, exist_ok=True)
        us_reach = REACHES[ind + 1] if ind + 1 < len(REACHES) else None
        us_list = [us_reach] if us_reach is not None else []

        payload = {
            "reach_id": reach_id,
            "reach_network_path": str(NETWORK_PATH.resolve()),
            "upstream_reach_ids": us_list,
            "upstream_mainstem_reach_id": us_reach,
            "base_output_path": str(output_path.resolve()),
            "grid_resolution": 100,
            "lulc_source": str(LULC_PATH.resolve()),
        }
        build_model_results[reach_id] = run_docker_job(
            validate_e2e_image["build_model"], payload
        )["plugin_results"]

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
        "volume_convergence_tolerance": 0.1,
        "allow_water_on_edges": True,
        "adaptive_step_algorithm_max_stage_min_acceptable": 0.5,
        "adaptive_step_algorithm_max_stage_max_acceptable": 3,
        "adaptive_step_algorithm_median_stage_min_acceptable": 0.5,
        "adaptive_step_algorithm_median_stage_max_acceptable": 3,
        "adaptive_step_algorithm_extent_min_acceptable": 0.075,
        "adaptive_step_algorithm_extent_max_acceptable": 0.2,
    }
    results = run_docker_job(validate_e2e_image["run_nd"], payload)["plugin_results"]

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
            "upstream_discharge": manifest.properties.us_discharge,
            "bc_value": manifest.properties.nominal_wse,
            "downstream_Scenario": i["trial_scenario_manifest"],
            "hotstart": hot_start,
        }
        last_q = manifest.properties.us_discharge
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
    results = run_docker_job(validate_e2e_image["run_kwse"], payload)["plugin_results"]

    export_discharge_vrts(RESULTS_ROOT)

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
            "upstream_discharge": manifest.properties.us_discharge,
            "bc_value": manifest.properties.nominal_wse,
            "downstream_Scenario": i,
            "hotstart": hot_start,
        }
        last_q = manifest.properties.us_discharge
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
    results = run_docker_job(validate_e2e_image["run_kwse"], payload)["plugin_results"]
