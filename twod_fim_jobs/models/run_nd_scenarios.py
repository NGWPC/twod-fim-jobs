from pydantic import BaseModel, ConfigDict, Field, field_validator
from typing import Literal
from twod_fim_jobs.consts import (
    ADAPTIVE_STEP_ALGORITHM_MIN_DELTA_Q,
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
        description="Path where the model manifest json is saved",
        examples=[
            "s3://twod-fim/version=v1/models/1257410937935512/fceb20c6_N164S214E230W107/model.json"
        ],
    )
    model_results_base_path: str = Field(
        description="Path where results will be saved",
        examples=["s3://twod-fim/version=v1/results/1257410937935512"],
    )
    min_upstream_inflow: float = Field(
        description="Minimum of the target discharge range in cms",
        examples=[100.0],
    )
    max_upstream_inflow: float = Field(
        description="Maximum of the target discharge range in cms",
        examples=[5000.0],
    )
    delta_upstream_inflow: float = Field(
        description="Discharge increment for adaptive step algorithm in cms",
        examples=[100.0],
    )
    ds_slope: float = Field(
        description="Slope value to apply for the downstream boundary condition in m/m",
        examples=[0.01],
    )
    outflow_area_polygon_path: str = Field(
        description="Path to a polygon that determines where normal depth boundary condition will be applied.",
        examples=["s3://twod-fim/version=v1/shared/outflow_area.geojson"],
    )

    # Optional
    solver: str = Field(
        default="lisflood",
        description="Hydraulic solver used (e.g., lisflood or sfincs)",
        examples=["lisflood"],
    )
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
    max_simulation_wall_time_minutes: float | None = Field(
        default=None,
        description="Maximum time (in wall time) that a model will be allowed to run before it is forcefully terminated",
        examples=[60.0],
    )
    adaptive_step_min_delta_q: float = Field(
        default=ADAPTIVE_STEP_ALGORITHM_MIN_DELTA_Q,
        description="Minimum sensitivity for Q in adaptive step algorithm.  If delta_q at the min and algorithm would reject high, trial is accepted instead.",
        examples=[10],
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
    ref_us_discharge: float = Field(examples=[1000.0])
    trial_us_discharge: float = Field(examples=[1100.0])
    max_stage_diff: float = Field(examples=[1.15])
    median_stage_diff: float = Field(examples=[1.03])
    extent_diff: float = Field(examples=[0.02])
    result: Literal["reject_high", "reject_low", "accept"] = Field(examples=["accept"])


class RunNDScenariosResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_manifest_paths: list[str] = Field(
        description="Paths to the scenario_manifest.json file for each completed scenario",
        examples=[
            [
                "s3://twod-fim/version=v1/results/1257410937935512/fceb20c6_N164S214E230W107/results/nd=1.0E02/q=1000/scenario.json"
            ]
        ],
    )
    scenario_comparison_results: list[AdaptiveStepComparisonResults | None] = Field(
        description="Adaptive step comparison results for each accepted scenario; None for the baseline and max-discharge scenarios",
        examples=[[None]],
    )
    warnings: list[JobWarning] = Field(examples=[[]])
