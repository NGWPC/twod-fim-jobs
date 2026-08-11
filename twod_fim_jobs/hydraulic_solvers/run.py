import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import threading
import time

import numpy as np

from twod_fim_jobs.consts import STABILITY_WAIT
from twod_fim_jobs.models.common import (
    TerminationCondition,
    BoundaryCheckResult,
    ConvergenceResult,
)
from twod_fim_jobs.utils.geospatial import Raster

logger = logging.getLogger(__name__)

### METHODS ###


def run_scenario(
    parfile_path: Path,
    inflow: float | None = None,
    convergence_tolerance: float | None = None,
    save_interval_sec: float | None = None,
    endpoint_indices: tuple[tuple[int, int], tuple[int, int]] | None = None,
    dem_array: np.ndarray | None = None,
    allow_water_on_edges: bool = False,
) -> tuple[list[ConvergenceResult], TerminationCondition, float]:
    _validate_convergence_params(inflow, save_interval_sec, convergence_tolerance)
    _validate_boundary_params(endpoint_indices, dem_array)
    process = run_lisflood(parfile_path)

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=1) as executor:
        watcher_future = executor.submit(
            watch_run,
            parfile_path.parent,
            process,
            inflow,
            save_interval_sec,
            convergence_tolerance,
            endpoint_indices,
            dem_array,
            allow_water_on_edges,
        )
        process.wait()  # watcher will terminate early if converged
        watcher_results = watcher_future.result()
    runtime_seconds = time.perf_counter() - t0

    return (*watcher_results, runtime_seconds)


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


def _validate_convergence_params(
    inflow: float | None,
    save_interval_sec: float | None,
    convergence_tolerance: float | None,
) -> None:
    params = {
        "inflow": inflow,
        "save_interval_sec": save_interval_sec,
        "convergence_tolerance": convergence_tolerance,
    }
    provided = {k for k, v in params.items() if v is not None}
    if provided and len(provided) != len(params):
        missing = sorted(set(params) - provided)
        raise ValueError(
            f"If any convergence parameter is provided, all must be provided. "
            f"Got: {sorted(provided)}, missing: {missing}"
        )


def _validate_boundary_params(
    endpoint_indices: tuple[tuple[int, int], tuple[int, int]] | None,
    dem_array: np.ndarray | None,
) -> None:
    params = {"endpoint_indices": endpoint_indices, "dem_array": dem_array}
    provided = {k for k, v in params.items() if v is not None}
    if provided and len(provided) != len(params):
        missing = sorted(set(params) - provided)
        raise ValueError(
            f"If any boundary parameter is provided, all must be provided. "
            f"Got: {sorted(provided)}, missing: {missing}"
        )


def watch_run(
    out_dir: Path,
    proc: subprocess.Popen,
    inflow: float | None,
    save_interval_sec: float | None,
    convergence_tolerance: float | None,
    endpoint_indices: tuple[tuple[int, int], tuple[int, int]] | None = None,
    dem_array: np.ndarray | None = None,
    allow_water_on_edges: bool = False,
) -> tuple[list[ConvergenceResult], TerminationCondition]:
    seen: set[Path] = set()
    prev_array: np.ndarray | None = None
    metric_log = []

    stem = out_dir.name
    running = True
    new_files = []
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
                inflow,
                save_interval_sec,
                endpoint_indices,
                dem_array,
                running,
            )
            seen.add(p)
            metric_log.append(metrics)

            converged = _is_converged(metrics, convergence_tolerance)
            edge_error = (
                metrics.boundary_check is not None
                and metrics.boundary_check.error is not None
                and not allow_water_on_edges
            )
            if converged or edge_error:
                if converged:
                    termination_condition = TerminationCondition.VOLUME_CONVERGENCE
                else:
                    termination_condition = TerminationCondition.EDGE_ERROR
                terminate_run(proc)
                running = False
        if running and proc.poll() is not None:
            running = False
            termination_condition = TerminationCondition.MAX_SIMULATION_TIME
        new_files = sorted(set(out_dir.glob(f"{stem}-????.wd")).difference(seen))
    return metric_log, termination_condition


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
        else float("nan")
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
        margin = float("nan")

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
