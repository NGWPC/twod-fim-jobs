from datetime import datetime
from typing import ClassVar

from pydantic import BaseModel

from twod_fim_jobs.utils.hashing import hash_file

### MODELS SHARED ACROSS JOBS ###


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


### WARNINGS ###


class JobWarning(BaseModel):
    code: str
    message: str


class CenterlineInflowMultiIntersectionWarning(JobWarning):
    """Warning emitted when inflow intersects the centerline more than once."""

    code: ClassVar[str] = "centerline_inflow_multi_intersection"
    intersection_points: list[tuple[float, float]]

    def __init__(self, intersection_points: list[tuple[float, float]]):
        self.intersection_points = intersection_points
        point_text = ", ".join(
            f"({pt[0]:.3f}, {pt[1]:.3f})" for pt in intersection_points
        )
        message = "Inflow line intersected centerline at multiple points" + (
            f": {point_text}" if point_text else "."
        )
        super().__init__(message=message)
