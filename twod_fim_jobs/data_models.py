# This will eventually split out into more files once I have a better feel for contents.

from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel

import twod_fim_jobs
from twod_fim_jobs.utils.hashing import hash_file

### BUILD MODEL ###


class BuildModelInputs(BaseModel):
    reach_id: int
    db_uri: str
    base_output_path: str
    dem_source: str
    lulc_source: str
    other_geometries: list[str] | None
    domain_buffer: float
    grid_resolution: int
    walk_us_dist_pct: float
    epsg_code: int
    bankfull_width_multiplier: float
    lulc_lookup: dict[int, float]


class Domain(BaseModel):
    bbox: tuple[float, float, float, float]  # xmin, ymin, xmax, ymax
    anchor: tuple[float, float]  # x, y
    offsets: tuple[float, float, float, float]  # N, S, E, W

    @property
    def offset_str(self) -> str:
        # TODO: Check with team whether this rounding can lead to issues.
        return "N{}S{}E{}W{}".format(*[int(i) for i in self.offsets])


class Identity(BaseModel):
    sdr_commit: str
    reach_geom_hash: str
    grid_resolution: int
    epsg_code: int
    dem_source_inputs_hash: str
    lulc_source_inputs_hash: str
    lulc_lookup_dict_hash: str


class GridProperties(BaseModel):
    rows: int
    cols: int


class Properties(BaseModel):
    grid: GridProperties
    drainage_area_sqkm: float
    bankfull_width_m: float
    upstream_reach_ids: list[int]
    stream_order: int
    length_m: float
    slope: float
    downstream_reach_id: int
    upstream_mainstem_reach_id: int


class Asset(BaseModel):
    href: str
    checksum: str
    source_url: str | None
    retrieved: datetime | None = None

    @classmethod
    def from_file(cls, href: str, source_url: str, retrieved: datetime | None):
        checksum = hash_file(href, role_length=16)
        return cls(
            href=href,
            checksum=checksum,
            source_url=source_url,
            retrieved=retrieved,
        )


class Assets(BaseModel):
    terrain: Asset
    roughness: Asset
    centerline: Asset
    reach_centroid: Asset
    domain: Asset

    def normalize_hrefs(self, new_base: str) -> None:
        """Update the href for each asset such that they are all rooted in new_base."""
        base = PurePosixPath(new_base)

        for _, asset in self:
            filename = Path(asset.href).name
            asset.href = str(base / filename)


class JobWarning(BaseModel):
    code: str
    message: str


class ModelManifest(BaseModel):
    type: Literal["model"] = "model"
    hash_algo: Literal["sha256"] = "sha256"
    twod_fim_version: str = twod_fim_jobs.__version__
    created_at: datetime
    reach_id: int
    identity_hash: str
    domain_code: str
    model_id: str
    inputs: BuildModelInputs
    domain: Domain
    identity: Identity
    properties: Properties
    assets: Assets
    warnings: list[JobWarning]
