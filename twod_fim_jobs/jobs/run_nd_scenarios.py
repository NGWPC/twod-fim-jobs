import copy
import logging
import math
import tempfile
from pathlib import Path

import geopandas as gpd
import numpy as np
from shapely.geometry import Point, Polygon

from twod_fim_jobs.consts import (
    MINIMUM_REACH_SLOPE,
    bieger_bankfull_width,
)
from twod_fim_jobs.hydraulic_solvers.common import run_scenario
from twod_fim_jobs.hydraulic_solvers.identities import get_run_identity_hash
from twod_fim_jobs.jobs.common import Job
from twod_fim_jobs.models.build_model import ModelManifest
from twod_fim_jobs.models.common import (
    Asset,
)
from twod_fim_jobs.models.run_nd_scenarios import (
    AdaptiveStepComparisonResults,
    RunNDScenariosInputs,
    RunNDScenariosResult,
)
from twod_fim_jobs.models.solvers import (
    BoundaryCondition,
    FreeBC,
    QFixBC,
    RunConfig,
    RunScenarioInputs,
    RunScenarioManifest,
)
from twod_fim_jobs.models.warnings import WaterOnEdgeWarning
from twod_fim_jobs.utils.geospatial import (
    Raster,
    ensure_linestring,
    load_dem_and_get_pt_indices,
)
from twod_fim_jobs.utils.hashing import hash_file
from twod_fim_jobs.utils.storage import ASSET_CACHE, copy_file, read_json

logger = logging.getLogger(__name__)


def _scale_delta(delta: int, factor: float) -> int:
    """Grow or shrink the discharge step, keeping it a whole number of cms.

    Discharge is integral throughout this system: the bounds and the step are
    authored as integers, the folder a scenario is written to is named by the
    discharge, and the library records the set of discharges that were run. The
    shrink and grow factors are ratios rather than flows, so scaling has to be
    rounded back or the step — and every discharge derived from it — drifts off
    the integers.

    Rounded to at least 1 so a small step cannot scale to zero and stall the
    sweep at one discharge.
    """
    return max(1, round(delta * factor))


class RunNDScenariosJob(Job[RunNDScenariosInputs]):
    """Initialize a 2D FIM model for a single reach."""

    Inputs = RunNDScenariosInputs

    def _run(self, inputs: RunNDScenariosInputs, tmp_dir: Path) -> RunNDScenariosResult:
        """Run normal-depth scenarios for a single reach and publish results."""
        # Initialize
        model_manifest = ModelManifest.model_validate_json(
            read_json(inputs.model_manifest_path)
        )
        downstream_bc = get_normal_depth_boundary_condition(model_manifest, inputs)
        delta_us_discharge = copy.copy(inputs.delta_upstream_inflow)

        # Start algorithms
        logger.info(
            f"Starting adaptive step algorithm for discharge range {inputs.min_upstream_inflow} - {inputs.max_upstream_inflow} w/ delta {delta_us_discharge}"
        )
        ref_scenario = _run_scenario(
            inputs.min_upstream_inflow, downstream_bc, model_manifest, inputs, tmp_dir
        )
        current_scenario = ref_scenario
        scenario_comparison = compare_scenario_changes(current_scenario, inputs, None)
        results = RunNDScenariosResult(
            scenario_comparison_results=[scenario_comparison], warnings=[]
        )
        q_trial = current_scenario.properties.us_discharge + delta_us_discharge

        while q_trial < inputs.max_upstream_inflow:
            logger.info(f"Evaluating trial discharge {q_trial}")

            trial_scenario = _run_scenario(
                q_trial,
                downstream_bc,
                model_manifest,
                inputs,
                tmp_dir,
                hot_start=current_scenario.assets.depth,
            )

            if trial_scenario.properties.termination_condition == "edge_error":
                logger.error("Aborting adaptive step algorithm for edge error")
                results.warnings.append(WaterOnEdgeWarning())
                return results

            scenario_comparison = compare_scenario_changes(
                trial_scenario,
                inputs,
                ref_scenario,
                force_accept=(delta_us_discharge <= inputs.adaptive_step_min_delta_q),
            )
            results.scenario_comparison_results.append(scenario_comparison)

            if scenario_comparison.result == "reject_high":
                logger.info(f"Rejecting trial discharge {q_trial}: high")
                delta_us_discharge = _scale_delta(
                    delta_us_discharge, inputs.adaptive_step_algorithm_shrink_factor
                )

            elif scenario_comparison.result == "accept":
                logger.info(f"Accepting trial discharge {q_trial}")
                ref_scenario = trial_scenario
                current_scenario = trial_scenario

            elif scenario_comparison.result == "reject_low":
                logger.info(f"Rejecting trial discharge {q_trial}: low")
                current_scenario = trial_scenario
                delta_us_discharge = _scale_delta(
                    delta_us_discharge, inputs.adaptive_step_algorithm_grow_factor
                )

            delta_us_discharge = max(
                inputs.adaptive_step_min_delta_q, delta_us_discharge
            )
            q_trial = current_scenario.properties.us_discharge + delta_us_discharge

        trial_scenario = _run_scenario(
            inputs.max_upstream_inflow,
            downstream_bc,
            model_manifest,
            inputs,
            tmp_dir,
            hot_start=current_scenario.assets.depth,
        )
        scenario_comparison = compare_scenario_changes(
            trial_scenario, inputs, ref_scenario, force_accept=True
        )
        results.scenario_comparison_results.append(scenario_comparison)

        logger.info("Completed adaptive step algorithm")

        return results


def get_normal_depth_boundary_condition(
    model_manifest: ModelManifest, inputs: RunNDScenariosInputs
) -> BoundaryCondition:
    if inputs.outflow_area_polygon_path is None:
        outflow_area_polygon_path = derive_outflow_polygon(model_manifest)
    else:
        outflow_area_polygon_path = inputs.outflow_area_polygon_path
    geom_asset = Asset(
        href=outflow_area_polygon_path,
        checksum=hash_file(outflow_area_polygon_path, role_length=16),
    )
    slope = get_normal_depth_slope(model_manifest)
    return FreeBC(bc_type="FREE", vector=geom_asset, value=slope)


def derive_outflow_polygon(model_manifest: ModelManifest) -> str:
    """Estimate an acceptable downstream outflow area for a reach."""
    # Load geometries
    resolved_domain_path = ASSET_CACHE.materialize_path(model_manifest.assets.domain)
    resolved_centerline_path = ASSET_CACHE.materialize_path(
        model_manifest.assets.centerline
    )
    domain_gdf = gpd.read_file(resolved_domain_path)
    centerline_gdf = gpd.read_file(resolved_centerline_path)
    centerline_geom = ensure_linestring(centerline_gdf.geometry.iloc[0])
    domain_geom = domain_gdf.geometry.iloc[0]

    # Generate ray for lower 50% of centerline: chord from midpoint to downstream end
    mid_pt = centerline_geom.interpolate(0.5, normalized=True)
    ds_pt = Point(centerline_geom.coords[-1])
    dx = ds_pt.x - mid_pt.x
    dy = ds_pt.y - mid_pt.y
    mag = math.sqrt(dx**2 + dy**2)
    dx, dy = dx / mag, dy / mag  # downstream unit vector
    perp_x, perp_y = -dy, dx  # lateral unit vector

    # Offset (positive and negative) ray by 10x bieger bankfull width
    bankfull_w = bieger_bankfull_width(model_manifest.properties.drainage_area_sqkm)
    offset = 10 * bankfull_w
    bounds = domain_geom.bounds
    scale = 2 * math.sqrt((bounds[2] - bounds[0]) ** 2 + (bounds[3] - bounds[1]) ** 2)

    # Project 2 offsets and centerline ray until they hit the domain edge:
    # build a strip polygon spanning the domain, bounded by the two offset rays
    strip = Polygon(
        [
            (
                mid_pt.x + perp_x * offset - dx * scale,
                mid_pt.y + perp_y * offset - dy * scale,
            ),
            (
                mid_pt.x + perp_x * offset + dx * scale,
                mid_pt.y + perp_y * offset + dy * scale,
            ),
            (
                mid_pt.x - perp_x * offset + dx * scale,
                mid_pt.y - perp_y * offset + dy * scale,
            ),
            (
                mid_pt.x - perp_x * offset - dx * scale,
                mid_pt.y - perp_y * offset - dy * scale,
            ),
        ]
    )

    # Clip domain to segment in between rays, using centerline to identify the appropriate half:
    # the downstream half lies beyond mid_pt in the (dx, dy) direction
    ds_half = Polygon(
        [
            (mid_pt.x + perp_x * 2 * scale, mid_pt.y + perp_y * 2 * scale),
            (mid_pt.x - perp_x * 2 * scale, mid_pt.y - perp_y * 2 * scale),
            (
                mid_pt.x - perp_x * 2 * scale + dx * 2 * scale,
                mid_pt.y - perp_y * 2 * scale + dy * 2 * scale,
            ),
            (
                mid_pt.x + perp_x * 2 * scale + dx * 2 * scale,
                mid_pt.y + perp_y * 2 * scale + dy * 2 * scale,
            ),
        ]
    )
    outflow_zone = domain_geom.intersection(strip).intersection(ds_half)

    # Square buffer clipped domain edge by 2x cell resolution to arrive at outflow polygon
    outflow_edge = domain_geom.boundary.intersection(outflow_zone)
    grid_res = model_manifest.inputs.grid_resolution
    outflow_polygon = outflow_edge.buffer(2 * grid_res, cap_style=3)

    # Publish to dir containing centerline (href may be an S3 URI)
    centerline_href = model_manifest.assets.centerline.href
    parent_dir = centerline_href.rsplit("/", 1)[0]
    publish_path = f"{parent_dir}/outflow_area.geojson"
    result_gdf = gpd.GeoDataFrame(geometry=[outflow_polygon], crs=domain_gdf.crs)
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = str(Path(tmp_dir) / "outflow_area.geojson")
        result_gdf.to_file(tmp_path, driver="GeoJSON")
        copy_file(tmp_path, publish_path)

    return publish_path


def get_normal_depth_slope(model_manifest: ModelManifest) -> float:
    dem_array, endpoint_indices = load_dem_and_get_pt_indices(
        model_manifest.assets.centerline, model_manifest.assets.terrain
    )
    length = model_manifest.properties.length_m
    delta_e = abs(dem_array[endpoint_indices[0]] - dem_array[endpoint_indices[1]])
    slope = delta_e / length
    return max(slope, MINIMUM_REACH_SLOPE)


def _run_scenario(
    us_inflow: int,
    ds_bc: BoundaryCondition,
    model_manifest: ModelManifest,
    inputs: RunNDScenariosInputs,
    tmp_dir: Path,
    hot_start: Asset | None = None,
):
    # Define Configuration
    run_config = RunConfig(
        sim_time_seconds=inputs.max_simulation_length_seconds,
        save_interval_seconds=inputs.save_interval_seconds,
        volume_convergence_tolerance=inputs.volume_convergence_tolerance,
        allow_water_on_edges=inputs.allow_water_on_edges,
        max_simulation_wall_time_seconds=inputs.max_simulation_wall_time_seconds,
    )

    # Make boundary conditions
    inflow_bc = QFixBC(
        bc_type="QFIX", vector=model_manifest.assets.inflow_line, value=us_inflow
    )
    bcs = [inflow_bc, ds_bc]

    # Make run inputs
    run_scenario_inputs = RunScenarioInputs(
        domain=model_manifest.domain,
        grid_properties=model_manifest.properties.grid,
        terrain=model_manifest.assets.terrain,
        roughness=model_manifest.assets.roughness,
        boundary_conditions=bcs,
        hot_start=hot_start,
        run_config=run_config,
        base_out_dir=inputs.model_results_base_path,
        reach_id=model_manifest.reach_id,
        model_id=model_manifest.model_id,
        centerline=model_manifest.assets.centerline,
        run_identity_hash=get_run_identity_hash(),
    )
    working_dir = tmp_dir / run_scenario_inputs.scenario_dir_name

    # Execute run
    scenario_manifest = run_scenario(run_scenario_inputs, working_dir)

    return scenario_manifest


def compare_scenario_changes(
    trial_scenario: RunScenarioManifest,
    inputs: RunNDScenariosInputs,
    ref_scenario: RunScenarioManifest | None = None,
    force_accept: bool = False,
) -> AdaptiveStepComparisonResults:
    """Compare depth and extent changes between a reference and trial scenario to accept or reject the step."""
    if ref_scenario is None:
        return AdaptiveStepComparisonResults(
            ref_scenario_manifest=None,
            trial_scenario_manifest=trial_scenario.self_href,
            max_stage_diff=0,
            median_stage_diff=0,
            extent_diff=0,
            result="accept",
        )
    # Materialize assets
    resolved_ref_depth = ASSET_CACHE.materialize_path(ref_scenario.assets.depth)
    resolved_tria_depth = ASSET_CACHE.materialize_path(trial_scenario.assets.depth)

    # Load data
    ref_raster = Raster(resolved_ref_depth)
    trial_raster = Raster(resolved_tria_depth)
    ref_raster.data = np.clip(ref_raster.data, 0, None)
    trial_raster.data = np.clip(trial_raster.data, 0, None)
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
        max_depth_diff > inputs.adaptive_step_algorithm_max_stage_max_acceptable
        or median_depth_diff
        > inputs.adaptive_step_algorithm_median_stage_max_acceptable
        or extent_diff > inputs.adaptive_step_algorithm_extent_max_acceptable
    ):
        result = "reject_high"

    elif (
        inputs.adaptive_step_algorithm_max_stage_min_acceptable <= max_depth_diff
        or inputs.adaptive_step_algorithm_median_stage_min_acceptable
        <= median_depth_diff
        or inputs.adaptive_step_algorithm_extent_min_acceptable <= extent_diff
    ):
        result = "accept"
    else:
        result = "reject_low"

    if force_accept:
        result = "accept"

    return AdaptiveStepComparisonResults(
        ref_scenario_manifest=ref_scenario.self_href,
        trial_scenario_manifest=trial_scenario.self_href,
        max_stage_diff=max_depth_diff,
        median_stage_diff=median_depth_diff,
        extent_diff=extent_diff,
        result=result,
    )
