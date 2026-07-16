from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

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
    """Computed domain realization."""

    model_config = ConfigDict(extra="forbid")

    bbox: tuple[float, float, float, float] = Field(description="Domain bbox in native CRS [west, south, east, north].")  # xmin, ymin, xmax, ymax
    anchor: tuple[float, float] = Field(description="Grid-snapped reach centroid in native CRS [x, y]; origin the offsets are measured from.")  # x, y
    offsets: tuple[float, float, float, float] = Field(description="[N, S, E, W] grid-snapped offsets in CRS units; matches domain_token.")  # N, S, E, W

    @property
    def offset_str(self) -> str:
        # TODO: Check with team whether this rounding can lead to issues.
        return "N{}S{}E{}W{}".format(*[int(i) for i in self.offsets])


class Identity(BaseModel):
    """The inputs that define model identity; hashed to identity_hash."""

    model_config = ConfigDict(extra="forbid")

    sdr_commit: str = Field(description="Methodology version pin (output-determining).")
    reach_geom_hash: str = Field(pattern=r"^[0-9a-f]{8}$", description="Hash of WKT representation of reach geometry")
    grid_resolution: float = Field(description="Model horizontal resolution", gt=0)
    epsg_code: int = Field(description="EPSG integer that model will be created in")
    dem_source_inputs_hash: str = Field(pattern=r"^[0-9a-f]{8}$", description="Hash of connection parameters to roughness dataset")
    lulc_source_inputs_hash: str = Field(pattern=r"^[0-9a-f]{8}$", description="Hash of connection parameters to lulc dataset")
    lulc_lookup_dict_hash: str = Field(pattern=r"^[0-9a-f]{8}$", description="Hash of mapping from land use code to manning's roughness")


class GridProperties(BaseModel):
    """Grid dimensions of the model domain."""

    model_config = ConfigDict(extra="forbid")

    rows: int = Field(description="Number of rows in the model domain", gt=0)
    cols: int = Field(description="Number of columns in the model domain", gt=0)


class Properties(BaseModel):
    """Computed values and hydrofabric attributes (informational; not part of identity)."""

    model_config = ConfigDict(extra="forbid")

    grid: GridProperties
    drainage_area_sqkm: float = Field(description="From the reach DB; missing/invalid raises InvalidAttributeError (no model.json written).", gt=0)
    bankfull_width_m: float = Field(description="Estimated bankfull width (pre-multiplier) used to build the inflow.", gt=0)
    upstream_reach_ids: list[int] = Field(description="Reach IDs in the network db of any reaches tributary to this model's reach")
    stream_order: int | None = Field(description="Strahler order of the reach for this model")
    length_m: float | None = Field(description="Length of the reach centerline for this model")
    slope: float | None = Field(description="Slope along the reach centerline for this model")
    downstream_reach_id: int | None = Field(description="ID of the reach downstream of this model's reach")
    upstream_mainstem_reach_id: int | None = Field(description="ID of the reach with the largest drainage area of the reaches draining to this reach")


class Assets(BaseModel):
    """One entry per output (model.json excluded), keyed by role; hrefs are the flat files under <id>/."""

    model_config = ConfigDict(extra="forbid")

    terrain: Asset = Field(description="Terrain raster used by the hydraulic model.")
    roughness: Asset = Field(description="Manning's n raster used by the hydraulic model.")
    centerline: Asset = Field(description="River centerline for this model's reach.")
    reach_centroid: Asset = Field(description="Centroid of the river centerline for this model's reach.")
    domain: Asset = Field(description="Derived polygon of the full model domain.")


### CORE JOB MODELS ###


class BuildModelInputs(BaseModel):
    """Inputs for the build_model workflow."""

    model_config = ConfigDict(extra="forbid")

    # Required
    reach_id: int = Field(description="Primary key for the reach in the reach db")
    db_uri: str = Field(description="Connection string for the refactored hydrofabric")
    base_output_path: str = Field(description="Path where output artifacts will be written")

    # Optional
    dem_source: str = Field(
        default=DEFAULT_DEM_SOURCE,
        description="Connection string for the DEM dataset",
    )
    lulc_source: str = Field(
        default=DEFAULT_LULC_SOURCE,
        description="Connection string for the LULC source dataset",
    )
    other_geometries: list[str] = Field(
        default_factory=list,
        description="A list of geometries that will be included when making the model domain bounding box",
    )
    domain_buffer: float = Field(
        default=DEFAULT_DOMAIN_BUFFER,
        ge=0,
        description="How far to buffer the bounding box on model geometries",
    )
    grid_resolution: float = Field(
        default=DEFAULT_GRID_RESOLUTION,
        gt=0,
        description="Resolution that grid will snap to and that DEM and roughness will resample to",
    )
    walk_us_dist_pct: float = Field(
        default=DEFAULT_WALK_US_DIST_PCT,
        gt=0,
        description="How far to walk up the upstream mainstem centerline to place the inflow boundary condition, as percent of upstream centerline length",
    )
    epsg_code: int = Field(
        default=DEFAULT_EPSG_CODE,
        gt=0,
        description="EPSG integer for all georeferenced output artifacts",
    )
    bankfull_width_multiplier: float = Field(
        default=DEFAULT_BANKFULL_WIDTH_MULTIPLIER,
        gt=0,
        description="How much to multiply bankfull width to arrive at inflow line width",
    )
    lulc_lookup: dict[str, float] = Field(
        default=DEFAULT_LULC_LOOKUP,
        description="A dictionary mapping land use codes to Manning's roughness values",
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
    model_config = ConfigDict(extra="forbid")

    identity_hash: str
    model_id: str
    model_dir: Path
    warnings: list[JobWarning]


class ModelManifest(BaseModel):
    """build_model output record: model definition + artifact inventory."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["model"] = Field(default="model", description="Discriminator vs. a run record.")
    hash_algo: Literal["sha256"] = Field(default="sha256", description="Hash function for every hash/checksum in this document. Truncation length is per-field (see build_model-design.md).")
    twod_fim_version: str = Field(default=twod_fim_jobs.__version__, description="Producer software version (provenance).")
    created_at: datetime = Field(description="Build completion time (UTC). model.json is written last.")
    reach_id: int = Field(description="Primary key for the reach in the reach db")
    identity_hash: str = Field(pattern=r"^[0-9a-f]{8}$", description="Hash of the identity object. Stable across domain changes; results group under it.")
    domain_code: str = Field(pattern=r"^N(0|[1-9][0-9]*)S(0|[1-9][0-9]*)E(0|[1-9][0-9]*)W(0|[1-9][0-9]*)$", description="Domain realization: grid-snapped N/S/E/W offsets in CRS units from the anchor. A grid-reference code, not a hash.")
    model_id: str = Field(pattern=r"^[0-9a-f]{8}_N(0|[1-9][0-9]*)S(0|[1-9][0-9]*)E(0|[1-9][0-9]*)W(0|[1-9][0-9]*)$", description="<identity_hash>+<domain_code>. Also the folder name.")
    inputs: BuildModelInputs = Field(description="The build_model call arguments, recorded verbatim.")
    domain: Domain = Field(description="Computed domain realization.")
    identity: Identity = Field(description="The inputs that define model identity; hashed to identity_hash.")
    properties: Properties = Field(description="Computed values and hydrofabric attributes (informational; not part of identity).")
    assets: Assets = Field(description="One entry per output (model.json excluded), keyed by role; hrefs are the flat files under <id>/.")
    warnings: list[JobWarning] = Field(default=[], description="Non-fatal check results; the build still completes and writes model.json.")
