from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from twod_fim_jobs.consts import (
    DEFAULT_MAX_WALL_TIME_SECONDS,
    DEFAULT_SIM_SAVE_INTERVAL_SECONDS,
    DEFAULT_SIM_TIME_SECONDS,
    DEFAULT_VOLUME_CONVERGENCE_THRESHOLD,
)
from twod_fim_jobs.hydraulic_solvers.identities import get_run_identity_hash
from twod_fim_jobs.models.warnings import JobWarning


class HotStart(BaseModel):
    upstream_discharge: float = Field(
        description="Flows applied at the top of the reach in cms", examples=[1000.0]
    )
    bc_type: Literal["ND", "KWSE"] = Field(
        default="KWSE",
        description="Kind of downstream condition the seed scenario ran with, this decides whether hotstart folder is nd=<slope> or kwse=<stage>. Defaults to KWSE.",
        examples=["ND"],
    )
    bc_value: float = Field(
        description="Value of the seed scenario's downstream condition: a normal-depth slope when bc_type is ND, a water surface elevation when it is KWSE",
        examples=[202.3],
    )
    identity_hash: str | None = Field(
        pattern=r"^[0-9a-f]{8}$",
        description="Hash of the run identity object. If none, assumed to be same as current scenario's.",
        examples=["fceb20c6"],
        default=get_run_identity_hash(),
    )


class KWSEScenario(BaseModel):
    """Definition of KWSE scenario configuration."""

    upstream_discharge: int = Field(
        gt=0,
        description="Flows applied at the top of the reach in cms. Must be greater than 0",
        examples=[1000.0],
    )
    bc_value: float = Field(
        description="Nominal water surface elevation at the bottom of the reach",
        examples=[202.3],
    )
    downstream_Scenario: str = Field(
        description="Path to the scenario manifest json for the model providing downstrem WSE forcing",
        examples=[
            "s3://twod-fim/version=v1/results/1257410937935512/fceb20c6_N164S214E230W107/results/nd=1.0E02/q=1000/scenario.json"
        ],
    )
    hotstart: HotStart | None = Field(
        default=None,
        description="Scenario used for initial water depths in the simulation.",
    )


class RunKWSEScenariosInputs(BaseModel):
    """Inputs for the run_kwse_scenarios workflow."""

    model_config = ConfigDict(extra="forbid")

    # Required
    model_manifest_path: str = Field(
        description="Path where the model manifest json is saved",
        examples=[
            "s3://twod-fim/version=v1/models/1257410937935512/fceb20c6_N164S214E230W107/model.json"
        ],
    )
    model_results_base_path: str = Field(
        description="Path where results will be saved",
        examples=["s3://twod-fim/version=v1/results"],
    )
    scenarios: list[KWSEScenario] = Field(
        description="A list of KWSE scenarios to run.  If hot start files will be needed "
    )

    # Optional
    volume_convergence_tolerance: float = Field(
        default=DEFAULT_VOLUME_CONVERGENCE_THRESHOLD,
        description="Volume increase in the reach as a percent of inflow below which model is considered steady",
        examples=[0.1],
    )
    allow_water_on_edges: bool = Field(
        default=False,
        description="Whether to ignore or terminate when water pools on an invalid edge",
        examples=[False],
    )
    max_simulation_length_seconds: float = Field(
        default=DEFAULT_SIM_TIME_SECONDS,
        description="Maximum time (in model seconds) that a model will be allowed to run before it is forcefully terminated",
        examples=[86400.0],
    )
    save_interval_seconds: float = Field(
        default=DEFAULT_SIM_SAVE_INTERVAL_SECONDS,
        description="Frequency (in model seconds) with which a model will export depth rasters",
        examples=[3600.0],
    )
    max_simulation_wall_time_seconds: float = Field(
        default=DEFAULT_MAX_WALL_TIME_SECONDS,
        description="Maximum time (in wall time) that a model will be allowed to run before it is forcefully terminated",
        examples=[60.0],
    )
    save_velocity: bool = Field(
        default=False,
        description="Whether or not to generate and save velocity tifs",
        examples=[False],
    )
    save_zarr: bool = Field(
        default=False,
        description="Whether or not to generate and save a zarr file with wse and depth at each print interval",
        examples=[False],
    )

    @field_validator("save_velocity")
    @classmethod
    def save_velocity_not_implemented(cls, v: bool) -> bool:
        if v is True:
            raise NotImplementedError("save_velocity is not yet implemented")
        return v


class RunKWSEScenariosResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifests: list[str] = Field(description="Paths to all generated scenario assets.")
    warnings: list[JobWarning] = Field(examples=[[]])
