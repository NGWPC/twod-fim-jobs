from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from shapely import Point

from twod_fim_jobs.data_models import Warning


@dataclass(slots=True)
class GenericJobWarning:
    """Base warning type used by workflows before serialization."""

    message: str
    code: ClassVar[str] = "generic_job_warning"

    def to_manifest(self) -> Warning:
        """Convert to the canonical manifest warning model."""
        return Warning(code=self.code, message=self.message)


class CenterlineInflowMultiIntersectionWarning(GenericJobWarning):
    """Warning emitted when inflow intersects the centerline more than once."""

    code: ClassVar[str] = "centerline_inflow_multi_intersection"
    intersection_points: list[Point]

    def __init__(self, intersection_points: list[Point]):
        self.intersection_points = intersection_points
        point_text = ", ".join(
            f"({point.x:.3f}, {point.y:.3f})" for point in intersection_points
        )
        message = "Inflow line intersected centerline at multiple points" + (
            f": {point_text}" if point_text else "."
        )
        super().__init__(message=message)
