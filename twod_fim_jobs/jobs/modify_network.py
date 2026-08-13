from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd

from twod_fim_jobs.consts import (
    COASTAL_LAYER,
    LAKE_ID_FIELD,
    LAKES_FILENAME,
    LAKES_LAYER,
    NETWORK_FILENAME,
    NETWORK_MANIFEST_FILENAME,
    REACH_TABLE,
    SDR_COMMIT,
)
from twod_fim_jobs.exceptions import WriteFailureError
from twod_fim_jobs.jobs.common import Job
from twod_fim_jobs.models.common import Asset, JobWarning
from twod_fim_jobs.models.modify_network import (
    AmbiguousReachClassificationWarning,
    Assets,
    Identity,
    ModifyNetworkInputs,
    ModifyNetworkResult,
    NetworkExistsWarning,
    NetworkManifest,
    Properties,
)
from twod_fim_jobs.utils.hashing import hash_dict, hash_str
from twod_fim_jobs.utils.network import (
    apply_coastal,
    apply_lakes,
    finalize_network,
    load_reach_network,
    load_vector_layer,
    merge_short_reaches,
    prepare_lakes,
    tag_headwater_reaches,
    tag_terminal_reaches,
)
from twod_fim_jobs.utils.storage import check_path_exists, copy_file

logger = logging.getLogger(__name__)


class ModifyNetworkJob(Job[ModifyNetworkInputs]):
    """Trim, tag, split, and merge the raw hydrofabric into a modeling network.

    Operates on the whole network in one call — no realization axis, so
    id == identity_hash and artifacts land under
    ``<base_output_path>/<identity_hash>/``.
    """

    Inputs = ModifyNetworkInputs

    def _run(self, inputs: ModifyNetworkInputs, tmp_dir: Path) -> ModifyNetworkResult:
        # Identity hashes cover connection params, not file contents, so the
        # output location is known before any dataset is opened. That is what
        # lets network_exists short-circuit without loading the network.
        identity = Identity(
            sdr_commit=SDR_COMMIT,
            reach_network_hash=hash_str(inputs.reach_network_path, role_length=8),
            lakes_layer_hash=_optional_hash(inputs.lakes_layer_path),
            coastal_influence_layer_hash=_optional_hash(
                inputs.coastal_influence_layer_path
            ),
            drainage_area_threshold_percent=inputs.drainage_area_threshold_percent,
            stream_order_filter_threshold=inputs.stream_order_filter_threshold,
            min_length_threshold_km=inputs.min_length_threshold_km,
            lake_area_threshold_sqkm=inputs.lake_area_threshold_sqkm,
            negative_lake_buffer_meters=inputs.negative_lake_buffer_meters,
        )
        identity_hash = hash_dict(identity.model_dump(mode="json"), role_length=8)
        network_dir = f"{inputs.base_output_path.rstrip('/')}/{identity_hash}/"

        if check_path_exists(f"{network_dir}{NETWORK_MANIFEST_FILENAME}"):
            warning = NetworkExistsWarning(
                network_dir=network_dir, identity_hash=identity_hash
            )
            logger.warning(warning.message)
            return ModifyNetworkResult(
                identity_hash=identity_hash,
                network_dir=network_dir,
                warnings=[warning],
            )

        gdf, counters = load_reach_network(
            inputs.reach_network_path, inputs.stream_order_filter_threshold
        )
        logger.info(
            "Loaded %d reaches (%s of %d in source)",
            len(gdf),
            "unfiltered"
            if inputs.stream_order_filter_threshold is None
            else f"stream_order >= {inputs.stream_order_filter_threshold}",
            counters.n_reaches_input,
        )

        gdf = tag_terminal_reaches(gdf)
        gdf = tag_headwater_reaches(gdf)

        # Coastal before lakes: the accounting rule attributes an ambiguous
        # reach to coastal precisely because coastal removes it first.
        coastal_touched: set[str] = set()
        if inputs.coastal_influence_layer_path is not None:
            coastal_gdf = load_vector_layer(
                inputs.coastal_influence_layer_path, COASTAL_LAYER, gdf.crs
            )
            gdf, coastal_touched = apply_coastal(gdf, coastal_gdf, counters)
            logger.info("Coastal pass left %d reaches", len(gdf))
        else:
            logger.info("No coastal dataset supplied, skipping coastal processing")

        lake_touched: set[str] = set()
        prepared_lakes: gpd.GeoDataFrame | None = None
        if inputs.lakes_layer_path is not None:
            lakes_gdf = load_vector_layer(inputs.lakes_layer_path, LAKES_LAYER, gdf.crs)
            prepared_lakes = prepare_lakes(
                lakes_gdf,
                inputs.lake_area_threshold_sqkm,
                inputs.negative_lake_buffer_meters,
            )
            gdf, lake_touched = apply_lakes(gdf, prepared_lakes, counters)
            logger.info(
                "Lake pass left %d reaches (%s lakes, %d polygon parts after "
                "the negative buffer)",
                len(gdf),
                prepared_lakes[LAKE_ID_FIELD].nunique()
                if LAKE_ID_FIELD in prepared_lakes.columns
                else "unknown",
                len(prepared_lakes),
            )
        else:
            logger.info("No lakes dataset supplied, skipping lake processing")

        gdf = merge_short_reaches(
            gdf,
            inputs.drainage_area_threshold_percent,
            inputs.min_length_threshold_km,
            counters,
        )
        gdf = finalize_network(gdf, counters)

        job_warnings: list[JobWarning] = []
        ambiguous = sorted(coastal_touched & lake_touched)
        if ambiguous:
            warning = AmbiguousReachClassificationWarning(reach_ids=ambiguous)
            logger.warning(warning.message)
            job_warnings.append(warning)

        # Write artifacts locally, then relocate. hrefs stay relative — the
        # manifest sits beside the files it describes.
        copy_job: dict[str, str] = {}
        network_local = tmp_dir / NETWORK_FILENAME
        _write_gpkg(gdf, network_local, REACH_TABLE)
        network_asset = _asset_for(network_local, NETWORK_FILENAME)
        copy_job[str(network_local)] = f"{network_dir}{NETWORK_FILENAME}"

        lakes_asset = None
        if prepared_lakes is not None:
            lakes_local = tmp_dir / LAKES_FILENAME
            _write_gpkg(prepared_lakes, lakes_local, LAKES_LAYER)
            lakes_asset = _asset_for(lakes_local, LAKES_FILENAME)
            copy_job[str(lakes_local)] = f"{network_dir}{LAKES_FILENAME}"

        manifest = NetworkManifest(
            created_at=datetime.now(timezone.utc),
            identity_hash=identity_hash,
            id=identity_hash,
            identity=identity,
            inputs=inputs,
            properties=Properties(**vars(counters)),
            assets=Assets(network=network_asset, lakes=lakes_asset),
            warnings=job_warnings,
        )
        manifest_local = tmp_dir / NETWORK_MANIFEST_FILENAME
        manifest_local.write_text(manifest.model_dump_json(indent=4))

        # GeoPackages first; network.json is written last so its presence
        # means the whole artifact set landed (and is what network_exists
        # keys on).
        for src, dst in copy_job.items():
            copy_file(src, dst)
        copy_file(str(manifest_local), f"{network_dir}{NETWORK_MANIFEST_FILENAME}")

        logger.info("Wrote network %s to %s", identity_hash, network_dir)
        return ModifyNetworkResult(
            identity_hash=identity_hash,
            network_dir=network_dir,
            warnings=job_warnings,
        )


def _optional_hash(path: str | None) -> str | None:
    """Hash a dataset path, or None when the dataset was not supplied.

    None is the identity's explicit record that the corresponding processing
    step was skipped, and forks identity_hash away from a run that had it.
    """
    return None if path is None else hash_str(path, role_length=8)


def _asset_for(local_path: Path, href: str) -> Asset:
    """Checksum a written artifact and record it under its relative href."""
    asset = Asset.from_file(str(local_path), source_url=None, retrieved=None)
    return asset.model_copy(update={"href": href})


def _write_gpkg(gdf: gpd.GeoDataFrame, path: Path, layer: str) -> None:
    try:
        gdf.to_file(path, layer=layer, driver="GPKG")
    except Exception as exc:
        raise WriteFailureError(f"Cannot write {layer} to {path}: {exc}") from exc
