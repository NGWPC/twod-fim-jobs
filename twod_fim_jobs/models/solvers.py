from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
import twod_fim_jobs
from twod_fim_jobs.consts import (
    DEFAULT_ELEVOFF,
    DEFAULT_INITIAL_TSTEP_SECONDS,
    DEFAULT_MASS_INTERVAL_SECONDS,
    RUN_NAME_KWSE_ROUNDING_PRECISION,
    RUN_NAME_Q_ROUNDING_PRECISION,
    RUN_NAME_SLOPE_ROUNDING_PRECISION,
    SCENARIO_MANIFEST_FILENAME,
    USE_CUDA,
)
from twod_fim_jobs.models.warnings import JobWarning
from twod_fim_jobs.models.common import Asset, GridProperties, Domain


class SolverInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Solver name", examples=["lisflood"])
    version: str = Field(description="Solver version string", examples=["8.0.0"])


class RunIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sdr_commit_id: str = Field(
        description="Git commit SHA of the SDR used to run the model",
        examples=["826a602ddcaf58bf4081dc04b65ba15b82cc8c8a"],
    )
    solver: SolverInfo = Field(description="Solver name and version")


class TerminationCondition(StrEnum):
    """Reason that a solver run ended."""

    VOLUME_CONVERGENCE = "volume_convergence"
    EDGE_ERROR = "edge_error"
    MAX_SIMULATION_TIME = "max_simulation_time"
    MAX_WALL_TIME = "max_wall_time"


class RunConfig(BaseModel):
    """Solver execution settings; constructed once per job and passed to every worker."""

    model_config = ConfigDict(frozen=True)

    sim_time_seconds: float = Field(examples=[36000.0])
    save_interval_seconds: float = Field(examples=[3600.0])
    mass_interval_seconds: float = Field(
        examples=[60.0], default=DEFAULT_MASS_INTERVAL_SECONDS
    )
    initial_tstep_seconds: float = Field(
        examples=[0.5], default=DEFAULT_INITIAL_TSTEP_SECONDS
    )
    use_cuda: bool = Field(default=USE_CUDA, examples=[True])
    use_elevoff: bool = Field(default=DEFAULT_ELEVOFF, examples=[False])
    volume_convergence_tolerance: float = Field(
        default=0,
        description="Volume increase in the reach as a percent of inflow below which model is considered steady",
        examples=[0.1],
    )
    allow_water_on_edges: bool = Field(
        default=False,
        description="Whether to ignore or terminate when water pools on an invalid edge",
        examples=[False],
    )
    max_simulation_wall_time_seconds: float = Field(examples=[36000.0])
    save_zarr: bool = Field(
        default=False,
        description="Whether or not to generate and save a zarr file with wse and depth at each print interval",
        examples=[False],
    )


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
    volume_convergence: float = Field(
        description="Ratio of net volume change to total inflow volume over the last mass interval; lower is more converged.",
        examples=[0.02],
    )
    boundary_check: BoundaryCheckResult | None = Field(
        default=None,
        description="Edge-boundary violation diagnostics; None if the check was not performed.",
    )
    model_running: bool = Field(
        description="True if the solver was still running at this print interval; False if it had already terminated.",
        examples=[True],
    )


class _BCBase(BaseModel):
    vector: Asset


class QFixBC(_BCBase):
    bc_type: Literal["QFIX"] = "QFIX"
    value: float


class HFixBC(_BCBase):
    bc_type: Literal["HFIX"] = "HFIX"
    value: float


class FreeBC(_BCBase):
    bc_type: Literal["FREE"] = "FREE"
    value: float


class TransferBC(_BCBase):
    bc_type: Literal["TRANSFER"] = "TRANSFER"
    value: Asset


BoundaryCondition = Annotated[
    QFixBC | HFixBC | FreeBC | TransferBC, Field(discriminator="bc_type")
]


class BoundaryConditionElement(BaseModel):
    """Single boundary condition element with its type, value, and grid location."""

    model_config = ConfigDict(frozen=True)

    element_type: str  # "P" for point; "N"/"S"/"E"/"W" for cardinal edge
    bc_type: Literal["QFIX", "HFIX", "FREE", "TRANSFER"]
    value: float | str
    x_coord: float
    y_coord: float
    x_ind: int
    y_ind: int


class PostProcessResult(BaseModel):
    depth_path: Path = Field(description="Local path to the depth grid output file")
    inundation_polygon_path: Path = Field(
        description="Local path to the inundation polygon output file"
    )
    stl_path: Path = Field(
        description="Local path to the stage transfer line output file"
    )
    nominal_wse: float = Field(
        description="Nominal water surface elevation achieved at the reach upstream point"
    )
    sim_time: float = Field(
        description="Simulation time (model time)in seconds at the final timestep"
    )
    zarr_path: Path | None = Field(
        default=None,
        description="Local path to the zarr store, if generated",
    )


class SolveScenarioResults(BaseModel):
    convergence_results: list[ConvergenceResult]
    termination_condition: TerminationCondition
    wall_time: float


class RunScenarioResults(BaseModel):
    """Convenience holder for solver results."""

    convergence_results: list[ConvergenceResult] = Field(
        description="Convergence status after each raster output"
    )
    termination_condition: TerminationCondition = Field(
        description="The reason the simulation ended."
    )
    wall_time: float = Field(description="How long the model ran in wall time")
    nominal_wse: float = Field(
        description="Nominal water surface elevation achieved at the reach upstream point"
    )
    sim_time: float = Field(
        description="Simulation time (model time)in seconds at the final timestep"
    )


class RunScenarioInputs(BaseModel):
    domain: Domain
    grid_properties: GridProperties
    terrain: Asset
    roughness: Asset
    boundary_conditions: list[BoundaryCondition]
    hot_start: Asset | None
    run_config: RunConfig
    base_out_dir: str
    reach_id: int
    model_id: str
    tmp_dir: Path
    centerline: Asset

    @model_validator(mode="after")
    def _validate_boundary_conditions(self) -> "RunScenarioInputs":
        # Currently limited to a single BC per type; may be relaxed in the future
        if len(self.kwse_bcs) == 0 and len(self.nd_bcs) == 0:
            raise ValueError("Exactly one of 'kwse' or 'nd' must be provided")
        if len(self.kwse_bcs) > 1:
            raise ValueError(
                "At most one 'HFIX' or 'TRANSFER' boundary condition is supported"
            )
        if len(self.nd_bcs) > 1:
            raise ValueError("At most one 'FREE' boundary condition is supported")
        if len(self.q_bcs) > 1:
            raise ValueError("At most one 'QFIX' boundary condition is supported")
        return self

    @property
    def scenario_out_dir(self) -> str:
        """Derive path where this scenario's data will be saved."""
        from twod_fim_jobs.hydraulic_solvers.identities import get_run_identity_hash

        run_id_hash = get_run_identity_hash()
        return f"{self.base_out_dir}/{self.reach_id}/{self.model_id}/{run_id_hash}/{self.scenario_dir_name}"

    @property
    def manifest_href(self) -> str:
        """Derive path where this scenario manifest will be saved."""
        return f"{self.scenario_out_dir}/{SCENARIO_MANIFEST_FILENAME}"

    @property
    def working_dir(self) -> Path:
        """Path where this run's data will live until it is copied to its final location."""
        return self.tmp_dir / self.scenario_dir_name

    @property
    def kwse_bcs(self) -> list[BoundaryCondition]:
        """Any boundary conditions with type HFIX or TRANSFER."""
        return [
            i
            for i in self.boundary_conditions
            if i.bc_type == "TRANSFER" or i.bc_type == "HFIX"
        ]

    @property
    def nd_bcs(self) -> list[FreeBC]:
        """Any boundary conditions with type FREE."""
        return [i for i in self.boundary_conditions if i.bc_type == "FREE"]

    @property
    def q_bcs(self) -> list[QFixBC]:
        """Any boundary conditions with type QFIX."""
        return [i for i in self.boundary_conditions if i.bc_type == "QFIX"]

    @property
    def scenario_dir_name(self) -> str:
        """Directory name used to house this scenario's assets."""
        if len(self.kwse_bcs) == 0:
            nd = self.nd_bcs[0].value
            ds_str = f"nd={f'{nd:.{RUN_NAME_SLOPE_ROUNDING_PRECISION}e}'.replace('-', '').replace('e', 'E')}"
        else:
            kwse = self.kwse_bcs[0].value
            ds_str = f"kwse={f'{kwse:.{RUN_NAME_KWSE_ROUNDING_PRECISION}f}'}"
        q = self.q_bcs[0].value
        us_str = f"q={q:.{RUN_NAME_Q_ROUNDING_PRECISION}f}"
        return f"{ds_str}/{us_str}"

    @property
    def scenario_code(self) -> str:
        """Scenario code for the run, e.g. KWSE200.2Q1000."""
        if len(self.kwse_bcs) == 0:
            nd = self.nd_bcs[0].value
            ds_str = f"ND{f'{nd:.{RUN_NAME_SLOPE_ROUNDING_PRECISION}e}'.replace('-', '').replace('e', 'E')}"
        else:
            kwse = self.kwse_bcs[0].value
            ds_str = f"KWSE{f'{kwse:.{RUN_NAME_KWSE_ROUNDING_PRECISION}f}'}"
        q = self.q_bcs[0].value
        us_str = f"Q{q:.{RUN_NAME_Q_ROUNDING_PRECISION}f}"
        return f"{ds_str}{us_str}"

    @property
    def inflow(self) -> float:
        """Total inflow to model."""
        return sum([i.value for i in self.q_bcs])


### SCENARIO MANIFEST CLASSES ###


class ScenarioAssets(BaseModel):
    model_config = ConfigDict(extra="forbid")

    depth: Asset = Field(
        description="Depth grid at the final timestep",
    )
    inundation_polygon: Asset = Field(
        description="Inundated area polygon at the final timestep",
    )
    stage_transfer_line: Asset = Field(
        description="Stage transfer line",
    )
    zarr_store: Asset | None = Field(
        default=None,
        description="Zarr store with depths at each print interval",
    )


class RunScenarioManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["run"] = Field(
        default="run", description="Discriminator vs. a run record."
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
        description="Run completion time (UTC). model.json is written last.",
        examples=["2026-08-06T22:17:07.406819Z"],
    )
    reach_id: int = Field(
        description="Primary key for the reach in the reach db",
        examples=[1257410937935512],
    )
    identity_hash: str = Field(
        pattern=r"^[0-9a-f]{8}$",
        description="Hash of the identity object. Stable across domain changes; results group under it.",
        examples=["fceb20c6"],
    )
    scenario_code: str = Field(
        pattern=r"^[A-Z]+(\d+(\.\d+)?E[+\-]?\d+|\d+(\.\d+)?)Q\d+$",
        description="Scenario identifier: bc_type + bc_value (decimal or scientific notation) + 'Q' + integer discharge",
        examples=["KWSE200.2Q200", "ND1.0E04Q1000"],
    )
    model_id: str = Field(
        pattern=r"^[0-9a-f]{8}_N(0|[1-9][0-9]*)S(0|[1-9][0-9]*)E(0|[1-9][0-9]*)W(0|[1-9][0-9]*)$",
        description="<identity_hash>+<domain_code>. Also the folder name.",
        examples=["fceb20c6_N164S214E230W107"],
    )
    identity: RunIdentity = Field(
        description="Canonical identity of the solver environment used for this run."
    )
    self_href: str = Field(
        description="Location of this scenario manifest's json",
        examples=[
            "s3://twod-fim/version=v1/results/nd=1.0E02/q=1000/scenario_manifest.json"
        ],
    )
    inputs: RunScenarioInputs = Field(description="Inputs used to run the model")

    properties: RunScenarioResults = Field(
        description="Computed values during the run."
    )
    assets: ScenarioAssets = Field(
        description="One entry per output (model.json excluded), keyed by role; hrefs are the flat files under <id>/."
    )
    warnings: list[JobWarning] = Field(
        default=[],
        description="Non-fatal check results; the scenario run still completes and writes scenario.json.",
        examples=[[]],
    )

    @property
    def us_discharge(self) -> float:
        """Sum of inflow discharges."""
        return self.inputs.inflow
