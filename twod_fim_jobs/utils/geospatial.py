from math import floor, ceil
import logging
import math
import shutil
from collections.abc import Iterable
from functools import cached_property
from pathlib import Path
from typing import Callable, cast, overload
from datetime import datetime

import geopandas as gpd
import numpy as np
import pandas as pd
import json
import xarray as xr
from numcodecs import Blosc
import rasterio.features
import rasterio.transform
from rasterio.features import shapes

import rasterio
import rasterio.errors
from rasterio.crs import CRS
from rasterio.enums import Resampling
from rasterio.transform import from_bounds
from rasterio.warp import reproject
from scipy.ndimage import gaussian_filter
from skimage import measure

from shapely import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPoint,
    Point,
    box,
)
from shapely.geometry.base import BaseGeometry
from shapely.geometry import shape, mapping
from shapely.ops import linemerge, unary_union

from affine import Affine

from twod_fim_jobs.consts import DA_FIELD, DEFAULT_EPSG_CODE, bieger_bankfull_width
from twod_fim_jobs.models.common import Asset
from twod_fim_jobs.models.build_model import Domain
from twod_fim_jobs.exceptions import (
    AnchorOutsideDomainError,
    DatasetUnavailableError,
    RasterProcessingError,
)

### CLASSES ###


class Raster:
    def __init__(self, raster_path: str | Path):
        self.raster_path = raster_path
        with rasterio.open(raster_path) as src:
            self.transform = src.transform
            self.nodata = src.nodata
            self.profile = dict(src.profile)  # plain dict so callers can mutate freely
            self.height = src.height
            self.width = src.width
            self.dtype = src.dtypes[0]

    @property
    def resolution(self) -> float:
        return self.transform.a

    @cached_property
    def data(self) -> np.ndarray:
        with rasterio.open(self.raster_path) as src:
            return src.read(1)

    @property
    def crs(self) -> CRS | None:
        return self.profile.get("crs")


### METHODS ###


@overload
def ensure_linestring(geom: gpd.GeoDataFrame) -> gpd.GeoDataFrame: ...
@overload
def ensure_linestring(geom: LineString | MultiLineString) -> LineString: ...
def ensure_linestring(
    geom: LineString | MultiLineString | gpd.GeoDataFrame,
) -> LineString | gpd.GeoDataFrame:
    """Coerce MultiLineString geometry to LineString by merging contiguous parts."""
    if isinstance(geom, gpd.GeoDataFrame):
        result = geom.copy()
        result.geometry = gpd.GeoSeries(geom.geometry.apply(ensure_linestring))
        return result
    if isinstance(geom, LineString):
        return geom
    merged = linemerge(geom)
    if not isinstance(merged, LineString):
        raise ValueError(
            "Cannot coerce MultiLineString to LineString: parts are not contiguous."
        )
    return merged


def perpendicular_line(
    base_line: LineString, at_point: Point, length: float
) -> LineString:
    """Create a perpendicular line of given length centered at a point on a base line."""
    # Get local direction of the base line near that point
    d = 0.001 * base_line.length  # small distance to estimate tangent
    p1 = base_line.interpolate(base_line.project(at_point) - d)
    p2 = base_line.interpolate(base_line.project(at_point) + d)

    # Compute the angle of the line at that point
    angle = math.atan2(p2.y - p1.y, p2.x - p1.x)

    # Perpendicular angle
    perp_angle = angle + math.pi / 2

    # Half-length offsets
    dx = (length / 2) * math.cos(perp_angle)
    dy = (length / 2) * math.sin(perp_angle)

    # Construct the perpendicular line centered at the point
    p_left = Point(at_point.x - dx, at_point.y - dy)
    p_right = Point(at_point.x + dx, at_point.y + dy)

    return LineString([p_left, p_right])


def make_inflow_line(
    reach: gpd.GeoDataFrame,
    us_mainstem: gpd.GeoDataFrame,
    bankfull_width_multiplier: float,
    walk_us_dist_pct: float,
) -> gpd.GeoDataFrame:
    """Create an inflow boundary line perpendicular to the reach at the upstream end."""
    inflow_width = (
        bieger_bankfull_width(float(reach[DA_FIELD].iloc[0]))
        * bankfull_width_multiplier
    )
    reach_geom = reach.geometry.iloc[0]
    if us_mainstem.empty:
        first_line = reach_geom.geoms[0] if hasattr(reach_geom, "geoms") else reach_geom
        us_bc_pt = Point(first_line.coords[0])
        inflow_geom = perpendicular_line(first_line, us_bc_pt, inflow_width)
    else:
        us_geom = us_mainstem.geometry.iloc[0]
        # Walk upstream a bit for u/s boundary condition
        walk_us_dist = us_geom.length * walk_us_dist_pct
        us_bc_pt = us_geom.interpolate(1 - walk_us_dist)
        inflow_geom = perpendicular_line(us_geom, us_bc_pt, inflow_width)
    return gpd.GeoDataFrame({"ind": [1]}, geometry=[inflow_geom], crs=reach.crs)


def get_line_intersections(geom_1: LineString, geom_2: LineString) -> list[Point]:
    """Return all intersection points between two geometries."""
    intersection = geom_1.intersection(geom_2)
    if intersection.is_empty:
        return []

    points = _intersection_points(intersection)

    # De-duplicate exact coordinate duplicates introduced by mixed geometry types.
    return list({point for point in points})


def _intersection_points(geom: BaseGeometry) -> list[Point]:
    """Flatten intersection geometry into intersection points."""
    if geom.is_empty:
        return []

    if isinstance(geom, Point):
        return [geom]

    if isinstance(geom, MultiPoint):
        return list(geom.geoms)

    if isinstance(geom, LineString):
        # If lines overlap, count overlap endpoints as intersections.
        boundary = geom.boundary
        if isinstance(boundary, MultiPoint):
            return list(boundary.geoms)
        if isinstance(boundary, Point):
            return [boundary]
        return []

    if isinstance(geom, (MultiLineString, GeometryCollection)):
        return _flatten_points(
            _intersection_points(sub_geom) for sub_geom in geom.geoms
        )

    return []


def _flatten_points(point_lists: Iterable[list[Point]]) -> list[Point]:
    """Flatten a sequence of point lists into a single list."""
    return [point for points in point_lists for point in points]


def build_model_domain(
    reach_cl: gpd.GeoDataFrame,
    other_geometries: gpd.GeoDataFrame,
    resolution: float,
    buffer_distance: float,
) -> Domain:
    """Build a model domain from a set of geometries."""
    # Define anchor as reach centroid
    anchor = reach_cl.centroid
    ax = anchor.x.iloc[0]
    ay = anchor.y.iloc[0]
    ax = floor(ax / resolution) * resolution
    ay = floor(ay / resolution) * resolution

    # Get bbox
    (xmin, ymin, xmax, ymax) = gpd.GeoDataFrame(
        pd.concat([reach_cl, other_geometries])
    ).total_bounds
    xmin -= buffer_distance
    ymin -= buffer_distance
    xmax += buffer_distance
    ymax += buffer_distance

    # Snap bbox to grid
    xmin = floor(xmin / resolution) * resolution
    ymin = floor(ymin / resolution) * resolution
    xmax = ceil(xmax / resolution) * resolution
    ymax = ceil(ymax / resolution) * resolution

    # Calculate offsets
    w = (ax - xmin) / resolution
    e = (xmax - ax) / resolution
    s = (ay - ymin) / resolution
    n = (ymax - ay) / resolution

    if n < 0 or s < 0 or e < 0 or w < 0:
        raise AnchorOutsideDomainError(
            f"Anchor ({ax}, {ay}) falls outside domain bbox ({xmin}, {ymin}, {xmax}, {ymax})"
        )

    # Return Domain object
    return Domain(bbox=(xmin, ymin, xmax, ymax), anchor=(ax, ay), offsets=(n, s, e, w))


def extract_raster(*args, **kwargs) -> Asset:
    """Wrap extraction to provide consistent error handling."""
    try:
        return _extract_raster(*args, **kwargs)
    except rasterio.errors.RasterioIOError as e:
        raise DatasetUnavailableError(str(e)) from e
    except Exception as e:
        raise RasterProcessingError(str(e)) from e


def _extract_raster(
    src_path: str | Path,
    out_path: str | Path,
    bbox: tuple[float, float, float, float],
    cols: int,
    rows: int,
    dst_crs: str,
    value_transform: Callable[[np.ndarray], np.ndarray] | None = None,
) -> Asset:
    """Extract/reproject a raster to a target grid and optionally transform values."""
    with rasterio.open(src_path) as src:
        dst_transform = from_bounds(
            *bbox,
            cols,
            rows,
        )

        profile = src.profile.copy()
        profile.update(
            {
                "driver": "GTiff",
                "width": cols,
                "height": rows,
                "transform": dst_transform,
                "crs": dst_crs,
                "dtype": "float32",
                "compress": "deflate",
                "predictor": 3,
                "tiled": True,
            }
        )

        # Reproject into memory
        data = np.empty(
            (rows, cols),
            dtype="float32",
        )

        reproject(
            source=rasterio.band(src, 1),
            destination=data,
            src_transform=src.transform,
            src_crs=src.crs,
            dst_transform=dst_transform,
            dst_crs=dst_crs,
        )

        # Optional pixel-value transformation
        if value_transform is not None:
            data = value_transform(data)

        # Write final raster once
        with rasterio.open(out_path, "w", **profile) as dst:
            dst.write(data, 1)

    return Asset.from_file(str(out_path), str(src_path), datetime.now())


def download_dem(
    src_path: str | Path,
    out_path: str | Path,
    bbox: tuple[float, float, float, float],
    cols: int,
    rows: int,
    dst_crs: str,
) -> Asset:
    """Extract and reproject a DEM to a target grid and CRS."""

    def noop(data):
        return data

    return extract_raster(src_path, out_path, bbox, cols, rows, dst_crs, noop)


def download_roughness(
    src_path: str | Path,
    out_path: str | Path,
    bbox: tuple[float, float, float, float],
    cols: int,
    rows: int,
    dst_crs: str,
    lulc_lookup: dict[int, float],
) -> Asset:
    """Extract and reproject a land-use raster to Manning roughness values on a target grid."""
    manning_lut = _make_lookup_array(lulc_lookup)
    return extract_raster(
        src_path,
        out_path,
        bbox,
        cols,
        rows,
        dst_crs,
        lambda x: manning_lut[x.astype(np.uint8)],
    )


def _make_lookup_array(
    lookup: dict[int, float],
    size: int = 256,
    fill_value: float = -9999,
) -> np.ndarray:
    """Convert a categorical lookup dictionary into a NumPy lookup array."""
    lut = np.full(size, fill_value, dtype="float32")

    for key, value in lookup.items():
        lut[key] = value

    return lut


def write_gdf_asset(
    gdf: gpd.GeoDataFrame, out_path: str | Path, source_url: str
) -> Asset:
    """Write a geodataframe to disk and return an Asset record."""
    gdf.to_file(out_path)

    return Asset.from_file(str(out_path), source_url, datetime.now())


def export_domain_gdfs(
    domain: Domain, crs: str
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Convert a Domain object into anchor point and extent polygon geodataframes."""
    ax, ay = domain.anchor
    xmin, ymin, xmax, ymax = domain.bbox
    n, s, e, w = domain.offsets

    # Single-point anchor geometry, plus coordinate columns for easy inspection.
    anchor_gdf = gpd.GeoDataFrame(
        {"x": [ax], "y": [ay]},
        geometry=[Point(ax, ay)],
        crs=crs,
    )

    # Domain extent polygon and metadata used in model identifiers.
    domain_gdf = gpd.GeoDataFrame(
        {
            "xmin": [xmin],
            "ymin": [ymin],
            "xmax": [xmax],
            "ymax": [ymax],
            "offset_n": [n],
            "offset_s": [s],
            "offset_e": [e],
            "offset_w": [w],
            "offset_str": [domain.offset_str],
        },
        geometry=[box(xmin, ymin, xmax, ymax)],
        crs=crs,
    )

    return anchor_gdf, domain_gdf


def wd_to_cog(wd_path: Path, out_path: Path) -> None:
    """Convert a LISFLOOD .wd ASCII raster to a COG, masking dry (zero) cells."""
    r = Raster(wd_path)
    data = r.data.copy()  # copy so the in-place mask below doesn't corrupt the cache
    profile = r.profile.copy()
    nd = r.nodata

    if profile.get("crs") is None:
        profile["crs"] = CRS.from_string("EPSG:5070")

    # Zero-depth cells are dry — treat as nodata
    data[data == 0] = nd

    for key in ("blockxsize", "blockysize", "tiled"):
        profile.pop(key, None)

    profile.update(
        driver="COG",
        compress="DEFLATE",
        nodata=nd,
        resampling=Resampling.bilinear,
    )

    with rasterio.open(out_path, "w", **profile) as w:
        w.write(data, 1)


def raster_to_polygon(raster_path: Path, out_path: Path) -> None:
    """Vectorize non-nodata raster cells into a polygon GeoJSON."""
    r = Raster(raster_path)

    mask = (r.data != r.nodata).astype(np.uint8)
    # rasterio.features.shapes uses the deprecated OGR 'Memory' driver internally
    _env_logger = logging.getLogger("rasterio._env")
    _prev_level = _env_logger.level
    _env_logger.setLevel(logging.ERROR)
    polys = [
        shape(geom) for geom, val in shapes(mask, transform=r.transform) if val == 1
    ]
    _env_logger.setLevel(_prev_level)
    if not polys:
        merged = None
    else:
        merged = unary_union(polys)

    geojson = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": mapping(merged),
                "properties": {},
            }
        ]
        if merged
        else [],
    }
    if r.crs is not None:
        geojson["crs"] = {"type": "name", "properties": {"name": r.crs.to_string()}}

    out_path.write_text(json.dumps(geojson))


def tif_to_asc(tif_path: Path) -> Path:
    """Convert a GeoTIFF to an Arc ASCII raster alongside the source file."""
    out_path = tif_path.with_suffix(".asc")
    src = Raster(tif_path)
    asc_profile = {
        "driver": "AAIGrid",
        "dtype": src.dtype,
        "width": src.width,
        "height": src.height,
        "count": 1,
        "crs": src.crs,
        "transform": src.transform,
        "nodata": src.nodata,
    }
    with rasterio.open(out_path, "w", **asc_profile) as dst:
        dst.write(src.data, 1)
    return out_path


def rasterize_geometry(
    geometry: BaseGeometry, rows: int, cols: int, transform: Affine
) -> list[tuple[float, float]]:
    """Rasterize a geometry into a mask and return coordinates of marked cells."""
    # Create an empty mask
    mask = np.zeros((rows, cols), dtype=np.uint8)

    # rasterio._features uses the deprecated OGR 'Memory' driver internally
    _env_logger = logging.getLogger("rasterio._env")
    _prev_level = _env_logger.level
    _env_logger.setLevel(logging.ERROR)
    rasterio.features.rasterize(
        [(geometry, 1)],
        out=mask,
        transform=transform,
        all_touched=True,
        dtype="uint8",
    )
    _env_logger.setLevel(_prev_level)

    # Get row/col indices where the line intersects
    sel_rows, sel_cols = np.where(mask == 1)

    # Convert row/col indices to map (x, y) coordinates
    xs, ys = rasterio.transform.xy(transform, sel_rows, sel_cols)
    return list(zip(xs, ys))


def nan_smooth(data: np.ndarray, sigma: int = 3) -> np.ndarray:
    """Smooth data with Gaussian filter, preserving NaN regions via inverse weighting."""
    data = data.copy()
    nan_mask = np.isnan(data)
    data[nan_mask] = 0
    w = np.ones_like(data)
    w[nan_mask] = 0
    v_filtered = gaussian_filter(data, sigma=sigma)
    w_filtered = gaussian_filter(w, sigma=sigma)
    return v_filtered / w_filtered


def smooth_raster_data(data: np.ndarray, pad_fraction: float = 0.25) -> np.ndarray:
    """Smooth raster data with Gaussian filters using padding to avoid edge effects."""
    # Pad out to avoid edge effects
    nrows, ncols = data.shape
    pad_rows = int(np.ceil(nrows * pad_fraction))
    pad_cols = int(np.ceil(ncols * pad_fraction))
    data = np.pad(
        data,
        pad_width=((pad_rows, pad_rows), (pad_cols, pad_cols)),
        mode="constant",
        constant_values=np.nan,
    )

    # Log valid data mask
    nan_mask = np.isnan(data)

    # Smooth
    smoothed = data.copy()

    # Initial coarse smooth to get downvalley gradient in nodata areas
    s = max(3, min(data.shape) // 4)
    smoothed = nan_smooth(smoothed, s)

    # Fine scale smooth (smooth out noise)
    smoothed[~nan_mask] = data[~nan_mask]  # Impute original non smooth values
    smoothed = nan_smooth(smoothed, 3)

    # Coarse smooth to reflect macro trends ()
    s = max(3, min(data.shape) // 20)
    smoothed = nan_smooth(smoothed, s)

    # reset pad
    smoothed = smoothed[pad_rows : pad_rows + nrows, pad_cols : pad_cols + ncols]

    return smoothed


def extract_contour(
    wse: np.ndarray, pt: Point, wse_transform: Affine
) -> tuple[MultiLineString, float]:
    """Extract the contour line at a given point's water surface elevation value."""
    # Get sample ind
    col, row = cast(tuple[float, float], ~wse_transform * (pt.x, pt.y))  # type: ignore[operator]
    col = np.floor(col).astype(int)
    row = np.floor(row).astype(int)
    contour_val = wse[row, col]

    # Make line
    contours = measure.find_contours(wse, level=contour_val)
    lines = []
    for c in contours:
        # c is (row, col) in pixel coordinates
        rows, cols = c[:, 0], c[:, 1]
        xs, ys = rasterio.transform.xy(wse_transform, rows, cols, offset="center")
        line = LineString(zip(xs, ys))
        if line.length > 0:
            lines.append(line)

    return MultiLineString(lines), float(contour_val)


def compute_wse_contour(
    dem_path: str | Path,
    depth_path: str | Path,
    wse_pt: Point,
    countour_path: str | Path,
    smoothed_raster_path: str | Path | None = None,
    clip_poly: Path | None = None,
    **kwargs,
) -> float:
    """Compute a water surface elevation contour from DEM and depth, optionally saving smoothed raster."""
    # Generate WSE grid
    dem = Raster(dem_path)
    depth = Raster(depth_path)

    inun_mask = (depth.data == depth.nodata) | (depth.data <= 0)
    clean_depth = np.where(inun_mask, np.nan, depth.data)

    wse_data = dem.data + clean_depth

    # Smooth and export
    smooth_wse = smooth_raster_data(wse_data, **kwargs)
    if smoothed_raster_path is not None:
        with rasterio.open(smoothed_raster_path, "w", **depth.profile) as src:
            src.write(smooth_wse, 1)

    # Extract contour
    contour, wse_val = extract_contour(smooth_wse, wse_pt, dem.transform)
    if clip_poly is not None:
        clip_geom = gpd.read_file(clip_poly).geometry.iloc[0]
        contour = contour.intersection(clip_geom)

    gdf = gpd.GeoDataFrame(
        {"wse": [wse_val]},
        geometry=[contour],
        crs=dem.profile["crs"],
    )
    if str(countour_path).endswith(".parquet"):
        gdf.to_parquet(countour_path)
    else:
        gdf.to_file(countour_path)

    return wse_val


def wd_files_to_zarr(
    wd_files: list[Path],
    zarr_path: Path,
    dem_path: Path,
    delete: bool = False,
    chunk_xy: int = 512,
    chunk_time: int = 1,
    crs: CRS | None = None,
) -> Path:
    """Write all .wd depth rasters in out_dir to a Zarr time-series store."""
    compressor = Blosc(cname="zstd", clevel=5, shuffle=Blosc.BITSHUFFLE)
    _chunk = {
        "compressor": compressor,
        "dtype": "float32",
        "chunks": (chunk_time, chunk_xy, chunk_xy),
    }
    encoding = {"depth": _chunk, "wse": _chunk}

    dem = Raster(dem_path)
    resolved_crs = crs or dem.crs or CRS.from_epsg(DEFAULT_EPSG_CODE)
    crs_wkt = resolved_crs.to_wkt() if resolved_crs is not None else None

    for i, fpath in enumerate(wd_files):
        r = Raster(fpath)
        t = r.transform
        xs = t.c + (np.arange(r.width) + 0.5) * t.a
        ys = t.f + (np.arange(r.height) + 0.5) * t.e
        depth = r.data.astype("float32")
        wse = np.where(depth > 0, dem.data.astype("float32") + depth, np.nan)
        depth = np.where(depth > 0, depth, np.nan)
        ds = xr.Dataset(
            {
                "depth": (["time", "y", "x"], depth[np.newaxis]),
                "wse": (["time", "y", "x"], wse[np.newaxis]),
            },
            coords={"time": [i], "y": ys, "x": xs},
        )
        if crs_wkt is not None:
            ds = ds.assign_coords(
                spatial_ref=xr.DataArray(
                    0, attrs={"crs_wkt": crs_wkt, "grid_mapping_name": "unknown"}
                )
            )
            ds["depth"].attrs["grid_mapping"] = "spatial_ref"
            ds["wse"].attrs["grid_mapping"] = "spatial_ref"

        if delete:
            fpath.unlink()

        if zarr_path.exists() and i > 0:
            for var in ds.data_vars:
                for k in ["add_offset", "scale_factor", "_FillValue", "missing_value"]:
                    ds[var].attrs.pop(
                        k, None
                    )  # xarray may inject encoding attrs that break append
            ds.to_zarr(zarr_path, mode="a", append_dim="time")
        else:
            if zarr_path.exists():
                shutil.rmtree(zarr_path)
            ds.to_zarr(zarr_path, mode="w", encoding=encoding, zarr_format=2)

    return zarr_path
