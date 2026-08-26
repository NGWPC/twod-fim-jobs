from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field
from twod_fim_jobs.utils.hashing import hash_file


class Domain(BaseModel):
    """Computed domain realization."""

    model_config = ConfigDict(extra="forbid")

    bbox: tuple[float, float, float, float] = Field(
        description="Domain bbox in native CRS [west, south, east, north].",
        examples=[[-2059240.0, 2806980.0, -2055870.0, 2810760.0]],
    )
    anchor: tuple[float, float] = Field(
        description="Grid-snapped reach centroid in native CRS [x, y]; origin the offsets are measured from.",
        examples=[[-2058170.0, 2809120.0]],
    )
    offsets: tuple[float, float, float, float] = Field(
        description="[N, S, E, W] grid-snapped offsets in CRS units; matches domain_token.",
        examples=[[164.0, 214.0, 230.0, 107.0]],
    )

    @property
    def offset_str(self) -> str:
        # TODO: Check with team whether this rounding can lead to issues.
        return "N{}S{}E{}W{}".format(*[int(i) for i in self.offsets])

    @property
    def area(self) -> float:
        return (self.bbox[2] - self.bbox[0]) * (self.bbox[3] - self.bbox[1])


class GridProperties(BaseModel):
    """Grid dimensions of the model domain."""

    model_config = ConfigDict(extra="forbid")

    rows: int = Field(
        description="Number of rows in the model domain", gt=0, examples=[378]
    )
    cols: int = Field(
        description="Number of columns in the model domain", gt=0, examples=[337]
    )


class Asset(BaseModel):
    """One output file entry: role-keyed href plus integrity metadata."""

    model_config = ConfigDict(extra="forbid")

    href: str = Field(
        description="Path to the file, e.g. dem.tif, cl.geojson, or a full s3:// URI.",
        examples=["s3://bucket/prefix/file.ext"],
    )
    checksum: str = Field(
        pattern=r"^[0-9a-f]{16}$",
        description="First 16 hex of SHA-256 (64-bit). File-integrity checksums; longer than identity hashes to allow cross-corpus content comparison.",
        examples=["a1b2c3d4e5f6a7b8"],
    )
    source_url: str | None = Field(
        default=None,
        description="External provenance URL (DEM/land-cover endpoint, or db_uri). null for purely computed assets.",
        examples=["https://bucket.s3.amazonaws.com/prefix/source.ext"],
    )
    retrieved: datetime | None = Field(
        default=None,
        description="When the source was fetched, if applicable.",
        examples=["2026-08-06T22:17:07.406819Z"],
    )
    derived: bool = Field(
        default=False,
        description="True for outputs regenerable from sources (terrain, roughness); subject to S3 lifecycle deletion.",
        examples=[True],
    )

    @classmethod
    def from_file(cls, href: str, source_url: str, retrieved: datetime | None):
        checksum = hash_file(href, role_length=16)
        return cls(
            href=href,
            checksum=checksum,
            source_url=source_url,
            retrieved=retrieved,
        )
