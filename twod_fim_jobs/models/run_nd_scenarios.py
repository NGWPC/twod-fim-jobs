from pydantic import BaseModel, ConfigDict, Field, field_validator
from typing import Literal
from twod_fim_jobs.consts import (
    DEFAULT_SIM_TIME_SECONDS,
    DEFAULT_SIM_SAVE_INTERVAL_SECONDS,
    SupportedSolver,
)
from twod_fim_jobs.models.warnings import JobWarning


class RunNDScenariosInputs(BaseModel):
    """Inputs for the run_nd_scenarios workflow."""

    model_config = ConfigDict(extra="forbid")

    # Required
    model_manifest_path: str = Field(
        description="Path where the model manifest json is saved"
    )
    model_results_base_path: str = Field(description="Path where results will be saved")
    min_upstream_inflow: float = Field(
        description="Minimum of the target discharge range in cms"
    )
    max_upstream_inflow: float = Field(
        description="Maximum of the target discharge range in cms"
    )
    delta_upstream_inflow: float = Field(
        description="Discharge increment for adaptive step algorithm in cms"
    )
    ds_slope: float = Field(
        description="Slope value to apply for the downstream boundary condition in m/m"
    )
    outflow_area_polygon_path: str = Field(
        description="Path to a polygon that determines where normal depth boundary condition will be applied."
    )

    # Optional
    solver: str = Field(
        default="lisflood",
        description="Hydraulic solver used (e.g., lisflood or sfincs)",
    )
    volume_convergence_tolerance: float = Field(
        default=0,
        description="Volume increase in the reach as a percent of inflow below which model is considered steady",
    )
    allow_water_on_edges: bool = Field(
        default=False,
        description="Whether to ignore or terminate when water pools on an invalid edge",
    )
    max_simulation_length_seconds: float = Field(
        default=DEFAULT_SIM_TIME_SECONDS,
        description="Maximum time (in model seconds) that a model will be allowed to run before it is forcefully terminated",
    )
    save_interval_seconds: float = Field(
        default=DEFAULT_SIM_SAVE_INTERVAL_SECONDS,
        description="Frequency (in model seconds) with which a model will export depth rasters",
    )
    max_simulation_wall_time_minutes: float | None = Field(
        default=None,
        description="Maximum time (in wall time) that a model will be allowed to run before it is forcefully terminated",
    )
    save_velocity: bool = Field(
        default=False,
        description="Whether or not to generate and save velocity tifs",
    )
    save_zarr: bool = Field(
        default=False,
        description="Whether or not to generate and save a zarr file with wse and depth at each print interval",
    )

    @field_validator("save_velocity")
    @classmethod
    def save_velocity_not_implemented(cls, v: bool) -> bool:
        if v is True:
            raise NotImplementedError("save_velocity is not yet implemented")
        return v

    @field_validator("solver")
    @classmethod
    def solver_supported(cls, solver: str) -> str:
        try:
            SupportedSolver(solver)
        except ValueError:
            supported = [e.value for e in SupportedSolver]
            raise ValueError(f"Solver must be one of {supported}, got '{solver}'")
        return solver

    @property
    def solver_enum(self) -> SupportedSolver:
        """Get validated solver as enum."""
        return SupportedSolver(self.solver)


class AdaptiveStepComparisonResults(BaseModel):
    ref_us_discharge: float
    trial_us_discharge: float
    max_stage_diff: float
    median_stage_diff: float
    extent_diff: float
    result: Literal["reject_high", "reject_low", "accept"]


class RunNDScenariosResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_manifest_paths: list[str] = Field(
        description="Paths to the scenario_manifest.json file for each completed scenario"
    )
    scenario_comparison_results: list[AdaptiveStepComparisonResults | None] = Field(
        description="Adaptive step comparison results for each accepted scenario; None for the baseline and max-discharge scenarios"
    )
    warnings: list[JobWarning]
