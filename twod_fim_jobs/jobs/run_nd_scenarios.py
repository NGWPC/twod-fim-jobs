import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import numpy as np
from shapely import LineString, Point, Polygon
from pydantic import BaseModel
from twod_fim_jobs.hydraulic_solvers.post_process import post_process_lisflood
from twod_fim_jobs.consts import (
    DEFAULT_INITIAL_TSTEP_SECONDS,
    DEFAULT_MASS_INTERVAL_SECONDS,
    SCENARIO_MANIFEST_FILENAME,
    ADAPTIVE_STEP_ALGORITHM_GROW_FACTOR,
    ADAPTIVE_STEP_ALGORITHM_SHRINK_FACTOR,
    ADAPTIVE_STEP_ALGORITHM_EXTENT_MAX_ACCEPTABLE,
    ADAPTIVE_STEP_ALGORITHM_EXTENT_MIN_ACCEPTABLE,
    ADAPTIVE_STEP_ALGORITHM_MAX_STAGE_MAX_ACCEPTABLE,
    ADAPTIVE_STEP_ALGORITHM_MAX_STAGE_MIN_ACCEPTABLE,
    ADAPTIVE_STEP_ALGORITHM_MEDIAN_STAGE_MAX_ACCEPTABLE,
    ADAPTIVE_STEP_ALGORITHM_MEDIAN_STAGE_MIN_ACCEPTABLE,
    USE_CUDA,
)
from twod_fim_jobs.hydraulic_solvers.run import run_scenario
from twod_fim_jobs.jobs.common import Job, make_scenario_dir_name
from twod_fim_jobs.models.build_model import Domain, GridProperties, ModelManifest
from twod_fim_jobs.models.common import (
    RunConfig,
    ScenarioAssets,
    ScenarioProperties,
    ScenarioRunInputs,
    ScenarioRunManifest,
    ScenarioWorkerManifest,
)
from twod_fim_jobs.models.run_nd_scenarios import (
    RunNDScenariosInputs,
    RunNDScenariosResult,
    AdaptiveStepComparisonResults,
)
from twod_fim_jobs.utils.storage import copy_file, copy_dir, read_json, write_json
from twod_fim_jobs.utils.geospatial import Raster, tif_to_asc
from twod_fim_jobs.utils.hashing import get_run_identity_hash
from twod_fim_jobs.hydraulic_solvers.pre_process import write_nd_model_files

logger = logging.getLogger(__name__)


class AdaptiveStepAlgorithmStepResult(BaseModel):
    worker_manifest: ScenarioWorkerManifest
    comparison_results: AdaptiveStepComparisonResults | None


class RunNDScenariosJob(Job[RunNDScenariosInputs]):
    """Initialize a 2D FIM model for a single reach."""

    Inputs = RunNDScenariosInputs

    def _run(self, inputs: RunNDScenariosInputs, tmp_dir: Path) -> RunNDScenariosResult:
        """Run normal-depth scenarios for a single reach and publish results."""
        model_manifest = ModelManifest.model_validate_json(
            read_json(inputs.model_manifest_path)
        )

        # Fetch all assets in the `assets` block to local
        logger.info("Copying model assets to local working directory")
        for _, asset in model_manifest.assets:
            copy_file(asset.href, tmp_dir / Path(asset.href).name)

        # Prepare shared run data
        terrain_asc, roughness_asc = prepare_rasters(model_manifest, tmp_dir)
        inflow_line, outflow_area, us_point, ds_point = load_geometries(
            model_manifest, inputs
        )
        run_config = RunConfig(
            sim_time_seconds=inputs.max_simulation_length_seconds,
            save_interval_seconds=inputs.save_interval_seconds,
            mass_interval_seconds=DEFAULT_MASS_INTERVAL_SECONDS,
            initial_tstep_seconds=DEFAULT_INITIAL_TSTEP_SECONDS,
            use_cuda=USE_CUDA,
        )

        # Run adaptive step method
        scenario_manifests = run_adaptive_step_scenarios(
            ds_slope=inputs.ds_slope,
            outflow_area=outflow_area,
            min_us_discharge=inputs.min_upstream_inflow,
            max_us_discharge=inputs.max_upstream_inflow,
            delta_us_discharge=inputs.delta_upstream_inflow,
            inflow_line=inflow_line,
            us_point=us_point,
            ds_point=ds_point,
            domain=model_manifest.domain,
            grid=model_manifest.properties.grid,
            terrain_asc=terrain_asc,
            roughness_asc=roughness_asc,
            run_config=run_config,
            out_dir=tmp_dir,
            convergence_tolerance=inputs.volume_convergence_tolerance,
            allow_water_on_edges=inputs.allow_water_on_edges,
            save_zarr=inputs.save_zarr,
        )

        # Publish models to final location
        scenario_manifest_paths = [
            publish_scenario(m.worker_manifest, inputs, model_manifest)
            for m in scenario_manifests
        ]

        # Prepare results onject
        scenario_comparison_results = [m.comparison_results for m in scenario_manifests]
        results = RunNDScenariosResult(
            scenario_manifest_paths=scenario_manifest_paths,
            scenario_comparison_results=scenario_comparison_results,
            warnings=[],
        )

        return results


def prepare_rasters(model_manifest: ModelManifest, tmp_dir: Path) -> tuple[Path, Path]:
    """Convert terrain and roughness rasters from GeoTIFF to ASC format."""
    terrain_asc = tif_to_asc(tmp_dir / Path(model_manifest.assets.terrain.href).name)
    roughness_asc = tif_to_asc(
        tmp_dir / Path(model_manifest.assets.roughness.href).name
    )
    return terrain_asc, roughness_asc


def load_geometries(
    model_manifest: ModelManifest, inputs: RunNDScenariosInputs
) -> tuple[LineString, Polygon, Point, Point]:
    """Load inflow line, outflow area, and upstream/downstream points from disk."""
    inflow_line: LineString = gpd.read_file(
        model_manifest.assets.inflow_line.href
    ).geometry.iloc[0]
    outflow_area: Polygon = gpd.read_file(
        inputs.outflow_area_polygon_path
    ).geometry.iloc[0]
    centerline = gpd.read_file(model_manifest.assets.centerline.href).geometry.iloc[0]
    us_point: Point = Point(centerline.coords[0])
    ds_point: Point = Point(centerline.coords[-1])
    return inflow_line, outflow_area, us_point, ds_point


def run_adaptive_step_scenarios(
    ds_slope: float,
    outflow_area: Polygon,
    min_us_discharge: float,
    max_us_discharge: float,
    delta_us_discharge: float,
    inflow_line: LineString,
    us_point: Point,
    ds_point: Point,
    domain: Domain,
    grid: GridProperties,
    terrain_asc: Path,
    roughness_asc: Path,
    run_config: RunConfig,
    out_dir: Path,
    convergence_tolerance: float | None,
    allow_water_on_edges: bool,
    save_zarr: bool,
) -> list[AdaptiveStepAlgorithmStepResult]:
    """Run scenarios across a discharge range using an adaptive step size algorithm."""

    def _run_scenario(
        q: float, hotstart_path: Path | None = None
    ) -> ScenarioWorkerManifest:
        """Run a single scenario, optionally warm-starting from a previous depth raster."""
        if hotstart_path is not None:
            hotstart_asc = tif_to_asc(hotstart_path)
            _run_config = run_config.model_copy(
                update={"initial_state_path": hotstart_asc}
            )
        else:
            _run_config = run_config
        scenario_dir_name = make_scenario_dir_name(q, nd=ds_slope)
        return process_scenario_worker(
            ds_slope=ds_slope,
            outflow_area=outflow_area,
            us_discharge=q,
            inflow_line=inflow_line,
            us_point=us_point,
            ds_point=ds_point,
            domain=domain,
            grid=grid,
            terrain_asc=terrain_asc,
            roughness_asc=roughness_asc,
            run_config=_run_config,
            out_dir=out_dir / scenario_dir_name,
            scenario_dir_name=scenario_dir_name,
            convergence_tolerance=convergence_tolerance,
            allow_water_on_edges=allow_water_on_edges,
            save_zarr=save_zarr,
        )

    logger.info(
        f"Starting adaptive step algorithm for discharge range {min_us_discharge} - {max_us_discharge} w/ delta {delta_us_discharge}"
    )
    ref_scenario = _run_scenario(min_us_discharge)
    current_scenario = ref_scenario
    step_results = AdaptiveStepAlgorithmStepResult(
        worker_manifest=ref_scenario, comparison_results=None
    )
    scenarios: list[AdaptiveStepAlgorithmStepResult] = [step_results]
    q_trial = current_scenario.us_discharge + delta_us_discharge

    while q_trial < max_us_discharge:
        logger.info(f"Evaluating trial discharge {round(q_trial, 1)}")

        trial_scenario = _run_scenario(
            q_trial, hotstart_path=current_scenario.depth_path
        )
        if trial_scenario.termination_condition == "edge_error":
            logger.error("Aborting adaptive step algorithm for edge error")
            return []

        scenario_comparison = compare_scenario_changes(ref_scenario, trial_scenario)

        step_results = AdaptiveStepAlgorithmStepResult(
            worker_manifest=trial_scenario, comparison_results=scenario_comparison
        )
        scenarios.append(step_results)

        if scenario_comparison.result == "reject_high":
            logger.info(f"Rejecting trial discharge {round(q_trial, 1)}: high")
            delta_us_discharge *= ADAPTIVE_STEP_ALGORITHM_SHRINK_FACTOR

        elif scenario_comparison.result == "accept":
            logger.info(f"Accepting trial discharge {round(q_trial, 1)}")
            ref_scenario = trial_scenario
            current_scenario = trial_scenario

        elif scenario_comparison.result == "reject_low":
            logger.info(f"Rejecting trial discharge {round(q_trial, 1)}: low")
            current_scenario = trial_scenario
            delta_us_discharge *= ADAPTIVE_STEP_ALGORITHM_GROW_FACTOR

        q_trial = current_scenario.us_discharge + delta_us_discharge

    trial_scenario = _run_scenario(
        max_us_discharge, hotstart_path=current_scenario.depth_path
    )
    step_results = AdaptiveStepAlgorithmStepResult(
        worker_manifest=trial_scenario, comparison_results=None
    )
    scenarios.append(step_results)

    logger.info("Completed adaptive step algorithm")
    return scenarios


def process_scenario_worker(
    ds_slope: float,
    outflow_area: Polygon,
    us_discharge: float,
    inflow_line: LineString,
    us_point: Point,
    ds_point: Point,
    domain: Domain,
    grid: GridProperties,
    terrain_asc: Path,
    roughness_asc: Path,
    run_config: RunConfig,
    out_dir: Path,
    scenario_dir_name: str,
    convergence_tolerance: float | None,
    allow_water_on_edges: bool,
    save_zarr: bool,
) -> ScenarioWorkerManifest:
    """Prepare files, run simulation, and post-process a single KWSE scenario."""
    logger.info(
        f"Processing normal depth run with slope={round(ds_slope, 6)} and inflow={round(us_discharge, 1)}"
    )
    # Build hydraulic solver files for scenario.
    paths = write_nd_model_files(
        domain,
        grid,
        terrain_asc,
        roughness_asc,
        inflow_line,
        outflow_area,
        ds_slope,
        us_discharge,
        run_config,
        out_dir,
    )

    # Get indices of us and ds point on terrain raster
    raster = Raster(terrain_asc)
    us_col, us_row = ~raster.transform * (us_point.x, us_point.y)
    ds_col, ds_row = ~raster.transform * (ds_point.x, ds_point.y)
    us_inds = (int(np.floor(us_row)), int(np.floor(us_col)))
    ds_inds = (int(np.floor(ds_row)), int(np.floor(ds_col)))
    endpoint_indices = (us_inds, ds_inds)

    # Initialize simulation and watch
    scenario_diagnostics, termination_condition = run_scenario(
        paths["par_path"],
        us_discharge,
        convergence_tolerance,
        run_config.save_interval_seconds,
        endpoint_indices,
        raster.data,
        allow_water_on_edges,
    )

    # Post-process results
    processed = post_process_lisflood(out_dir, us_point, terrain_asc, save_zarr)

    # Initialize scenario scenario manifest
    return ScenarioWorkerManifest(
        nominal_wse=processed.nominal_wse,
        ds_wse=None,
        ds_slope=ds_slope,
        us_discharge=us_discharge,
        allow_water_on_edges=allow_water_on_edges,
        dir_name=scenario_dir_name,
        depth_path=processed.depth_path,
        inundation_polygon_path=processed.inundation_polygon_path,
        stl_path=processed.stl_path,
        scenario_diagnostics=scenario_diagnostics,
        termination_condition=termination_condition,
        zarr_path=processed.zarr_path,
        run_config=run_config,
    )


def compare_scenario_changes(
    ref_scenario: ScenarioWorkerManifest, trial_scenario: ScenarioWorkerManifest
) -> AdaptiveStepComparisonResults:
    """Compare depth and extent changes between a reference and trial scenario to accept or reject the step."""
    ref_raster = Raster(ref_scenario.depth_path)
    trial_raster = Raster(trial_scenario.depth_path)
    comparison_mask = (ref_raster.data > 0) | (trial_raster.data > 0)

    depth_diffs = (
        trial_raster.data[comparison_mask] - ref_raster.data[comparison_mask]
    ).flatten()
    max_depth_diff = np.quantile(depth_diffs, 0.95)
    median_depth_diff = np.median(depth_diffs)
    ref_extent = (ref_raster.data > 0).sum()
    extent_diff = ((trial_raster.data > 0).sum() - ref_extent) / ref_extent

    # reject_high takes priority: any criterion over its ceiling means the step was too large
    if (
        max_depth_diff > ADAPTIVE_STEP_ALGORITHM_MAX_STAGE_MAX_ACCEPTABLE
        or median_depth_diff > ADAPTIVE_STEP_ALGORITHM_MEDIAN_STAGE_MAX_ACCEPTABLE
        or extent_diff > ADAPTIVE_STEP_ALGORITHM_EXTENT_MAX_ACCEPTABLE
    ):
        result = "reject_high"
    elif (
        ADAPTIVE_STEP_ALGORITHM_MAX_STAGE_MIN_ACCEPTABLE <= max_depth_diff
        or ADAPTIVE_STEP_ALGORITHM_MEDIAN_STAGE_MIN_ACCEPTABLE <= median_depth_diff
        or ADAPTIVE_STEP_ALGORITHM_EXTENT_MIN_ACCEPTABLE <= extent_diff
    ):
        result = "accept"
    else:
        result = "reject_low"

    return AdaptiveStepComparisonResults(
        ref_us_discharge=ref_scenario.us_discharge,
        trial_us_discharge=trial_scenario.us_discharge,
        max_stage_diff=max_depth_diff,
        median_stage_diff=median_depth_diff,
        extent_diff=extent_diff,
        result=result,
    )


def publish_scenario(
    manifest: ScenarioWorkerManifest,
    job_inputs: RunNDScenariosInputs,
    model_manifest: ModelManifest,
) -> str:
    """Copy scenario assets to model_results_base_path, write the manifest JSON, and return its path."""
    # NOTE: Doing manual path construction because PurePosixPath was dropping s3:// to s3:/
    # TODO: Investigate better path management.
    run_identity_hash = get_run_identity_hash(job_inputs.solver_enum)

    # Handle S3 and other cloud paths by using string concatenation to preserve scheme
    base_path = job_inputs.model_results_base_path.rstrip("/")
    dest_dir_str = f"{base_path}/{run_identity_hash}/{manifest.dir_name}"

    # Build destination paths using string concatenation for S3 compatibility
    dest_depth_str = f"{dest_dir_str}/{manifest.depth_path.name}"
    dest_inun_str = f"{dest_dir_str}/{manifest.inundation_polygon_path.name}"
    dest_stl_str = f"{dest_dir_str}/{manifest.stl_path.name}"

    dest_depth = dest_depth_str
    dest_inun = dest_inun_str
    dest_stl = dest_stl_str
    copy_file(manifest.depth_path, dest_depth)
    copy_file(manifest.inundation_polygon_path, dest_inun)
    copy_file(manifest.stl_path, dest_stl)

    dest_zarr = None
    if manifest.zarr_path is not None:
        dest_zarr = f"{dest_dir_str}/{manifest.zarr_path.name}"
        copy_dir(manifest.zarr_path, dest_zarr)

    if manifest.run_config.initial_state_path is not None:
        shared_parent = Path(
            os.path.commonpath(
                [str(manifest.run_config.initial_state_path), str(manifest.depth_path)]
            )
        )
        # Build hotstart path using string concatenation for S3 compatibility
        relative_part = manifest.run_config.initial_state_path.relative_to(
            shared_parent
        )
        hotstart_path = Path(f"{dest_dir_str}/../{relative_part}".replace("\\", "/"))
    else:
        hotstart_path = None

    final_manifest = ScenarioRunManifest(
        created_at=datetime.now(timezone.utc),
        reach_id=model_manifest.reach_id,
        identity_hash=model_manifest.identity_hash,
        domain_code=model_manifest.domain_code,
        model_id=model_manifest.model_id,
        inputs=ScenarioRunInputs(
            ds_slope=manifest.ds_slope,
            ds_wse=manifest.ds_wse,
            us_discharge=manifest.us_discharge,
            scenario_dir_name=manifest.dir_name,
            volume_convergence_tolerance=job_inputs.volume_convergence_tolerance,
            allow_water_on_edges=manifest.allow_water_on_edges,
            outflow_area_polygon_path=job_inputs.outflow_area_polygon_path,
            inflow_line_path=model_manifest.assets.inflow_line.href,
            centerline_path=model_manifest.assets.centerline.href,
            domain=model_manifest.domain,
            grid=model_manifest.properties.grid,
            run_config=manifest.run_config.model_copy(
                update={"initial_state_path": hotstart_path}
            ),
            terrain_path=model_manifest.assets.terrain.href,
            roughness_path=model_manifest.assets.roughness.href,
            out_dir=dest_dir_str,
        ),
        properties=ScenarioProperties(
            nominal_wse=manifest.nominal_wse,
            scenario_diagnostics=manifest.scenario_diagnostics,
            termination_condition=manifest.termination_condition,
        ),
        assets=ScenarioAssets(
            depth=Path(str(dest_depth)),
            inundation_polygon=Path(str(dest_inun)),
            stage_transfer_line=Path(str(dest_stl)),
            zarr_store=Path(str(dest_zarr)) if dest_zarr is not None else None,
        ),
    )

    manifest_dest = f"{dest_dir_str}/{SCENARIO_MANIFEST_FILENAME}"
    write_json(manifest_dest, final_manifest.model_dump_json())
    return manifest_dest
