import logging
from pathlib import Path


from pydantic import BaseModel
from shapely import Point

from twod_fim_jobs.consts import (
    DEPTH_FILENAME,
    DEPTH_ZARR_FILENAME,
    INUNDATED_AREA_FILENAME,
    STL_FILENAME,
)
from twod_fim_jobs.utils.geospatial import (
    compute_wse_contour,
    raster_to_polygon,
    wd_files_to_zarr,
    wd_to_cog,
)

logger = logging.getLogger(__name__)


class PostProcessResult(BaseModel):
    depth_path: Path
    inundation_polygon_path: Path
    stl_path: Path
    nominal_wse: float
    sim_time: float
    zarr_path: Path | None


def post_process_lisflood(
    out_dir: Path,
    us_point: Point,
    dem_path: Path,
    save_zarr: bool,
    save_interval: float,
) -> PostProcessResult:
    """Post-process a LISFLOOD output directory into a COG and a flood polygon GeoJSON."""
    logger.info(f"Post-processing lisflood model at {out_dir}")
    # Define output paths
    depth_path = out_dir / DEPTH_FILENAME
    inun_path = out_dir / INUNDATED_AREA_FILENAME
    stl_path = out_dir / STL_FILENAME

    # Get wd files
    stem = out_dir.name
    wd_files = sorted(out_dir.glob(f"{stem}-????.wd"))
    if not wd_files:
        raise FileNotFoundError(f"No .wd files found in {out_dir}")

    # Process
    last_wd = wd_files[-1]
    wd_to_cog(last_wd, depth_path)
    raster_to_polygon(depth_path, inun_path)
    wse_value = compute_wse_contour(
        dem_path, depth_path, us_point, stl_path, clip_poly=inun_path
    )
    if save_zarr:
        zarr_path = out_dir / DEPTH_ZARR_FILENAME
        wd_files_to_zarr(wd_files, zarr_path, dem_path)
    else:
        zarr_path = None

    return PostProcessResult(
        depth_path=depth_path,
        inundation_polygon_path=inun_path,
        stl_path=stl_path,
        nominal_wse=wse_value,
        sim_time=save_interval * len(wd_files),
        zarr_path=zarr_path,
    )
