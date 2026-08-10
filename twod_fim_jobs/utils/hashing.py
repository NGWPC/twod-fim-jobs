import hashlib
import json
from pathlib import Path
from shapely.geometry.base import BaseGeometry
from twod_fim_jobs.consts import HASH_ALGORITHM, SupportedSolver, SDR_COMMIT
from twod_fim_jobs.hydraulic_solvers.versions import get_model_version


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
    """Hash a file."""
    with open(href, "rb") as f:
        digest = hashlib.file_digest(f, algorithm)
    hash_str = digest.hexdigest()
    if role_length:
        hash_str = hash_str[:role_length]
    return hash_str


def get_run_identity_hash(solver: SupportedSolver) -> str:
    "Make canonical hash for solver and sdr commit id."
    version = get_model_version(solver)
    js = {
        "sdr_commit_id": SDR_COMMIT,
        "solver": {"name": solver.value, "version": version},
    }
    return hash_dict(js, role_length=8)
