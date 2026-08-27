import hashlib
import json
from pathlib import Path

import fsspec
from shapely.geometry.base import BaseGeometry
from twod_fim_jobs.consts import HASH_ALGORITHM


def hash_dict(
    d: dict, algorithm: str = HASH_ALGORITHM, role_length: int | None = None
) -> str:
    """Hash a Python dictionary."""
    hasher = hashlib.new(algorithm)

    # Create canonical json (sorted keys and no insignificant whitespace)
    d_str = json.dumps(d, sort_keys=True, separators=(",", ":"))

    # Hash
    hasher.update(d_str.encode())
    hash_str = hasher.hexdigest().lower()

    if role_length:
        hash_str = hash_str[:role_length]
    return hash_str


def hash_str(
    s: str, algorithm: str = HASH_ALGORITHM, role_length: int | None = None
) -> str:
    """Hash a string."""
    hasher = hashlib.new(algorithm)

    hasher.update(s.encode())
    hash_str = hasher.hexdigest().lower()

    if role_length:
        hash_str = hash_str[:role_length]
    return hash_str


def hash_geometry(
    geom: BaseGeometry, algorithm: str = HASH_ALGORITHM, role_length: int | None = None
) -> str:
    """Hash the WKT rep of a shapely geometry."""
    return hash_str(geom.wkt, algorithm, role_length)


def hash_file(
    href: str | Path, algorithm: str = HASH_ALGORITHM, role_length: int | None = None
) -> str:
    """Hash a local or remote file (s3://, https://, etc.)."""
    hasher = hashlib.new(algorithm)
    fs, path = fsspec.url_to_fs(str(href))
    with fs.open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            hasher.update(chunk)
    hash_str = hasher.hexdigest().lower()
    if role_length:
        hash_str = hash_str[:role_length]
    return hash_str
