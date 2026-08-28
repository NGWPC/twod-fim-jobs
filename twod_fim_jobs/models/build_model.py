from datetime import datetime
from typing import Iterator, Literal

from pydantic import BaseModel, ConfigDict

import twod_fim_jobs
from twod_fim_jobs.models.common import Asset
from twod_fim_jobs.models.warnings import JobWarning


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
from twod_fim_jobs.models.common import Domain, GridProperties

### HELPER JOB MODELS ###


class Identity(BaseModel):
    """The inputs that define model identity; hashed to identity_hash."""

    model_config = ConfigDict(extra="forbid")

    sdr_commit: str = Field(
        description="Methodology version pin (output-determining).",
        examples=["a1b2c3d4"],
    )
    reach_geom_hash: str = Field(
        pattern=r"^[0-9a-f]{8}$",
        description="Hash of WKT representation of reach geometry",
        examples=["fceb20c6"],
    )
    grid_resolution: float = Field(
        description="Model horizontal resolution", gt=0, examples=[10.0]
    )
    epsg_code: int = Field(
        description="EPSG integer that model will be created in", examples=[5070]
    )
    dem_source_inputs_hash: str = Field(
        pattern=r"^[0-9a-f]{8}$",
        description="Hash of connection parameters to roughness dataset",
        examples=["a1b2c3d4"],
    )
    lulc_source_inputs_hash: str = Field(
        pattern=r"^[0-9a-f]{8}$",
        description="Hash of connection parameters to lulc dataset",
        examples=["e5f6a7b8"],
    )
    lulc_lookup_dict_hash: str = Field(
        pattern=r"^[0-9a-f]{8}$",
        description="Hash of mapping from land use code to manning's roughness",
        examples=["c9d0e1f2"],
    )


class Properties(BaseModel):
    """Computed values and hydrofabric attributes (informational; not part of identity)."""

    model_config = ConfigDict(extra="forbid")

    grid: GridProperties
    drainage_area_sqkm: float = Field(
        description="From the reach network; missing/invalid raises InvalidAttributeError (no model.json written).",
        gt=0,
        examples=[142.7],
    )
    bankfull_width_m: float = Field(
        description="Estimated bankfull width (pre-multiplier) used to build the inflow.",
        gt=0,
        examples=[35.2],
    )
    upstream_reach_ids: list[int] = Field(
        description="Reach IDs of any reaches tributary to this model's reach",
        examples=[[1257410937935510]],
    )
    stream_order: int | None = Field(
        description="Strahler order of the reach for this model",
        examples=[4],
    )
    length_m: float | None = Field(
        description="Length of the reach centerline for this model",
        examples=[2340.5],
    )
    downstream_reach_id: int | None = Field(
        description="ID of the reach downstream of this model's reach",
        examples=[1257410937935513],
    )
    upstream_mainstem_reach_id: int | None = Field(
        description="ID of the reach with the largest drainage area of the reaches draining to this reach",
        examples=[1257410937935510],
    )


class Assets(BaseModel):
    """One entry per output (model.json excluded), keyed by role; hrefs are the flat files under <id>/."""

    model_config = ConfigDict(extra="forbid")

    terrain: Asset = Field(description="Terrain raster used by the hydraulic model.")
    roughness: Asset = Field(
        description="Manning's n raster used by the hydraulic model."
    )
    centerline: Asset = Field(description="River centerline for this model's reach.")
    inflow_line: Asset = Field(
        description="Inflow boundary condition line for this model's reach."
    )
    reach_centroid: Asset = Field(
        description="Centroid of the river centerline for this model's reach."
    )
    domain: Asset = Field(description="Derived polygon of the full model domain.")

    def __iter__(self) -> Iterator[tuple[str, Asset]]:  # type: ignore[override]
        for field in Assets.model_fields:
            yield field, getattr(self, field)


### CORE JOB MODELS ###


class BuildModelInputs(BaseModel):
    """Inputs for the build_model workflow."""

    model_config = ConfigDict(extra="forbid")

    # Required
    reach_id: int = Field(
        description="Primary key for the reach in the reach network",
        examples=[1257410937935512],
    )
    reach_network_path: str = Field(
        description="Path to the reach network GeoParquet, sorted by reach_id",
        examples=["s3://twod-fim/version=v1/reference_data/reach_network.parquet"],
    )
    upstream_reach_ids: list[int] = Field(
        default_factory=list,
        description="Ids of the reaches draining into this one",
        examples=[[1257410937935511, 1257410937935510]],
    )
    upstream_mainstem_reach_id: int | None = Field(
        default=None,
        description=(
            "Upstream reach with the largest drainage area; null for a headwater"
        ),
        examples=[1257410937935511],
    )
    base_output_path: str = Field(
        description="Path where output artifacts will be written",
        examples=["s3://twod-fim/version=v1/models/"],
    )

    # Optional
    dem_source: str = Field(
        default=DEFAULT_DEM_SOURCE,
        description="Connection string for the DEM dataset",
        examples=[
            "https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/13/TIFF/USGS_Seamless_DEM_13.vrt"
        ],
    )
    lulc_source: str = Field(
        default=DEFAULT_LULC_SOURCE,
        description="Connection string for the LULC source dataset",
        examples=[
            "/vsis3/usgs-landcover/annual-nlcd/c1/v0/cu/mosaic/Annual_NLCD_LndCov_2023_CU_C1V0.tif"
        ],
    )
    other_geometries: list[str] = Field(
        default_factory=list,
        description="A list of geometries that will be included when making the model domain bounding box",
        examples=[["POLYGON ((0 0, 1 0, 1 1, 0 1, 0 0))"]],
    )
    domain_buffer: float = Field(
        default=DEFAULT_DOMAIN_BUFFER,
        ge=0,
        description="How far to buffer the bounding box on model geometries",
        examples=[100.0],
    )
    grid_resolution: float = Field(
        default=DEFAULT_GRID_RESOLUTION,
        gt=0,
        description="Resolution that grid will snap to and that DEM and roughness will resample to",
        examples=[10.0],
    )
    walk_us_dist_pct: float = Field(
        default=DEFAULT_WALK_US_DIST_PCT,
        gt=0,
        description="How far to walk up the upstream mainstem centerline to place the inflow boundary condition, as percent of upstream centerline length",
        examples=[0.25],
    )
    epsg_code: int = Field(
        default=DEFAULT_EPSG_CODE,
        gt=0,
        description="EPSG integer for all georeferenced output artifacts",
        examples=[5070],
    )
    bankfull_width_multiplier: float = Field(
        default=DEFAULT_BANKFULL_WIDTH_MULTIPLIER,
        gt=0,
        description="How much to multiply bankfull width to arrive at inflow line width",
        examples=[1.0],
    )
    lulc_lookup: dict[int, float] = Field(
        default=DEFAULT_LULC_LOOKUP,
        description="A dictionary mapping land use codes to Manning's roughness values",
        examples=[{11: 0.04, 21: 0.04, 31: 0.025, 41: 0.16, 82: 0.035}],
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

    identity_hash: str = Field(
        description="Hash of the model identity inputs (methodology, sources, params). Used for grouping, rollback, and path addressing.",
        examples=["fceb20c6"],
    )
    model_id: str = Field(
        description="Full model identifier: identity_hash + domain_code. Locates the model within the storage layout.",
        examples=["fceb20c6_N164S214E230W107"],
    )
    model_dir: str = Field(
        description="Content-addressed path where model artifacts were written.",
        examples=[
            "s3://twod-fim/version=v1/models/1257410937935512/fceb20c6_N164S214E230W107"
        ],
    )
    warnings: list[JobWarning] = Field(
        description="Non-fatal warnings raised during the job.", examples=[[]]
    )


class ModelManifest(BaseModel):
    """build_model output record: model definition + artifact inventory."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["model"] = Field(
        default="model", description="Discriminator vs. a run record."
    )
    hash_algo: Literal["sha256"] = Field(
        default="sha256",
        description="Hash function for every hash/checksum in this document. Truncation length is per-field (see build_model-design.md).",
    )
    twod_fim_version: str = Field(
        default=twod_fim_jobs.__version__,
        description="Producer software version (provenance).",
    )
    created_at: datetime = Field(
        description="Build completion time (UTC). model.json is written last.",
        examples=["2026-08-06T22:17:07.406819Z"],
    )
    reach_id: int = Field(
        description="Primary key for the reach in the reach network",
        examples=[1257410937935512],
    )
    identity_hash: str = Field(
        pattern=r"^[0-9a-f]{8}$",
        description="Hash of the identity object. Stable across domain changes; results group under it.",
        examples=["fceb20c6"],
    )
    domain_code: str = Field(
        pattern=r"^N(0|[1-9][0-9]*)S(0|[1-9][0-9]*)E(0|[1-9][0-9]*)W(0|[1-9][0-9]*)$",
        description="Domain realization: grid-snapped N/S/E/W offsets in CRS units from the anchor. A grid-reference code, not a hash.",
        examples=["N164S214E230W107"],
    )
    model_id: str = Field(
        pattern=r"^[0-9a-f]{8}_N(0|[1-9][0-9]*)S(0|[1-9][0-9]*)E(0|[1-9][0-9]*)W(0|[1-9][0-9]*)$",
        description="<identity_hash>+<domain_code>. Also the folder name.",
        examples=["fceb20c6_N164S214E230W107"],
    )
    inputs: BuildModelInputs = Field(
        description="The build_model call arguments, recorded verbatim."
    )
    domain: Domain = Field(description="Computed domain realization.")
    identity: Identity = Field(
        description="The inputs that define model identity; hashed to identity_hash."
    )
    properties: Properties = Field(
        description="Computed values and hydrofabric attributes (informational; not part of identity)."
    )
    assets: Assets = Field(
        description="One entry per output (model.json excluded), keyed by role; hrefs are the flat files under <id>/."
    )
    warnings: list[JobWarning] = Field(
        default=[],
        description="Non-fatal check results; the build still completes and writes model.json.",
        examples=[[]],
    )
