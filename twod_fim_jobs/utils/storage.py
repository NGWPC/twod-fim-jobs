from urllib.parse import urlparse

import geopandas as gpd
from sqlalchemy.exc import ArgumentError, OperationalError
from sqlalchemy import create_engine, inspect, text
from twod_fim_jobs.consts import (
    DA_FIELD,
    REACH_FIELDS,
    REACH_TABLE,
    REACH_ID_FIELD,
    REACH_TO_ID_FIELD,
)
from twod_fim_jobs.exceptions import (
    DuplicateReachError,
    InvalidAttributeError,
    ReachNotFoundError,
    ReachDatasetUnavailable,
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


def query_database(sql: str, db_uri: str) -> gpd.GeoDataFrame:
    scheme = urlparse(db_uri).scheme
    if scheme in {"postgresql", "postgres"}:
        engine = create_engine(db_uri)
        gdf = gpd.read_postgis(sql, engine, geom_col="geom")
    elif scheme == "sqlite":
        path = db_uri.removeprefix("sqlite:///")
        gdf = gpd.read_file(path, sql=sql)
    else:
        raise ValueError(f"scheme '{scheme}' is not supported for db_uri '{db_uri}'")
    return gdf


def query_upstream_reach(
    reach_id: int, db_uri: str, layer: str = REACH_TABLE
) -> list[list[int], gpd.GeoDataFrame]:
    # Validate 1
    fields = [REACH_ID_FIELD, REACH_TO_ID_FIELD, DA_FIELD, "geom"]
    validate_db_connection(db_uri=db_uri, layer=layer, fields=fields)

    # Query
    reach_fields_str = ", ".join(fields)
    sql = (
        f"SELECT {reach_fields_str} FROM {layer} WHERE {REACH_TO_ID_FIELD} = {reach_id}"
    )
    gdf = query_database(sql, db_uri)

    # Validate 2
    if gdf.empty:
        raise ReachNotFoundError(f"No reach found for {REACH_TO_ID_FIELD}={reach_id}")

    us_mainstem = gdf[gdf[DA_FIELD] == gdf[DA_FIELD].max()].iloc[:1]
    us_reach_ids = gdf[REACH_ID_FIELD].to_list()
    return us_reach_ids, us_mainstem


def query_reach(
    reach_id: int, db_uri: str, layer: str = REACH_TABLE
) -> gpd.GeoDataFrame:
    """Load reach geometry and attributes from a reach provider."""
    # Validate 1
    validate_db_connection(db_uri=db_uri, layer=layer, fields=REACH_FIELDS)

    # Query
    reach_fields_str = ", ".join(REACH_FIELDS)
    sql = f"SELECT {reach_fields_str} FROM {layer} WHERE {REACH_ID_FIELD} = {reach_id}"
    gdf = query_database(sql, db_uri)

    # Validate 2
    if gdf.empty:
        raise ReachNotFoundError(f"No reach found for {REACH_ID_FIELD}={reach_id}")
    if len(gdf) > 1:
        raise DuplicateReachError(f"Found multiple reaches for reach_id={reach_id}")
    return gdf


def check_model_exists(model_uri: str) -> bool:
    # TODO: Implement this
    return False
