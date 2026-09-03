import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import threading
import time

import numpy as np

from twod_fim_jobs.consts import DEFAULT_RESROOT_LISFLOOD, STABILITY_WAIT
from twod_fim_jobs.models.solvers import (
    TerminationCondition,
    BoundaryCheckResult,
    ConvergenceResult,
)
from twod_fim_jobs.models.solvers import RunScenarioInputs, SolveScenarioResults
from twod_fim_jobs.utils.geospatial import Raster, load_dem_and_get_pt_indices

logger = logging.getLogger(__name__)

### METHODS ###


def solve_scenario(
    config_path: Path, run_scenario_inputs: RunScenarioInputs, working_dir: Path
) -> SolveScenarioResults:
    process = run_lisflood(config_path)

    with ThreadPoolExecutor(max_workers=1) as executor:
        watcher_future = executor.submit(
            watch_run, process, run_scenario_inputs, working_dir
        )
        process.wait()  # watcher will terminate early if converged
        watcher_results = watcher_future.result()

    return watcher_results


def run_lisflood(parfile_path: Path, pipe_out_logs: bool = True) -> subprocess.Popen:
    process = subprocess.Popen(
        ["lisflood", str(parfile_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    logger.info("lisflood started (pid=%d) for %s", process.pid, parfile_path)

    if pipe_out_logs:

        def _stream(pipe, log_fn):
            for line in pipe:
                log_fn(line.rstrip())
            pipe.close()

        threading.Thread(
            target=_stream, args=(process.stdout, logger.info), daemon=True
        ).start()
        threading.Thread(
            target=_stream, args=(process.stderr, logger.warning), daemon=True
        ).start()
    return process


def watch_run(
    proc: subprocess.Popen, run_scenario_inputs: RunScenarioInputs, working_dir: Path
) -> SolveScenarioResults:
    seen: set[Path] = set()
    prev_array: np.ndarray | None = None
    metric_log = []

    dem_array, endpoint_indices = load_dem_and_get_pt_indices(
        run_scenario_inputs.centerline, run_scenario_inputs.terrain
    )

    running = True
    new_files = sorted(
        set(working_dir.glob(f"{DEFAULT_RESROOT_LISFLOOD}-????.wd")).difference(seen)
    )
    t0 = time.perf_counter()
    elapsed_wall_time = 0
    termination_condition = TerminationCondition.MAX_SIMULATION_TIME

    while running or len(new_files) > 0:
        if not new_files:
            time.sleep(0.5)
        for p in new_files:
            if not _is_stable(p):
                # break instead of continue so that we don't somehow get out of order
                break
            metrics, prev_array = check_status(
                p,
                prev_array,
                run_scenario_inputs.inflow,
                run_scenario_inputs.run_config.save_interval_seconds,
                endpoint_indices,
                dem_array,
                running,
            )
            seen.add(p)
            metric_log.append(metrics)

            converged = _is_converged(
                metrics, run_scenario_inputs.run_config.volume_convergence_tolerance
            )
            edge_error = (
                metrics.boundary_check is not None
                and metrics.boundary_check.error is not None
                and not run_scenario_inputs.run_config.allow_water_on_edges
            )
            if converged or edge_error:
                if converged:
                    termination_condition = TerminationCondition.VOLUME_CONVERGENCE
                else:
                    termination_condition = TerminationCondition.EDGE_ERROR
                terminate_run(proc)
                running = False
        elapsed_wall_time = time.perf_counter() - t0
        if running and proc.poll() is not None:
            if proc.returncode != 0:
                raise subprocess.CalledProcessError(proc.returncode, proc.args)
            running = False
            termination_condition = TerminationCondition.MAX_SIMULATION_TIME
        elif (
            running
            and elapsed_wall_time
            > run_scenario_inputs.run_config.max_simulation_wall_time_seconds
        ):
            terminate_run(proc)
            running = False
            termination_condition = TerminationCondition.MAX_WALL_TIME
        new_files = sorted(
            set(working_dir.glob(f"{DEFAULT_RESROOT_LISFLOOD}-????.wd")).difference(
                seen
            )
        )
    return SolveScenarioResults(
        convergence_results=metric_log,
        termination_condition=termination_condition,
        wall_time=elapsed_wall_time,
    )


def check_status(
    path: Path,
    prev_array: np.ndarray | None,
    inflow: float | None = None,
    save_interval_sec: float | None = None,
    endpoint_indices: tuple[tuple[int, int], tuple[int, int]] | None = None,
    dem_array: np.ndarray | None = None,
    model_running: bool = True,
) -> tuple[ConvergenceResult, np.ndarray]:
    # Load current raster
    cur_raster = Raster(path)
    cur_array = cur_raster.data
    resolution = cur_raster.resolution
    if prev_array is None:
        return (
            ConvergenceResult(
                volume_convergence=1, boundary_check=None, model_running=model_running
            ),
            cur_array.copy(),
        )

    # Calculate convergence
    if inflow is not None and save_interval_sec is not None:
        relative_change = calculate_volume_convergence(
            cur_array, prev_array, inflow, save_interval_sec, resolution
        )
    else:
        relative_change = np.inf

    # Check if water on invalid edge cell(s)
    boundary = None
    if endpoint_indices is not None and dem_array is not None:
        boundary = check_boundary_errors(cur_array, endpoint_indices, dem_array)

    convergence = ConvergenceResult(
        volume_convergence=relative_change,
        boundary_check=boundary,
        model_running=model_running,
    )
    return (convergence, cur_array.copy())


def calculate_volume_convergence(
    cur_array: np.ndarray,
    prev_array: np.ndarray,
    inflow: float,
    save_interval_sec: float,
    resolution: float,
) -> float:
    v1 = np.nansum(cur_array[cur_array > 0]) * (resolution**2)
    v2 = np.nansum(prev_array[prev_array > 0]) * (resolution**2)
    delta_volume = v1 - v2
    relative_change = delta_volume / (inflow * save_interval_sec)
    return relative_change


def check_boundary_errors(
    cur_array: np.ndarray,
    endpoint_indices: tuple[tuple[int, int], tuple[int, int]],
    dem_array: np.ndarray,
) -> BoundaryCheckResult | None:
    wse_array = cur_array + dem_array
    wse_array[cur_array == 0] = np.nan
    wse_0 = float(wse_array[endpoint_indices[0]])
    wse_1 = float(wse_array[endpoint_indices[1]])
    if np.isnan(wse_1):
        # Water not yet at downstream end.  Cannot conclude anything yet
        return None

    top = wse_array[0, :]
    bottom = wse_array[-1, :]
    left = wse_array[1:-1, 0]
    right = wse_array[1:-1, -1]
    edge_cells = np.concatenate([top, bottom, left, right])

    lo, hi = min(wse_0, wse_1), max(wse_0, wse_1)
    in_range = (edge_cells >= lo) & (edge_cells <= hi)  # TODO: <= and >=
    wetted = ~np.isnan(edge_cells)

    violating_wse = edge_cells[in_range]
    worst = (
        float(violating_wse[np.nanargmax(np.abs(violating_wse - wse_0))])
        if violating_wse.size
        else None
    )

    # margin: for wetted non-violating cells, distance to nearest range boundary
    non_violating_wetted = edge_cells[wetted & ~in_range]
    if non_violating_wetted.size:
        margin = float(
            np.min(
                np.minimum(
                    np.abs(non_violating_wetted - lo), np.abs(non_violating_wetted - hi)
                )
            )
        )
    else:
        margin = None

    def _count(arr: np.ndarray) -> int:
        return int(np.sum((arr >= lo) & (arr <= hi)))

    error = None
    if violating_wse.size:
        error = "boundary error: edge cell WSE is between endpoint WSE values"
        logger.error(error)

    return BoundaryCheckResult(
        wse_0=wse_0,
        wse_1=wse_1,
        wse_range=float(wse_0 - wse_1),
        n_wetted_edge_cells=int(np.sum(wetted)),
        n_violating_edge_cells=int(violating_wse.size),
        n_violating_top=_count(top),
        n_violating_bottom=_count(bottom),
        n_violating_left=_count(left),
        n_violating_right=_count(right),
        worst_violating_wse=worst,
        closest_edge_margin=margin,
        error=error,
    )


def terminate_run(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()


def _is_converged(
    metrics: ConvergenceResult, convergence_tolerance: float | None
) -> bool:
    if convergence_tolerance is None:
        return False
    if metrics.volume_convergence < convergence_tolerance:
        return True
    return False


def _is_stable(path: Path) -> bool:
    try:
        s1 = path.stat().st_size
        if s1 == 0:
            return False
        time.sleep(STABILITY_WAIT)
        return path.stat().st_size == s1
    except OSError:
        return False


def wait_for_scenario(process: subprocess.Popen) -> None:
    """Block until the process finishes and raise on non-zero exit."""
    process.wait()
    if process.returncode != 0:
        raise subprocess.CalledProcessError(process.returncode, process.args)
    logger.info("lisflood (pid=%d) completed successfully", process.pid)
