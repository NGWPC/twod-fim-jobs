import copy
import logging
import math
import tempfile
from pathlib import Path
from collections.abc import Callable
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
    position: CompletedScenario


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
    that nothing in memory clears the floor.
    """
    accepted: list[CompletedScenario] = []
    while True:
        ref_q = ref.manifest.properties.us_discharge
        candidates = [q for q in sorted(done) if q > ref_q]
        if candidates:
            logger.info(
                f"Free pass: simulated {sorted(done)}; re-judging {candidates} "
                f"against reference {ref_q}"
            )
        best: CompletedScenario | None = None
        position = ref

        for q in reversed(candidates):
            logger.info(f"Re-judging simulated discharge {q} against reference {ref_q}")
            outcome = compare_scenario_changes(
                done[q].manifest, inputs, ref.manifest
            ).result
            if outcome == "reject_high":
                continue
            if outcome == "accept":
                best = done[q]
            else:
                position = done[q]
            break

        if best is None:
            return Reuse(ref, accepted, position)
        logger.info(
            "Accepting already-simulated discharge "
            f"{best.manifest.properties.us_discharge}"
        )
        accepted.append(best)
        ref = best


def _take_free_advances(
    ref: CompletedScenario,
    done: dict[int, CompletedScenario],
    inputs: RunNDScenariosInputs,
    publish: Callable[[CompletedScenario], None],
) -> Reuse:
    """Run the free pass and publish whatever it accepts."""
    reuse = _reuse_finished_runs(ref, done, inputs)
    for scenario in reuse.accepted:
        publish(scenario)
    return reuse


class Measure(NamedTuple):
    """One acceptance criterion, and the response curve it is read from.

    `values` are ABSOLUTE readings aligned with the sweep's sorted discharges,
    not increases. The band is applied to the reference's own reading, which is
    what makes the curve directly solvable for the next discharge.
    """

    name: str
    values: list[float]
    floor: float
    ceiling: float
    # Flooded area's band is a percentage of the reference's area; the depth
    # bands are absolute metres.
    relative: bool


def _monotone(values: list[float]) -> list[float]:
    """A running maximum: more water cannot mean less flooding.

    Small measurement wobbles flatten out, and a criterion that genuinely falls
    -- median depth does, when water spreads over shallow ground -- becomes flat
    instead. A flat stretch carries no information, which is the honest answer:
    a criterion moving downward can never satisfy a band asking for an increase.
    """
    out: list[float] = []
    high = -math.inf
    for value in values:
        high = max(high, value)
        out.append(high)
    return out


def _crossing(qs: list[int], values: list[float], target: float) -> float | None:
    """The discharge at which the curve first reaches `target`.

    Straight segments between simulated points, so a reading is exact at every
    discharge actually run. Above the highest point the last segment's slope is
    carried on -- these curves bend gently downward, so that always promises
    slightly more response than the reach delivers, and the sweep aims a little
    short rather than overshooting.

    None when the curve never gets there: fewer than two points to draw a line
    from, or a flat top that goes nowhere.
    """
    for i in range(len(qs) - 1):
        low, high = values[i], values[i + 1]
        if high > low and low <= target <= high:
            span = (target - low) / (high - low)
            return qs[i] + (qs[i + 1] - qs[i]) * span
    if len(qs) < 2 or target <= values[-1]:
        return None
    slope = (values[-1] - values[-2]) / (qs[-1] - qs[-2])
    if slope <= 0:
        return None
    return qs[-1] + (target - values[-1]) / slope


def _curves(
    done: dict[int, CompletedScenario], inputs: RunNDScenariosInputs
) -> tuple[list[int], list[Measure]]:
    """The three response curves, assembled from every scenario simulated."""
    qs = sorted(done)
    props = [done[q].manifest.properties for q in qs]
    return qs, [
        Measure(
            "max_depth",
            _monotone([p.max_depth for p in props]),
            *inputs.ld_q_max_depth_increase_range,
            False,
        ),
        Measure(
            "median_depth",
            _monotone([p.median_depth for p in props]),
            *inputs.ld_q_median_depth_increase_range,
            False,
        ),
        Measure(
            "flooded_area",
            _monotone([p.flooded_area for p in props]),
            *inputs.ld_q_flooded_area_prcnt_increase_range,
            True,
        ),
    ]


def _acceptance_window(
    qs: list[int], measures: list[Measure], ref_q: int
) -> tuple[float, float] | None:
    """The discharges at which a trial against `ref_q` would be accepted.

    Each criterion is read off its curve for where the change from the reference
    reaches its floor and where it reaches its ceiling. Acceptance needs only one
    criterion at its floor, so the window opens at the EARLIEST floor; it needs
    every criterion under its ceiling, so it closes at the EARLIEST ceiling.

    The window is never empty. The criterion that reaches a floor first must do
    so before any criterion reaches a ceiling, because its own ceiling comes
    later still.

    None means no criterion reaches a floor at any discharge -- the curves have
    gone flat, and there is nothing left to place below the top of the range.
    """
    index = qs.index(ref_q)
    starts: list[float] = []
    ends: list[float] = []
    for measure in measures:
        base = measure.values[index]
        if measure.relative:
            floor, ceiling = (
                base * (1 + measure.floor / 100),
                base * (1 + measure.ceiling / 100),
            )
        else:
            floor, ceiling = base + measure.floor, base + measure.ceiling
        opens = _crossing(qs, measure.values, floor)
        closes = _crossing(qs, measure.values, ceiling)
        if opens is not None:
            starts.append(opens)
        if closes is not None:
            ends.append(closes)
    if not starts:
        return None
    return min(starts), min(ends) if ends else math.inf


class Proposal(NamedTuple):
    """The next discharge to simulate, and what the window said about it."""

    q: int
    # The window landed at or beyond the top of the range: nothing further fits.
    at_max: bool
    # No grid line falls inside the window at all: the band asks for a step
    # finer than the axis allows. The next line above the reference is run and
    # kept WHATEVER it says, because there is nothing else to try -- taking the
    # line below the band leaves a near-duplicate, which is cheaper than the
    # hole that skipping it would leave.
    forced: bool


def _propose(
    done: dict[int, CompletedScenario],
    inputs: RunNDScenariosInputs,
    ref_q: int,
    position_q: int,
) -> Proposal:
    """Where the curves say the next library entry belongs."""
    max_q = inputs.max_upstream_inflow
    qs, measures = _curves(done, inputs)
    window = _acceptance_window(qs, measures, ref_q)
    if window is None:
        return Proposal(max_q, True, False)
    opens, closes = window
    if math.isinf(closes):
        return Proposal(max_q, True, False)
    # Every scenario lands on the grid, so the window is satisfied by a LINE or
    # by nothing. Measured from the REFERENCE, never the position: the bands are
    # reference-to-trial, so the question has to be asked the same way.
    grid = inputs.q_grid_resolution
    lowest = max(math.ceil(opens / grid) * grid, ref_q + grid)
    highest = math.floor(closes / grid) * grid
    if lowest <= highest:
        # Aim at the line nearest the middle of the window, kept inside it.
        middle = round((opens + closes) / 2 / grid) * grid
        proposed, forced = min(max(middle, lowest), highest), False
    else:
        # The window falls between two lines. Nothing on the axis satisfies the
        # bands, so take the next line up and keep whatever it gives.
        proposed, forced = ref_q + grid, True
    logger.info(
        f"Window {opens:.1f} to {closes:.1f} against reference {ref_q}; proposing "
        f"{proposed} on a {grid} cms grid"
        + (" -- no line lands in the window, so it is taken as it comes"
           if forced else "")
    )
    if proposed >= max_q:
        return Proposal(max_q, True, False)
    return Proposal(proposed, False, forced)


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
        # The maximum is published the moment it is run and may later be
        # re-judged by the free pass, so publishing is made idempotent rather
        # than every call site having to know what is already uploaded.
        published: set[int] = set()

        def publish(scenario: CompletedScenario) -> None:
            q = scenario.manifest.properties.us_discharge
            if q not in published:
                published.add(q)
                publish_scenario(scenario)

        ref_scenario = _run_scenario(
            inputs.min_upstream_inflow, downstream_bc, model_manifest, inputs, tmp_dir
        )
        publish(ref_scenario)
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
        # The bootstrap. One point is not a curve, so the opening step is the
        # authored one; every step after that is read off the curves.
        q_trial = inputs.min_upstream_inflow + delta_us_discharge
        max_q = inputs.max_upstream_inflow
        max_done = False

        while True:
            if len(done) == 1:
                # One point is not a curve: use the authored opening step.
                at_max = q_trial >= max_q
                q_trial, forced = min(q_trial, max_q), False
            else:
                q_trial, at_max, forced = _propose(
                    done,
                    inputs,
                    ref_scenario.manifest.properties.us_discharge,
                    current_scenario.manifest.properties.us_discharge,
                )
            if at_max and max_done:
                break

            logger.info(
                f"State: reference={ref_scenario.manifest.properties.us_discharge} "
                f"position={current_scenario.manifest.properties.us_discharge} "
                f"trial={q_trial}"
                + (" (top of range)" if at_max else "")
                + (" (taken as it comes)" if forced else "")
            )
            if q_trial in done:
                # Reached by the forced branch, which proposes the next line up
                # without knowing whether it has been tried. Re-running would
                # cost a simulation to learn what is already in hand.
                logger.info(f"Reusing already-simulated discharge {q_trial}")
                trial_scenario = done[q_trial]
            else:
                trial_scenario = _run_scenario(
                    q_trial,
                    downstream_bc,
                    model_manifest,
                    inputs,
                    tmp_dir,
                    hot_start=current_scenario.depth,
                )
                if (trial_scenario.manifest.properties.termination_condition
                        == "edge_error"):
                    logger.error("Aborting adaptive step algorithm for edge error")
                    results.warnings.append(WaterOnEdgeWarning())
                    return results
                done[q_trial] = trial_scenario
            scenario_comparison = compare_scenario_changes(
                trial_scenario.manifest, inputs, ref_scenario.manifest
            )
            results.scenario_comparison_results.append(scenario_comparison)
            verdict = scenario_comparison.result

            if at_max:
                # The top of the range is a library entry whatever its verdict:
                # the KWSE stage grid is built from it. It is still judged, and
                # a reject_high means the response resumed somewhere below, so
                # the sweep carries on and fills the gap underneath.
                publish(trial_scenario)
                max_done = True
                if verdict != "reject_high":
                    break
                logger.info(
                    f"Maximum discharge {max_q} is too large a step; filling below it"
                )
            elif forced and verdict != "accept":
                # No line on the axis satisfies the bands, so this is as close
                # as this reach can be sampled. Keeping it is also what stops
                # the sweep proposing the same line forever.
                logger.info(
                    f"Taking {q_trial} despite {verdict}: "
                    f"no grid line satisfies the bands"
                )
                verdict = "accept"

            if verdict == "accept":
                logger.info(f"Accepting trial discharge {q_trial}")
                publish(trial_scenario)
                ref_scenario = current_scenario = trial_scenario
                reuse = _take_free_advances(ref_scenario, done, inputs, publish)
                ref_scenario, current_scenario = reuse.ref, reuse.position
            elif verdict == "reject_low":
                logger.info(f"Rejecting trial discharge {q_trial}: low")
                current_scenario = trial_scenario
            else:
                logger.info(f"Rejecting trial discharge {q_trial}: high")

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
