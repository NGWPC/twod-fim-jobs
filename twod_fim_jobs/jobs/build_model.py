from __future__ import annotations

from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import geopandas as gpd
import pandas as pd
from pydantic import ValidationError

from twod_fim_jobs.consts import (
    ANCHOR_FILENAME,
    DA_FIELD,
    DEFAULT_CENTERLINE_BUFFER,
    DEM_FILENAME,
    DOMAIN_FILENAME,
    INFLOW_FILENAME,
    LARGE_DOMAIN_AREA_THRESHOLD,
    MANIFEST_FILENAME,
    REACH_FILENAME,
    REACH_ID_FIELD,
    ROUGHNESS_FILENAME,
    SDR_COMMIT,
    STREAM_ORDER_FIELD,
    bieger_bankfull_width,
)

from twod_fim_jobs.jobs.common import Job
from twod_fim_jobs.models.build_model import (
    Assets,
    BuildModelInputs,
    BuildModelResult,
    GridProperties,
    Identity,
    ModelManifest,
    Properties,
)
from twod_fim_jobs.models.warnings import (
    CenterlineInflowMultiIntersectionWarning,
    JobWarning,
    LargeDomainAreaWarning,
)
from twod_fim_jobs.models.common import Asset
from twod_fim_jobs.utils.geospatial import (
    build_model_domain,
    download_dem,
    download_roughness,
    ensure_linestring,
    export_domain_gdfs,
    get_line_intersections,
    make_inflow_line,
    write_gdf_asset,
)
from twod_fim_jobs.utils.hashing import hash_dict, hash_geometry, hash_str
from twod_fim_jobs.utils.storage import (
    check_file_exists,
    copy_file,
    query_reach,
    query_upstream_reach,
    read_json,
)


class BuildModelJob(Job[BuildModelInputs]):
    """Initialize a 2D FIM model for a single reach."""

    Inputs = BuildModelInputs

    def _run(self, inputs: BuildModelInputs, tmp_dir: Path) -> BuildModelResult:
        # TODO: eagerly check file access
        # Initialize empty warnings
        job_warnings: list[JobWarning] = []

        # Query database for relevant geometries
        reach = query_reach(inputs.reach_id, inputs.db_uri, inputs.epsg_code)
        reach = ensure_linestring(reach)
        us_reaches, us_mainstem = query_upstream_reach(
            inputs.reach_id, inputs.db_uri, inputs.epsg_code
        )
        if not us_mainstem.empty:
            us_mainstem_id = us_mainstem[REACH_ID_FIELD].iloc[0]
            us_mainstem = ensure_linestring(us_mainstem)
        else:
            us_mainstem_id = None

        # Make inflow line and validate
        inflow_line = make_inflow_line(
            reach,
            us_mainstem,
            inputs.bankfull_width_multiplier,
            inputs.walk_us_dist_pct,
        )
        cl_inf_intersections = _check_inflow_cl_intersection(reach, inflow_line)
        if cl_inf_intersections:
            job_warnings.append(cl_inf_intersections)

        # Assemble other geometries
        cl_buffer_dist = (
            bieger_bankfull_width(float(reach[DA_FIELD].iloc[0]))
            * DEFAULT_CENTERLINE_BUFFER
        )
        cl_buffer = reach.buffer(cl_buffer_dist)
        all_other_geometries = gpd.GeoDataFrame(
            pd.concat([inflow_line, cl_buffer, inputs.other_geometries_gdf])
        )

        # Build domain
        domain = build_model_domain(
            reach, all_other_geometries, inputs.grid_resolution, inputs.domain_buffer
        )
        cols = int((domain.bbox[2] - domain.bbox[0]) / inputs.grid_resolution)
        rows = int((domain.bbox[3] - domain.bbox[1]) / inputs.grid_resolution)
        if domain.area > LARGE_DOMAIN_AREA_THRESHOLD:
            job_warnings.append(
                LargeDomainAreaWarning(
                    domain_area=domain.area, threshold=LARGE_DOMAIN_AREA_THRESHOLD
                )
            )

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
        model_id = f"{identity_hash}_{domain.offset_str}"
        model_dir = f"{inputs.base_output_path.rstrip('/')}/{model_id}/"
        manifest_path = tmp_dir / MANIFEST_FILENAME
        dest_manifest_path = _normalize_href(str(manifest_path), model_dir)
        if _check_model_built(inputs, dest_manifest_path):
            return BuildModelResult(
                identity_hash=identity_hash,
                model_id=model_id,
                model_dir=model_dir,
                warnings=job_warnings,
            )

        # Get DEM
        dem_asset = download_dem(
            inputs.dem_source,
            tmp_dir / DEM_FILENAME,
            domain.bbox,
            cols,
            rows,
            inputs.authority_str,
        )

        # Get LULC
        # TODO: add warning when mannings values are all similar
        roughness_asset = download_roughness(
            inputs.lulc_source,
            tmp_dir / ROUGHNESS_FILENAME,
            domain.bbox,
            cols,
            rows,
            inputs.authority_str,
            inputs.lulc_lookup,
        )

        # Write vector artifacts
        cl_asset = write_gdf_asset(reach, tmp_dir / REACH_FILENAME, inputs.db_uri)
        inflow_asset = write_gdf_asset(
            inflow_line, tmp_dir / INFLOW_FILENAME, inputs.db_uri
        )
        anchor_gdf, domain_gdf = export_domain_gdfs(domain, inputs.authority_str)
        anchor_asset = write_gdf_asset(
            anchor_gdf, tmp_dir / ANCHOR_FILENAME, inputs.db_uri
        )
        domain_asset = write_gdf_asset(
            domain_gdf, tmp_dir / DOMAIN_FILENAME, inputs.db_uri
        )

        # Compile assets
        assets = Assets(
            terrain=dem_asset,
            roughness=roughness_asset,
            centerline=cl_asset,
            inflow_line=inflow_asset,
            reach_centroid=anchor_asset,
            domain=domain_asset,
        )
        assets, copy_job = _create_copy_job(assets, model_dir)

        # Make properties block
        properties = Properties(
            grid=GridProperties(rows=rows, cols=cols),
            drainage_area_sqkm=round(reach[DA_FIELD].iloc[0], 2),
            bankfull_width_m=round(bieger_bankfull_width(reach[DA_FIELD].iloc[0]), 2),
            upstream_reach_ids=us_reaches,
            stream_order=reach[STREAM_ORDER_FIELD].iloc[0],
            length_m=round(reach.length.iloc[0], 2),
            downstream_reach_id=reach[REACH_ID_FIELD].iloc[0],
            upstream_mainstem_reach_id=us_mainstem_id,
        )

        # Write model manifest json
        manifest = ModelManifest(
            created_at=datetime.now(),
            reach_id=inputs.reach_id,
            identity_hash=identity_hash,
            domain_code=domain.offset_str,
            model_id=model_id,
            inputs=inputs,
            domain=domain,
            identity=identitiy,
            properties=properties,
            assets=assets,
            warnings=job_warnings,
        )

        with open(manifest_path, mode="w") as f:
            f.write(manifest.model_dump_json(indent=4))
        copy_job[str(manifest_path)] = dest_manifest_path

        # Write files to storage
        for src, dst in copy_job.items():
            copy_file(src, dst)

        return BuildModelResult(
            identity_hash=identity_hash,
            model_id=model_id,
            model_dir=model_dir,
            warnings=job_warnings,
        )


def _check_inflow_cl_intersection(
    centerline: gpd.GeoDataFrame, inflow: gpd.GeoDataFrame
) -> CenterlineInflowMultiIntersectionWarning | None:
    intersections = get_line_intersections(
        centerline.geometry.iloc[0], inflow.geometry.iloc[0]
    )
    if len(intersections) > 1:
        return CenterlineInflowMultiIntersectionWarning(
            [(pt.x, pt.y) for pt in intersections]
        )
    else:
        return None


def _normalize_href(href: str, new_base_path: str) -> str:
    parsed = urlparse(href)
    filename = parsed.path.split("/")[-1]
    return f"{new_base_path.rstrip('/')}/{filename}"


def _create_copy_job(
    assets: Assets, new_base_path: str
) -> tuple[Assets, dict[str, str]]:
    """Return a new Assets with hrefs relocated to new_base_path and a mapping for how to copy them there."""
    copy_job: dict[str, str] = {}
    new_asset_fields: dict[str, Asset] = {}
    for field_name, asset in assets:
        dest = _normalize_href(asset.href, new_base_path)
        copy_job[asset.href] = dest
        new_asset_fields[field_name] = asset.model_copy(update={"href": dest})
    return Assets(**new_asset_fields), copy_job


def _check_model_built(inputs: BuildModelInputs, manifest_href: str) -> bool:
    """Checks if a model with the same inputs has already been built."""
    if not check_file_exists(manifest_href):
        return False
    try:
        ref = ModelManifest.model_validate_json(read_json(manifest_href))
        return ref.inputs == inputs
    except ValidationError:
        return False
