from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import geopandas as gpd
import pandas as pd
from pydantic import BaseModel, Field
from shapely.wkt import loads as load_wkt

from twod_fim_jobs.consts import (
    DA_FIELD,
    DEFAULT_BANKFULL_WIDTH_MULTIPLIER,
    DEFAULT_DEM_SOURCE,
    DEFAULT_DOMAIN_BUFFER,
    DEFAULT_EPSG_CODE,
    DEFAULT_GRID_RESOLUTION,
    DEFAULT_LULC_LOOKUP,
    DEFAULT_LULC_SOURCE,
    DEFAULT_WALK_US_DIST_PCT,
    REACH_ID_FIELD,
    SDR_COMMIT,
    SLOPE_FIELD,
    STREAM_ORDER_FIELD,
    bieger_bankfull_width,
)
from twod_fim_jobs.data_models import (
    Assets,
    GridProperties,
    Identity,
    ModelManifest,
    Properties,
)
from twod_fim_jobs.exceptions import InvalidWKTGeometryError
from twod_fim_jobs.jobs.shared import Job
from twod_fim_jobs.utils.geospatial import (
    build_model_domain,
    download_dem,
    download_roughness,
    export_domain_gdfs,
    get_line_intersections,
    make_inflow_line,
    write_gdf_asset,
)
from twod_fim_jobs.utils.hashing import hash_dict, hash_geometry, hash_str
from twod_fim_jobs.utils.storage import copy_file, query_reach, query_upstream_reach
from twod_fim_jobs.warnings import CenterlineInflowMultiIntersectionWarning


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
    lulc_lookup: dict | None = Field(
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
    warnings: list[str]


class BuildModelWorkflow(Job):
    """Initialize a 2D FIM model for a single reach."""

    Inputs = BuildModelInputs

    def run(self, inputs: BuildModelInputs) -> BuildModelResult:
        # Initialize empty warnings
        job_warnings: list[str] = []

        # Query database for relevant geometries
        reach = query_reach(inputs.reach_id, inputs.db_uri)
        us_reaches, us_mainstem = query_upstream_reach(inputs.reach_id, inputs.db_uri)

        # Make inflow line and validate
        inflow_line = make_inflow_line(
            reach,
            us_mainstem,
            inputs.bankfull_width_multiplier,
            inputs.walk_us_dist_pct,
        )
        cl_inf_intersections = self._check_inflow_cl_intersection(reach, inflow_line)
        if cl_inf_intersections:
            job_warnings.append(cl_inf_intersections)
        all_other_geometries = gpd.GeoDataFrame(
            pd.concat([inflow_line, inputs.other_geometries_gdf])
        )

        # Build domain
        domain = build_model_domain(
            reach, all_other_geometries, inputs.grid_resolution, inputs.domain_buffer
        )
        cols = int((domain.bbox[2] - domain.bbox[0]) / inputs.grid_resolution)
        rows = int((domain.bbox[3] - domain.bbox[1]) / inputs.grid_resolution)

        # Build identity
        identitiy = Identity(
            sdr_commit=SDR_COMMIT,
            reach_geom_hash=hash_geometry(reach.geometry.iloc[0], role_length=8),
            grid_resolution=inputs.grid_resolution,
            epsg_code=inputs.epsg_code,
            dem_source_inputs_hash=hash_str(inputs.dem_source, role_length=8),
            lulc_source_inputs_hash=hash_str(inputs.lulc_source, role_length=8),
            lulc_lookup_dict_hash=hash_dict(inputs.lulc_lookup, role_length=8),
        )
        identity_hash = hash_dict(identitiy.model_dump(), role_length=8)
        model_id = f"{identity_hash}+{domain.offset_str}"
        model_dir = f"{inputs.base_output_path}/{model_id}/model.json"
        # TODO: Check that model dir doesn't exist.  Exit if it does.

        # Get DEM
        dem_asset = download_dem(
            inputs.dem_source, "dem.tif", domain.bbox, cols, rows, inputs.authority_str
        )

        # Get LULC
        # TODO: add similarity check/warning
        roughness_asset = download_roughness(
            inputs.lulc_source,
            "mannings.tif",
            domain.bbox,
            cols,
            rows,
            inputs.authority_str,
            inputs.lulc_lookup,
        )

        # Write vector artifacts
        cl_asset = write_gdf_asset(reach, "reach.geojson", inputs.db_uri)
        anchor_gdf, domain_gdf = export_domain_gdfs(domain, inputs.authority_str)
        anchor_asset = write_gdf_asset(anchor_gdf, "anchor.geojson", inputs.db_uri)
        domain_asset = write_gdf_asset(domain_gdf, "domain.geojson", inputs.db_uri)

        # Compile assets
        assets = Assets(
            terrain=dem_asset,
            roughness=roughness_asset,
            centerline=cl_asset,
            reach_centroid=anchor_asset,
            domain=domain_asset,
        )

        # Make properties block
        properties = Properties(
            grid=GridProperties(rows=rows, cols=cols),
            drainage_area_sqkm=reach[DA_FIELD].iloc[0],
            bankfull_width_m=bieger_bankfull_width(reach[DA_FIELD].iloc[0]),
            upstream_reach_ids=us_reaches,
            stream_order=reach[STREAM_ORDER_FIELD].iloc[0],
            length_m=reach.length.iloc[0],
            slope=reach[SLOPE_FIELD].iloc[0],
            downstream_reach_id=reach[REACH_ID_FIELD].iloc[0],
            upstream_mainstem_reach_id=us_mainstem[REACH_ID_FIELD].iloc[0],
        )

        # Write model manifest json
        manifest = ModelManifest(
            created_at=datetime.now(),
            reach_id=inputs.reach_id,
            identity_hash=identity_hash,
            domain_code=domain.offset_str,
            model_id=model_id,
            inputs=inputs.model_dump(),
            domain=domain,
            identity=identitiy,
            properties=properties,
            assets=assets,
            warnings=job_warnings,
        )

        # Write files to storage
        out = f"{inputs.base_output_path}/{model_id}/"
        copy_file("dem.tif", out + "dem.tif")
        copy_file("mannings.tif", out + "mannings.tif")
        copy_file("reach.geojson", out + "reach.geojson")
        copy_file("anchor.geojson", out + "anchor.geojson")
        copy_file("domain.geojson", out + "domain.geojson")
        copy_file("model_manifest.json", out + "model.json")

        return BuildModelResult(
            identity_hash=identity_hash,
            model_id=model_id,
            model_dir=model_dir,
            warnings=job_warnings
        )

    def _check_inflow_cl_intersection(
        self, centerline: gpd.GeoDataFrame, inflow: gpd.GeoDataFrame
    ) -> CenterlineInflowMultiIntersectionWarning | None:
        intersections = get_line_intersections(
            centerline.geometry.iloc[0], inflow.geometry.iloc[0]
        )
        if len(intersections) > 1:
            return CenterlineInflowMultiIntersectionWarning(intersections)
        else:
            return None
