from datetime import datetime
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from twod_fim_jobs.utils.hashing import hash_file

### MODELS SHARED ACROSS JOBS ###


class Asset(BaseModel):
    """One output file entry: role-keyed href plus integrity metadata."""

    model_config = ConfigDict(extra="forbid")

    href: str = Field(
        description="Path to the file, e.g. dem.tif, cl.geojson, or a full s3:// URI."
    )
    checksum: str = Field(
        pattern=r"^[0-9a-f]{16}$",
        description="First 16 hex of SHA-256 (64-bit). File-integrity checksums; longer than identity hashes to allow cross-corpus content comparison.",
    )
    source_url: str | None = Field(
        description="External provenance URL (DEM/land-cover endpoint, or db_uri). null for purely computed assets."
    )
    retrieved: datetime | None = Field(
        default=None, description="When the source was fetched, if applicable."
    )
    derived: bool = Field(
        default=False,
        description="True for outputs regenerable from sources (terrain, roughness); subject to S3 lifecycle deletion.",
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


### WARNINGS ###


class JobWarning(BaseModel):
    code: ClassVar[str]
    message: str


class CenterlineInflowMultiIntersectionWarning(JobWarning):
    """Warning emitted when inflow intersects the centerline more than once."""

    code: ClassVar[str] = "centerline_inflow_multi_intersection"
    intersection_points: list[tuple[float, float]]

    def __init__(self, intersection_points: list[tuple[float, float]]):
        point_text = ", ".join(
            f"({pt[0]:.3f}, {pt[1]:.3f})" for pt in intersection_points
        )
        message = "Inflow line intersected centerline at multiple points" + (
            f": {point_text}" if point_text else "."
        )
        BaseModel.__init__(
            self, message=message, intersection_points=intersection_points
        )


class LargeDomainAreaWarning(JobWarning):
    """Warning emitted when the domain area exceeds the large-domain threshold."""

    code: ClassVar[str] = "large_domain_area"
    domain_area: float
    threshold: float

    def __init__(self, domain_area: float, threshold: float):
        message = f"Domain area ({domain_area:.0f} sq CRS units) exceeds threshold ({threshold:.0f} sq CRS units)."
        BaseModel.__init__(
            self, message=message, domain_area=domain_area, threshold=threshold
        )
