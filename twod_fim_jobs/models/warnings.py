from typing import ClassVar

from pydantic import BaseModel


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
