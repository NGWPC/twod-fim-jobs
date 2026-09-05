import copy
import logging
import math
import tempfile
from pathlib import Path
from typing import NamedTuple

import geopandas as gpd
from shapely.geometry import Point, Polygon

from twod_fim_jobs.consts import (
    MINIMUM_REACH_SLOPE,
    bieger_bankfull_width,
)
from twod_fim_jobs.hydraulic_solvers.common import publish_scenario, run_scenario
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
    CompletedScenario,
    FreeBC,
    QFixBC,
    RunConfig,
    RunScenarioInputs,
    RunScenarioManifest,
)
from twod_fim_jobs.models.warnings import WaterOnEdgeWarning
from twod_fim_jobs.utils.geospatial import (
    ensure_linestring,
    load_dem_and_get_pt_indices,
)
from twod_fim_jobs.utils.hashing import hash_file
from twod_fim_jobs.utils.storage import ASSET_CACHE, copy_file, read_json

logger = logging.getLogger(__name__)


class Reuse(NamedTuple):
    """How far the already-simulated scenarios carry the sweep on their own."""

    ref: CompletedScenario
    accepted: list[CompletedScenario]
    ceiling_q: int | None
    current: CompletedScenario
    # Width of the last accepted step, reference to trial. None if nothing was
    # accepted. This is measured evidence of what fits at this discharge, which
    # is why it becomes the next step rather than whatever the sweep was using
    # further down the curve.
    last_gap: int | None


def _reuse_finished_runs(
    ref: CompletedScenario,
    done: dict[int, CompletedScenario],
    inputs: RunNDScenariosInputs,
) -> Reuse:
    """Advance the reference as far as scenarios already run allow.

    A comparison is arithmetic over two manifests, so re-judging a finished
    scenario costs nothing. A discharge rejected as too high for one reference
    may sit squarely in the band for the next one, and it is already on disk.

    Response rises with discharge, so above the reference the outcomes fall in
    order: too low, then in band, then too high. The scan therefore runs downward
    from the highest simulated discharge and stops at the first verdict that is
    not reject_high, since that is either the furthest advance available or proof
    that nothing in memory clears the floor. Each reject_high seen on the way
    down lowers the ceiling.
    """
    accepted: list[CompletedScenario] = []
    last_gap: int | None = None
    while True:
        ref_q = ref.manifest.properties.us_discharge
        candidates = [q for q in sorted(done) if q > ref_q]
        if candidates:
            logger.info(
                f"Free pass: simulated {sorted(done)}; re-judging {candidates} "
                f"against reference {ref_q}"
            )
        best: CompletedScenario | None = None
        ceiling_q: int | None = None
        current = ref

        # Descending, because the furthest advance is what the pass is after: the
        # highest accepted discharge needs no evidence from anything below it.
        # Scanning down, every candidate above the band is reject_high, so the
        # first verdict that is not ends the scan -- an accept is the furthest
        # advance, and a reject_low means the band holds nothing at all.
        for q in reversed(candidates):
            logger.info(f"Re-judging simulated discharge {q} against reference {ref_q}")
            outcome = compare_scenario_changes(
                done[q].manifest, inputs, ref.manifest
            ).result
            if outcome == "reject_high":
                ceiling_q = q
                continue
            if outcome == "accept":
                best = done[q]
            else:
                current = done[q]
            break

        if best is None:
            return Reuse(ref, accepted, ceiling_q, current, last_gap)
        logger.info(
            "Accepting already-simulated discharge "
            f"{best.manifest.properties.us_discharge}"
        )
        last_gap = best.manifest.properties.us_discharge - ref_q
        accepted.append(best)
        ref = best


def _take_free_advances(
    ref: CompletedScenario,
    done: dict[int, CompletedScenario],
    inputs: RunNDScenariosInputs,
) -> tuple[Reuse, CompletedScenario | None]:
    """Run the free pass and publish whatever it accepts."""
    reuse = _reuse_finished_runs(ref, done, inputs)
    for scenario in reuse.accepted:
        publish_scenario(scenario)
    ceiling = done.get(reuse.ceiling_q) if reuse.ceiling_q is not None else None
    return reuse, ceiling


def _step_scale(
    comparison: AdaptiveStepComparisonResults,
    inputs: RunNDScenariosInputs,
    bracketed: bool,
) -> float:
    """How far to scale the discharge step, from the response it just produced.

    Each criterion asks for the factor that would land it on the middle of its
    band, and the smallest is taken. That one number serves both directions: a
    step judged too large has some criterion over its ceiling, whose factor is
    below one, and a step judged too small has every criterion under its floor,
    where the smallest factor is the least growth that reaches the band.

    Taking the minimum is what keeps the correction safe. Scaling by it moves the
    binding criterion to its midpoint and every other one to at or below its own,
    so nothing is pushed over a ceiling in the course of fixing something else.

    The shrink factor always bounds it, and the grow factor bounds it only while
    a ceiling exists. With a ceiling, an oversized proposal is discarded and
    bisected into the bracket anyway, so capping growth changes nothing. Without
    one the step is the whole search, and capping it throws away the measurement
    that was just paid for: the curve is concave, so the secant already asks for
    less growth than the reach needs, and clamping it compounds that error rather
    than guarding against it. Overshooting is self-correcting, since the trial
    becomes the ceiling and the next proposal is bisected back into range.
    """
    bands = (
        (comparison.max_depth_increase, inputs.ld_q_max_depth_increase_range),
        (comparison.median_depth_increase, inputs.ld_q_median_depth_increase_range),
        (
            comparison.flooded_area_prcnt_increase,
            inputs.ld_q_flooded_area_prcnt_increase_range,
        ),
    )
    scales = [
        (low + high) / 2 / measured for measured, (low, high) in bands if measured > 0
    ]
    if not scales:
        return inputs.adaptive_step_algorithm_grow_factor
    scale = max(min(scales), inputs.adaptive_step_algorithm_shrink_factor)
    if bracketed:
        return min(scale, inputs.adaptive_step_algorithm_grow_factor)
    return scale


def _next_trial_q(
    current_q: int, delta: int, ceiling_q: int | None, min_delta_q: int
) -> int | None:
    """The next discharge worth simulating, or None when the bracket has closed.

    A reject_high at ceiling_q proves, by monotonicity, that every q at or above
    it is also too high for this reference. Proposing one costs a simulation to
    learn what is already known, so proposals are bisected into the bracket
    instead. None means the bracket holds no untried discharge.
    """
    proposal = current_q + delta
    if ceiling_q is not None and proposal >= ceiling_q:
        # Bisect, but never below the smallest step allowed: a bracket narrower
        # than twice min_delta_q still holds discharges worth trying, and giving
        # up on them means taking the ceiling run and a step over the band.
        proposal = max((current_q + ceiling_q) // 2, current_q + min_delta_q)
        if proposal >= ceiling_q:
            return None
    if proposal - current_q < min_delta_q:
        return None
    return proposal


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
        publish_scenario(ref_scenario)
        current_scenario = ref_scenario
        scenario_comparison = compare_scenario_changes(
            current_scenario.manifest, inputs, None
        )
        results = RunNDScenariosResult(
            scenario_comparison_results=[scenario_comparison], warnings=[]
        )
        # Every scenario simulated this run, published or not. Rejections are
        # kept because the next reference may accept them, and re-judging one
        # costs no simulation.
        done: dict[int, CompletedScenario] = {
            ref_scenario.manifest.properties.us_discharge: ref_scenario
        }
        ceiling_q: int | None = None
        ceiling_scenario: CompletedScenario | None = None
        q_trial = current_scenario.manifest.properties.us_discharge + delta_us_discharge

        while q_trial < inputs.max_upstream_inflow:
            logger.info(
                f"State: reference={ref_scenario.manifest.properties.us_discharge} "
                f"position={current_scenario.manifest.properties.us_discharge} "
                f"ceiling={ceiling_q} step={delta_us_discharge}"
            )
            logger.info(f"Evaluating trial discharge {q_trial}")

            trial_scenario = _run_scenario(
                q_trial,
                downstream_bc,
                model_manifest,
                inputs,
                tmp_dir,
                hot_start=current_scenario.depth,
            )

            if trial_scenario.manifest.properties.termination_condition == "edge_error":
                logger.error("Aborting adaptive step algorithm for edge error")
                results.warnings.append(WaterOnEdgeWarning())
                return results

            done[q_trial] = trial_scenario
            scenario_comparison = compare_scenario_changes(
                trial_scenario.manifest, inputs, ref_scenario.manifest
            )
            results.scenario_comparison_results.append(scenario_comparison)

            if scenario_comparison.result == "reject_high":
                logger.info(f"Rejecting trial discharge {q_trial}: high")
                ceiling_q, ceiling_scenario = q_trial, trial_scenario
                delta_us_discharge = _scale_delta(
                    delta_us_discharge,
                    _step_scale(scenario_comparison, inputs, ceiling_q is not None),
                )

            elif scenario_comparison.result == "accept":
                logger.info(f"Accepting trial discharge {q_trial}")
                publish_scenario(trial_scenario)
                # The width that earned the verdict is reference to trial, which
                # is not the step when the proposal was bisected or the position
                # had moved. That width is what fits here, so it becomes the step.
                delta_us_discharge = (
                    q_trial - ref_scenario.manifest.properties.us_discharge
                )
                ref_scenario = current_scenario = trial_scenario
                ceiling_q, ceiling_scenario = None, None
                reuse, ceiling_scenario = _take_free_advances(
                    ref_scenario, done, inputs
                )
                ref_scenario, current_scenario = reuse.ref, reuse.current
                ceiling_q = reuse.ceiling_q
                if reuse.last_gap is not None:
                    delta_us_discharge = reuse.last_gap

            elif scenario_comparison.result == "reject_low":
                logger.info(f"Rejecting trial discharge {q_trial}: low")
                current_scenario = trial_scenario
                delta_us_discharge = _scale_delta(
                    delta_us_discharge,
                    _step_scale(scenario_comparison, inputs, ceiling_q is not None),
                )

            delta_us_discharge = max(
                inputs.adaptive_step_min_delta_q, delta_us_discharge
            )
            current_q = current_scenario.manifest.properties.us_discharge
            # An unbracketed secant off a barely-moved trial can ask for a jump
            # that would clear the rest of the range in one go. Half of what is
            # left still leaves room to sample above this point. The step itself
            # is left alone; only this proposal is bounded.
            step = min(
                delta_us_discharge,
                max(
                    inputs.adaptive_step_min_delta_q,
                    (inputs.max_upstream_inflow - current_q) // 2,
                ),
            )
            next_q = _next_trial_q(
                current_q, step, ceiling_q, inputs.adaptive_step_min_delta_q
            )

            if next_q is None and ceiling_scenario is not None:
                # No untried discharge is left below the ceiling, so the smallest
                # step that clears the band is the ceiling run itself. Taking it
                # keeps the library from growing denser than it was asked to be.
                logger.info(
                    f"Bracket closed; accepting {ceiling_q} as the smallest step"
                )
                publish_scenario(ceiling_scenario)
                delta_us_discharge = (
                    ceiling_scenario.manifest.properties.us_discharge
                    - ref_scenario.manifest.properties.us_discharge
                )
                ref_scenario = current_scenario = ceiling_scenario
                reuse, ceiling_scenario = _take_free_advances(
                    ref_scenario, done, inputs
                )
                ref_scenario, current_scenario = reuse.ref, reuse.current
                ceiling_q = reuse.ceiling_q
                if reuse.last_gap is not None:
                    delta_us_discharge = reuse.last_gap
                current_q = current_scenario.manifest.properties.us_discharge
                next_q = current_q + delta_us_discharge

            q_trial = next_q if next_q is not None else inputs.max_upstream_inflow

        trial_scenario = _run_scenario(
            inputs.max_upstream_inflow,
            downstream_bc,
            model_manifest,
            inputs,
            tmp_dir,
            hot_start=current_scenario.depth,
        )
        publish_scenario(trial_scenario)
        scenario_comparison = compare_scenario_changes(
            trial_scenario.manifest, inputs, ref_scenario.manifest, force_accept=True
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
    return run_scenario(run_scenario_inputs, working_dir)


def compare_scenario_changes(
    trial_scenario: RunScenarioManifest,
    inputs: RunNDScenariosInputs,
    ref_scenario: RunScenarioManifest | None = None,
    force_accept: bool = False,
    log_results: bool = True,
) -> AdaptiveStepComparisonResults:
    """Compare a trial scenario against a reference to accept or reject the step."""
    if ref_scenario is None:
        return AdaptiveStepComparisonResults(
            ref_scenario_manifest=None,
            trial_scenario_manifest=trial_scenario.self_href,
            max_depth_increase=0,
            median_depth_increase=0,
            flooded_area_prcnt_increase=0,
            result="accept",
        )
    ref, trial = ref_scenario.properties, trial_scenario.properties

    max_depth_increase = trial.max_depth - ref.max_depth
    median_depth_increase = trial.median_depth - ref.median_depth
    flooded_area_prcnt_increase = (
        (trial.flooded_area - ref.flooded_area) / ref.flooded_area * 100
        if ref.flooded_area > 0
        else 0.0
    )

    max_depth_lo, max_depth_hi = inputs.ld_q_max_depth_increase_range
    median_lo, median_hi = inputs.ld_q_median_depth_increase_range
    area_lo, area_hi = inputs.ld_q_flooded_area_prcnt_increase_range

    # reject_high takes priority: any criterion over its ceiling means the step was too large
    if (
        max_depth_increase > max_depth_hi
        or median_depth_increase > median_hi
        or flooded_area_prcnt_increase > area_hi
    ):
        result = "reject_high"

    elif (
        max_depth_lo <= max_depth_increase
        or median_lo <= median_depth_increase
        or area_lo <= flooded_area_prcnt_increase
    ):
        result = "accept"
    else:
        result = "reject_low"

    if force_accept:
        result = "accept"

    res = AdaptiveStepComparisonResults(
        ref_scenario_manifest=ref_scenario.self_href,
        trial_scenario_manifest=trial_scenario.self_href,
        max_depth_increase=max_depth_increase,
        median_depth_increase=median_depth_increase,
        flooded_area_prcnt_increase=flooded_area_prcnt_increase,
        result=result,
    )

    if log_results:
        logger.info(res.model_dump())

    return res
