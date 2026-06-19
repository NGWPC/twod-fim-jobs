from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel

import twod_fim_jobs
from twod_fim_jobs.models.common import Asset, JobWarning


import geopandas as gpd
from pydantic import Field
from shapely.wkt import loads as load_wkt

from twod_fim_jobs.consts import (
    DEFAULT_BANKFULL_WIDTH_MULTIPLIER,
    DEFAULT_DEM_SOURCE,
    DEFAULT_DOMAIN_BUFFER,
    DEFAULT_EPSG_CODE,
    DEFAULT_GRID_RESOLUTION,
    DEFAULT_LULC_LOOKUP,
    DEFAULT_LULC_SOURCE,
    DEFAULT_WALK_US_DIST_PCT,
)
from twod_fim_jobs.exceptions import InvalidWKTGeometryError


### HELPER JOB MODELS ###


class Domain(BaseModel):
    bbox: tuple[float, float, float, float]  # xmin, ymin, xmax, ymax
    anchor: tuple[float, float]  # x, y
    offsets: tuple[float, float, float, float]  # N, S, E, W

    @property
    def offset_str(self) -> str:
        # TODO: Check with team whether this rounding can lead to issues.
        return "N{}S{}E{}W{}".format(*[int(i) for i in self.offsets])


class Identity(BaseModel):
    sdr_commit: str
    reach_geom_hash: str
    grid_resolution: int
    epsg_code: int
    dem_source_inputs_hash: str
    lulc_source_inputs_hash: str
    lulc_lookup_dict_hash: str


class GridProperties(BaseModel):
    rows: int
    cols: int


class Properties(BaseModel):
    grid: GridProperties
    drainage_area_sqkm: float
    bankfull_width_m: float
    upstream_reach_ids: list[int]
    stream_order: int
    length_m: float
    slope: float
    downstream_reach_id: int
    upstream_mainstem_reach_id: int


class Assets(BaseModel):
    terrain: Asset
    roughness: Asset
    centerline: Asset
    reach_centroid: Asset
    domain: Asset



### CORE JOB MODELS ###


class BuildModelInputs(BaseModel):
    """Inputs for the build-model workflow."""

    # Required
    reach_id: int = Field(description="Reach identifier")
    db_uri: str = Field(description="Connection string for the hydrofabric database")
    base_output_path: str = Field(description="Root directory for model output")

    # Optional
    dem_source: str = Field(
        default=DEFAULT_DEM_SOURCE,
        description="DEM data source",
    )
    lulc_source: str = Field(
        default=DEFAULT_LULC_SOURCE,
        description="Land-cover data source",
    )
    other_geometries: list[str] | None = Field(
        default=None,
        description="WKT geometries that must be included in the reach bounding box",
    )
    domain_buffer: float = Field(
        default=DEFAULT_DOMAIN_BUFFER,
        ge=0,
        description="Buffer distance (CRS units) applied to the domain bounding box",
    )
    grid_resolution: int = Field(
        default=DEFAULT_GRID_RESOLUTION,
        gt=0,
        description="Pixel size (CRS units) for DEM and roughness resampling",
    )
    walk_us_dist_pct: float = Field(
        default=DEFAULT_WALK_US_DIST_PCT,
        gt=0,
        description="Upstream search distance as a fraction of reach length",
    )
    epsg_code: int = Field(
        default=DEFAULT_EPSG_CODE,
        gt=0,
        description="Output coordinate reference system EPSG code",
    )
    bankfull_width_multiplier: float = Field(
        default=DEFAULT_BANKFULL_WIDTH_MULTIPLIER,
        gt=0,
        description="Multiplier applied to the estimated bankfull width",
    )
    lulc_lookup: dict = Field(
        default=DEFAULT_LULC_LOOKUP,
        description="Mapping from land-cover class values to Manning's n roughness values",
    )

    @property
    def other_geometries_gdf(self) -> gpd.GeoDataFrame:
        """Convert optional WKT geometries into a GeoDataFrame."""
        if not self.other_geometries:
            return gpd.GeoDataFrame(geometry=[])

        geometries = []
        for index, wkt_text in enumerate(self.other_geometries):
            try:
                geometries.append(load_wkt(wkt_text))
            except Exception as exc:
                raise InvalidWKTGeometryError(
                    f"Invalid WKT at other_geometries[{index}]: {wkt_text}"
                ) from exc

        return gpd.GeoDataFrame(
            {"source_wkt": self.other_geometries},
            geometry=geometries,
            crs=self.epsg_code,
        )

    @property
    def authority_str(self) -> str:
        """Return the EPSG:num formatting of epsg code."""
        return f"EPSG:{self.epsg_code}"


class BuildModelResult(BaseModel):
    identity_hash: str
    model_id: str
    model_dir: Path
    warnings: list[JobWarning]


class ModelManifest(BaseModel):
    type: Literal["model"] = "model"
    hash_algo: Literal["sha256"] = "sha256"
    twod_fim_version: str = twod_fim_jobs.__version__
    created_at: datetime
    reach_id: int
    identity_hash: str
    domain_code: str
    model_id: str
    inputs: BuildModelInputs
    domain: Domain
    identity: Identity
    properties: Properties
    assets: Assets
    warnings: list[JobWarning]
