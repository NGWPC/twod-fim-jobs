import logging
from pathlib import Path


from twod_fim_jobs.consts import (
    DEPTH_FILENAME,
    DEPTH_ZARR_FILENAME,
    INUNDATED_AREA_FILENAME,
    STL_FILENAME,
)
from twod_fim_jobs.models.solvers import PostProcessResult, RunScenarioInputs
from twod_fim_jobs.utils.geospatial import (
    compute_wse_contour,
    get_us_pt,
    raster_to_polygon,
    wd_files_to_zarr,
    wd_to_cog,
)
from twod_fim_jobs.utils.storage import ASSET_CACHE

logger = logging.getLogger(__name__)


def post_process_lisflood(
    run_scenario_inputs: RunScenarioInputs, working_dir: Path
) -> PostProcessResult:
    """Post-process a LISFLOOD output directory into a COG and a flood polygon GeoJSON."""
    logger.info(f"Post-processing lisflood model at {working_dir}")
    # Materialize assets
    resolved_dem = ASSET_CACHE.materialize_path(run_scenario_inputs.terrain)

    # Define output paths
    depth_path = working_dir / DEPTH_FILENAME
    inun_path = working_dir / INUNDATED_AREA_FILENAME
    stl_path = working_dir / STL_FILENAME

    # Get wd files
    wd_files = sorted(working_dir.glob(f"{DEFAULT_RESROOT_LISFLOOD}-????.wd"))
    if not wd_files:
        raise FileNotFoundError(f"No .wd files found in {working_dir}")

    # Process
    last_wd = wd_files[-1]
    wd_to_cog(last_wd, depth_path)
    raster_to_polygon(depth_path, inun_path)
    us_point = get_us_pt(run_scenario_inputs.centerline)
    wse_value = compute_wse_contour(
        resolved_dem, depth_path, us_point, stl_path, clip_poly=inun_path
    )
    if run_scenario_inputs.run_config.save_zarr:
        zarr_path = working_dir / DEPTH_ZARR_FILENAME
        wd_files_to_zarr(wd_files, zarr_path, resolved_dem)
    else:
        zarr_path = None

    return PostProcessResult(
        depth_path=depth_path,
        inundation_polygon_path=inun_path,
        stl_path=stl_path,
        nominal_wse=wse_value,
        sim_time=run_scenario_inputs.run_config.save_interval_seconds * len(wd_files),
        zarr_path=zarr_path,
    )
