from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer
import twod_fim_jobs
from twod_fim_jobs.utils.hashing import hash_file
from twod_fim_jobs.models.warnings import JobWarning


class TerminationCondition(StrEnum):
    VOLUME_CONVERGENCE = "volume_convergence"
    EDGE_ERROR = "edge_error"
    MAX_SIMULATION_TIME = "max_simulation_time"


class Domain(BaseModel):
    """Computed domain realization."""

    model_config = ConfigDict(extra="forbid")

    bbox: tuple[float, float, float, float] = Field(
        description="Domain bbox in native CRS [west, south, east, north]."
    )
    anchor: tuple[float, float] = Field(
        description="Grid-snapped reach centroid in native CRS [x, y]; origin the offsets are measured from."
    )
    offsets: tuple[float, float, float, float] = Field(
        description="[N, S, E, W] grid-snapped offsets in CRS units; matches domain_token."
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

    rows: int = Field(description="Number of rows in the model domain", gt=0)
    cols: int = Field(description="Number of columns in the model domain", gt=0)


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


class RunConfig(BaseModel):
    """Solver execution settings; constructed once per job and passed to every worker."""

    model_config = ConfigDict(frozen=True)

    sim_time_seconds: float
    save_interval_seconds: float
    mass_interval_seconds: float
    initial_tstep_seconds: float
    use_cuda: bool = False
    use_elevoff: bool = False
    initial_state_path: Path | None = None


class BoundaryCheckResult(BaseModel):
    wse_0: float = Field(description="WSE at the upstream endpoint cell")
    wse_1: float = Field(description="WSE at the downstream endpoint cell")
    wse_range: float = Field(
        description="wse_0 - wse_1; the width of the valid WSE window"
    )
    n_wetted_edge_cells: int = Field(
        description="Number of edge cells with any water (non-nan WSE)"
    )
    n_violating_edge_cells: int = Field(
        description="Number of edge cells whose WSE falls within [wse_1, wse_0]"
    )
    n_violating_top: int = Field(description="Violating cells on the top edge (row 0)")
    n_violating_bottom: int = Field(
        description="Violating cells on the bottom edge (last row)"
    )
    n_violating_left: int = Field(
        description="Violating cells on the left edge (col 0, interior rows)"
    )
    n_violating_right: int = Field(
        description="Violating cells on the right edge (last col, interior rows)"
    )
    worst_violating_wse: float = Field(
        description="WSE of the edge cell furthest from wse_0 within the violation window; nan if no violations"
    )
    closest_edge_margin: float = Field(
        description="Minimum distance of any wetted non-violating edge cell WSE to the nearest range boundary; nan if all wetted edge cells are violating"
    )
    error: str | None = Field(
        default=None,
        description="Error message if a boundary violation was detected, else None",
    )


class ConvergenceResult(BaseModel):
    volume_convergence: float
    boundary_check: BoundaryCheckResult | None = None
    model_running: bool


class ScenarioProperties(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nominal_wse: float = Field(
        description="Nominal water surface elevation measured along the reach's u/s stage transfer line"
    )
    scenario_diagnostics: list[ConvergenceResult] = Field(
        description="Diagnostics for each model print interval"
    )
    termination_condition: TerminationCondition = Field(
        description="How the scenario run was terminated"
    )


class ScenarioAssets(BaseModel):
    model_config = ConfigDict(extra="forbid")

    depth: Path = Field(description="Path of the depth grid at the final timestep")
    inundation_polygon: Path = Field(
        description="Path of the inundated area polygon at the final timestep"
    )
    stage_transfer_line: Path = Field(description="Path of the stage transfer line")
    zarr_store: Path | None = Field(
        default=None, description="Path of the zarr with depths at each print interval"
    )

    @field_serializer(
        "depth", "inundation_polygon", "stage_transfer_line", "zarr_store"
    )
    def serialize_path(self, v) -> str | None:
        return str(v) if v is not None else None


class ScenarioRunInputs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ds_slope: float = Field(
        description="Normal-depth slope for the downstream boundary condition"
    )
    ds_wse: float | None = Field(
        default=None,
        description="Nominal water surface elevation along the downstream stage trnasfer line",
    )
    us_discharge: float = Field(description="Upstream inflow discharge in cms")
    scenario_dir_name: str = Field(
        description="Output directory name for this scenario's assets"
    )
    volume_convergence_tolerance: float | None = Field(
        description="Volume convergence threshold; None disables the check"
    )
    allow_water_on_edges: bool = Field(
        description="Whether to ignore or terminate when water pools on an invalid edge"
    )
    outflow_area_polygon_path: str = Field(
        description="Path to polygon marking where the normal-depth boundary condition is applied"
    )
    inflow_line_path: str = Field(
        description="Path to lineString defining the upstream inflow boundary"
    )
    centerline_path: str = Field(
        description="Path to the reach centerline used to determine WSE sample points for domain expansion criteria"
    )
    domain: Domain = Field(description="Domain configuration for the hydraulic model")
    grid: GridProperties = Field(description="Grid properties for the hydraulic model")
    run_config: RunConfig = Field(description="Solver runtime configuration")
    terrain_path: str = Field(description="Path to the terrain raster")
    roughness_path: str = Field(description="Path to the roughness raster")
    out_dir: str = Field(description="Directory where solver output files are written")


class ScenarioRunManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["model"] = Field(
        default="model", description="Discriminator vs. a run record."
    )
    hash_algo: Literal["sha256"] = Field(
        default="sha256",
        description="Hash function for every hash/checksum in this document. Truncation length is per-field (see build_model-design.md).",
    )
    twod_fim_version: str = Field(
        default=twod_fim_jobs.__version__,
        description="Producer software version (provenance).",
    )
    created_at: datetime = Field(
        description="Run completion time (UTC). model.json is written last."
    )
    reach_id: int = Field(description="Primary key for the reach in the reach db")
    identity_hash: str = Field(
        pattern=r"^[0-9a-f]{8}$",
        description="Hash of the identity object. Stable across domain changes; results group under it.",
    )
    domain_code: str = Field(
        pattern=r"^N(0|[1-9][0-9]*)S(0|[1-9][0-9]*)E(0|[1-9][0-9]*)W(0|[1-9][0-9]*)$",
        description="Domain realization: grid-snapped N/S/E/W offsets in CRS units from the anchor. A grid-reference code, not a hash.",
    )
    model_id: str = Field(
        pattern=r"^[0-9a-f]{8}_N(0|[1-9][0-9]*)S(0|[1-9][0-9]*)E(0|[1-9][0-9]*)W(0|[1-9][0-9]*)$",
        description="<identity_hash>+<domain_code>. Also the folder name.",
    )
    inputs: ScenarioRunInputs = Field(description="Inputs used to run the model")

    properties: ScenarioProperties = Field(
        description="Computed values during the run."
    )
    assets: ScenarioAssets = Field(
        description="One entry per output (model.json excluded), keyed by role; hrefs are the flat files under <id>/."
    )
    warnings: list[JobWarning] = Field(
        default=[],
        description="Non-fatal check results; the scenario run still completes and writes scenario.json.",
    )


class ScenarioWorkerManifest(BaseModel):
    """Intermediate result produced by a single scenario worker before publishing."""

    model_config = ConfigDict(extra="forbid")

    nominal_wse: float = Field(
        description="Nominal water surface elevation measured along the reach's u/s stage transfer line"
    )
    ds_wse: float | None = Field(
        description="Nominal water surface elevation along the downstream stage transfer line"
    )
    ds_slope: float = Field(
        description="Normal-depth slope for the downstream boundary condition"
    )
    us_discharge: float = Field(description="Upstream inflow discharge in cms")
    allow_water_on_edges: bool = Field(
        description="Whether to ignore or terminate when water pools on an invalid edge"
    )
    dir_name: str = Field(
        description="Output directory name for this scenario's assets"
    )
    depth_path: Path = Field(description="Path of the depth grid at the final timestep")
    inundation_polygon_path: Path = Field(
        description="Path of the inundated area polygon at the final timestep"
    )
    stl_path: Path = Field(description="Path of the stage transfer line")
    zarr_path: Path | None = Field(
        default=None, description="Path of the zarr with depths at each print interval"
    )
    scenario_diagnostics: list[ConvergenceResult] = Field(
        description="Diagnostics for each model print interval"
    )
    termination_condition: TerminationCondition = Field(
        description="How the scenario run was terminated"
    )
    run_config: RunConfig = Field(description="Solver runtime configuration")
