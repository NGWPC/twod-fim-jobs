from math import floor, ceil
import math
from collections.abc import Iterable
from pathlib import Path
from typing import Callable
from datetime import datetime

import geopandas as gpd
import numpy as np
import pandas as pd

import rasterio
from rasterio.transform import from_bounds
from rasterio.warp import reproject

from shapely import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPoint,
    Point,
    box,
)

from twod_fim_jobs.consts import DA_FIELD, bieger_bankfull_width
from twod_fim_jobs.models.common import Asset
from twod_fim_jobs.models.build_model import Domain
from twod_fim_jobs.exceptions import DatasetUnavailableError, RasterProcessingError


def perpendicular_line(
    base_line: LineString, at_point: Point, length: float
) -> LineString:
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
    inflow_width = (
        bieger_bankfull_width(float(reach[DA_FIELD].iloc[0]))
        * bankfull_width_multiplier
    )
    reach_geom = reach.geometry.iloc[0]
    us_geom = us_mainstem.geometry.iloc[0]
    if us_mainstem.empty:
        us_bc_pt = Point(reach_geom.coords[0])
        inflow_geom = perpendicular_line(reach_geom, us_bc_pt, inflow_width)
    else:
        # Walk upstream a bit for u/s boundary condition
        walk_us_dist = us_geom.length * walk_us_dist_pct
        us_bc_pt = us_geom.interpolate(1 - walk_us_dist)
        inflow_geom = perpendicular_line(us_geom, us_bc_pt, inflow_width)
    return gpd.GeoDataFrame({"ind": [1]}, geometry=[inflow_geom], crs=reach.crs)


def get_line_intersections(geom_1: LineString, geom_2: LineString) -> list[Point]:
    """Count the number of times two geometries intersect."""
    intersection = geom_1.intersection(geom_2)
    if intersection.is_empty:
        return []

    points = _intersection_points(intersection)

    # De-duplicate exact coordinate duplicates introduced by mixed geometry types.
    return list({point for point in points})


def _intersection_points(geom) -> list[Point]:
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

    w = ax - xmin
    e = xmax - ax
    s = ay - ymin
    n = ymax - ay

    # Return Domain object
    return Domain(bbox=(xmin, ymin, xmax, ymax), anchor=(ax, ay), offsets=(n, s, e, w))


def extract_raster(*args, **kwargs) -> None:
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
    return_asset: bool = True,
):
    """
    Extract/reproject a raster to a target grid and optionally transform values.
    """

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

    if return_asset:
        return Asset.from_file(str(out_path), src_path, datetime.now())


def download_dem(
    src_path: str | Path,
    out_path: str | Path,
    bbox: tuple[float, float, float, float],
    cols: int,
    rows: int,
    dst_crs: str,
    return_asset: bool = True,
) -> Asset | None:
    def noop(data):
        return data

    return extract_raster(
        src_path, out_path, bbox, cols, rows, dst_crs, noop, return_asset
    )


def download_roughness(
    src_path: str | Path,
    out_path: str | Path,
    bbox: tuple[float, float, float, float],
    cols: int,
    rows: int,
    dst_crs: str,
    lulc_lookup: dict[int, float],
    return_asset: bool = True,
) -> Asset | None:
    manning_lut = _make_lookup_array(lulc_lookup)
    return extract_raster(
        src_path,
        out_path,
        bbox,
        cols,
        rows,
        dst_crs,
        lambda x: manning_lut[x.astype(np.uint8)],
        return_asset,
    )


def _make_lookup_array(
    lookup: dict[int, float],
    size: int = 256,
    fill_value: float = -9999,
) -> np.ndarray:
    """
    Convert a categorical lookup dictionary into a NumPy lookup array.
    """
    lut = np.full(size, fill_value, dtype="float32")

    for key, value in lookup.items():
        lut[key] = value

    return lut


def write_gdf_asset(
    gdf: gpd.GeoDataFrame, out_path: str | Path, source_url: str
) -> Asset:
    gdf.to_file(out_path)
    return Asset.from_file(str(out_path), source_url, datetime.now())


def export_domain_gdfs(
    domain: Domain, crs: str
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Convert Domain object into component geodataframes (convenience func)."""
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
