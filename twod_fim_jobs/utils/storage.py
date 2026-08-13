import json
import os
import shutil
from typing import IO, cast
from urllib.parse import urlparse

import fsspec
import geopandas as gpd
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import ArgumentError, OperationalError
from twod_fim_jobs.consts import (
    DA_FIELD,
    REACH_FIELDS,
    REACH_ID_FIELD,
    REACH_TABLE,
    REACH_TO_ID_FIELD,
)
from twod_fim_jobs.exceptions import (
    DuplicateReachError,
    InvalidAttributeError,
    ReachDatasetUnavailable,
    ReachNotFoundError,
    WriteFailureError,
)


def validate_db_connection(db_uri: str, layer: str, fields: list[str]) -> None:
    # Validate connection
    try:
        engine = create_engine(db_uri)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except (ArgumentError, OperationalError) as e:
        raise ReachDatasetUnavailable(f"Cannot connect to database: {db_uri}") from e

    # Validate table exists
    inspector = inspect(engine)
    if layer not in inspector.get_table_names():
        raise ReachDatasetUnavailable(
            f"Table '{layer}' not found in database: {db_uri}"
        )

    # Validate table schema
    db_columns = {col["name"] for col in inspector.get_columns(layer)}
    missing = set(fields) - db_columns
    if missing:
        raise InvalidAttributeError(
            f"Table '{layer}' is missing required fields: {sorted(missing)}"
        )


def query_database(sql: str, db_uri: str, epsg: int | None = None) -> gpd.GeoDataFrame:
    scheme = urlparse(db_uri).scheme
    if scheme in {"postgresql", "postgres"}:
        engine = create_engine(db_uri)
        gdf = gpd.read_postgis(sql, engine, geom_col="geom")
        gdf = gdf.rename_geometry("geometry")
    elif scheme == "sqlite":
        path = db_uri.removeprefix("sqlite:///")
        gdf = gpd.read_file(path, sql=sql)
    else:
        raise ValueError(f"scheme '{scheme}' is not supported for db_uri '{db_uri}'")
    if epsg is not None and gdf.crs is not None and gdf.crs.to_epsg() != epsg:
        raise InvalidAttributeError(
            f"Expected CRS EPSG:{epsg}, got {gdf.crs.to_string()} from '{db_uri}'"
        )
    return gdf


def query_upstream_reach(
    reach_id: int, db_uri: str, epsg: int | None = None, layer: str = REACH_TABLE
) -> tuple[list[int], gpd.GeoDataFrame]:
    # Validate 1
    fields = [REACH_ID_FIELD, REACH_TO_ID_FIELD, DA_FIELD, "geom"]
    validate_db_connection(db_uri=db_uri, layer=layer, fields=fields)

    # Query
    reach_fields_str = ", ".join(fields)
    sql = (
        f"SELECT {reach_fields_str} FROM {layer} WHERE {REACH_TO_ID_FIELD} = {reach_id}"
    )
    gdf = query_database(sql, db_uri, epsg=epsg)

    if gdf.empty:
        return [], gpd.GeoDataFrame()

    us_mainstem = gdf[gdf[DA_FIELD] == gdf[DA_FIELD].max()].iloc[:1]
    us_reach_ids = gdf[REACH_ID_FIELD].to_list()
    return us_reach_ids, us_mainstem


def query_reach(
    reach_id: int, db_uri: str, epsg: int | None = None, layer: str = REACH_TABLE
) -> gpd.GeoDataFrame:
    """Load reach geometry and attributes from a reach provider."""
    # Validate 1
    validate_db_connection(db_uri=db_uri, layer=layer, fields=REACH_FIELDS)

    # Query
    reach_fields_str = ", ".join(REACH_FIELDS)
    sql = f"SELECT {reach_fields_str} FROM {layer} WHERE {REACH_ID_FIELD} = {reach_id}"
    gdf = query_database(sql, db_uri, epsg=epsg)

    # Validate 2
    if gdf.empty:
        raise ReachNotFoundError(f"No reach found for {REACH_ID_FIELD}={reach_id}")
    if len(gdf) > 1:
        raise DuplicateReachError(f"Found multiple reaches for reach_id={reach_id}")
    return gdf


def check_model_exists(model_uri: str) -> bool:
    # TODO: Implement this
    return False


def check_path_exists(uri: str) -> bool:
    """Whether a local path or S3 URI exists.

    An unreachable backend is reported as "does not exist" so a transient
    storage failure cannot be mistaken for an existing artifact — the caller
    would otherwise skip a rebuild it should have performed.
    """
    try:
        fs, path = fsspec.core.url_to_fs(uri)
        return bool(fs.exists(path))
    except Exception:
        return False


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
