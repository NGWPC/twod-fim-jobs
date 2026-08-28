import json
import logging
import os
import shutil
from pathlib import Path
from typing import IO, cast
from urllib.parse import urlparse

import fsspec
import geopandas as gpd

from twod_fim_jobs.consts import (
    ASSET_CACHE_DIR,
    MAX_ASSET_CACHE_SIZE_GB,
    REACH_FIELDS,
    REACH_FIELDS_PARQUET,
    REACH_ID_FIELD,
)
from twod_fim_jobs.exceptions import (
    DuplicateReachError,
    InvalidAttributeError,
    ReachDatasetUnavailable,
    ReachNotFoundError,
    WriteFailureError,
)
from twod_fim_jobs.models.common import Asset

logger = logging.getLogger(__name__)


def read_reaches(
    reach_network_path: str, reach_ids: list[int], epsg: int | None = None
) -> gpd.GeoDataFrame:
    """Fetch specific reaches from the reach network GeoParquet, by id."""
    if not reach_ids:
        return gpd.GeoDataFrame()

    try:
        gdf = gpd.read_parquet(
            reach_network_path,
            columns=REACH_FIELDS_PARQUET,
            filters=[
                (REACH_ID_FIELD, "in", list(reach_ids))
            ],  # predicate pushed down to parquet reader
        )
    except FileNotFoundError as e:
        raise ReachDatasetUnavailable(
            f"Reach network not found: {reach_network_path}"
        ) from e
    except Exception as e:
        raise ReachDatasetUnavailable(
            f"Cannot read reach network {reach_network_path}: {e}"
        ) from e

    missing = set(REACH_FIELDS) - set(gdf.columns)
    if missing:
        raise InvalidAttributeError(
            f"Reach network {reach_network_path} is missing required fields: {sorted(missing)}"
        )
    if epsg is not None and gdf.crs is not None and gdf.crs.to_epsg() != epsg:
        raise InvalidAttributeError(
            f"Expected CRS EPSG:{epsg}, got {gdf.crs.to_string()} from '{reach_network_path}'"
        )
    return gdf


def query_reach(
    reach_id: int, reach_network_path: str, epsg: int | None = None
) -> gpd.GeoDataFrame:
    """Load one reach's geometry and attributes from the reach network."""
    gdf = read_reaches(reach_network_path, [reach_id], epsg=epsg)
    if gdf.empty:
        raise ReachNotFoundError(f"No reach found for {REACH_ID_FIELD}={reach_id}")
    if len(gdf) > 1:
        raise DuplicateReachError(f"Found multiple reaches for reach_id={reach_id}")
    return gdf


def check_file_exists(uri: str) -> bool:
    """Check whether a local or remote file exists."""
    fs, path = fsspec.core.url_to_fs(uri)
    return fs.exists(path)


def copy_file(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
    """Copy a file from src to dst, supporting local paths and S3 URIs."""
    try:
        with fsspec.open(str(src), "rb") as f_src, fsspec.open(str(dst), "wb") as f_dst:
            shutil.copyfileobj(cast(IO[bytes], f_src), cast(IO[bytes], f_dst))
    except Exception as e:
        raise WriteFailureError(str(e)) from e


def copy_dir(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
    """Recursively copy a directory tree from src to dst, supporting local paths and S3 URIs."""
    src, dst = str(src), str(dst)
    src_fs, src_path = fsspec.core.url_to_fs(src)
    dst_fs, dst_path = fsspec.core.url_to_fs(dst)
    try:
        for dirpath, _, filenames in src_fs.walk(src_path):
            rel = dirpath[len(src_path) :].lstrip("/")
            dst_dir = f"{dst_path}/{rel}" if rel else dst_path
            dst_fs.makedirs(dst_dir, exist_ok=True)
            for fname in filenames:
                src_file = f"{dirpath}/{fname}"
                dst_file = f"{dst_dir}/{fname}"
                with (
                    src_fs.open(src_file, "rb") as f_src,
                    dst_fs.open(dst_file, "wb") as f_dst,
                ):
                    shutil.copyfileobj(f_src, f_dst)
    except Exception as e:
        raise WriteFailureError(str(e)) from e


def read_json(path: str) -> str:
    """Read a JSON file from a local path or S3 URI and return its contents as a string."""
    with fsspec.open(path, "r") as f:
        return cast(IO[str], f).read()


def write_json(path: str, content: str, indent: int | None = 4) -> None:
    """Write a JSON string to a local path or S3 URI."""
    try:
        if indent is not None:
            content = json.dumps(json.loads(content), indent=indent)
        with fsspec.open(path, "w") as f:
            cast(IO[str], f).write(content)
    except Exception as e:
        raise WriteFailureError(str(e)) from e


class AssetCache:
    """Manages pulling remote assets to local cache."""

    def __init__(self, cache_root: Path):
        """Initialize class."""
        self.cache_root = cache_root

    def materialize_path(self, asset: Asset) -> Path:
        """Return local path to an asset, caching from remote, if necessary."""
        parsed = urlparse(asset.href)
        if not parsed.scheme or parsed.scheme in {"", "file"}:
            return Path(asset.href)
        return self._cache_asset(asset)

    def _cache_asset(self, asset: Asset) -> Path:
        """Copy a remote asset to cache_root."""
        filename = Path(urlparse(asset.href).path).name
        cached_path = self.cache_root / asset.checksum / filename

        if cached_path.exists():
            return cached_path

        max_bytes = int(MAX_ASSET_CACHE_SIZE_GB) * 1024**3
        self._evict_to_fit(max_bytes)

        cached_path.parent.mkdir(parents=True, exist_ok=True)
        copy_file(asset.href, cached_path)
        return cached_path

    def _evict_to_fit(self, max_bytes: int) -> None:
        """Remove oldest cached files until total cache size is under max_bytes."""
        if not self.cache_root.exists():
            return
        files = sorted(self.cache_root.rglob("*"), key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files if p.is_file())
        for path in files:
            if total <= max_bytes:
                break
            if path.is_file():
                size = path.stat().st_size
                path.unlink()
                logger.info("Evicted cached asset: %s", path)
                total -= size


ASSET_CACHE = AssetCache(Path(ASSET_CACHE_DIR))
