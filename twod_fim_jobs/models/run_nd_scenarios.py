from pydantic import BaseModel, ConfigDict, Field, field_validator
from typing import Literal
from twod_fim_jobs.consts import (
    ADAPTIVE_STEP_ALGORITHM_GROW_FACTOR,
    ADAPTIVE_STEP_ALGORITHM_MIN_DELTA_Q,
    ADAPTIVE_STEP_ALGORITHM_SHRINK_FACTOR,
    LD_Q_FLOODED_AREA_PRCNT_INCREASE_RANGE,
    LD_Q_MAX_DEPTH_INCREASE_RANGE,
    LD_Q_MEDIAN_DEPTH_INCREASE_RANGE,
    DEFAULT_MAX_WALL_TIME_SECONDS,
    DEFAULT_SIM_TIME_SECONDS,
    DEFAULT_SIM_SAVE_INTERVAL_SECONDS,
    DEFAULT_VOLUME_CONVERGENCE_THRESHOLD,
)

from twod_fim_jobs.models.warnings import JobWarning


# Narrower than this and the adaptive step has no room to accept: every trial is
# either too small or too large, and the sweep oscillates instead of converging.
_MIN_RANGE_SPAN = {
    "ld_q_max_depth_increase_range": 0.1,
    "ld_q_median_depth_increase_range": 0.1,
    "ld_q_flooded_area_prcnt_increase_range": 1.0,
}


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
        examples=["s3://twod-fim/version=v1/results"],
    )
    min_upstream_inflow: int = Field(
        gt=0,
        description="Minimum of the target discharge range in whole cms. Must be greater than 0",
        examples=[100],
    )
    max_upstream_inflow: int = Field(
        description="Maximum of the target discharge range in whole cms",
        examples=[5000],
    )
    delta_upstream_inflow: int = Field(
        gt=0,
        description="Discharge increment for adaptive step algorithm in whole cms. Must be greater than 0",
        examples=[100],
    )

    # Optional
    outflow_area_polygon_path: str | None = Field(
        default=None,
        description="Path to a polygon that determines where normal depth boundary condition will be applied.",
        examples=["s3://twod-fim/version=v1/shared/outflow_area.geojson"],
    )
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
    adaptive_step_min_delta_q: int = Field(
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
    adaptive_step_algorithm_shrink_factor: float = Field(
        default=ADAPTIVE_STEP_ALGORITHM_SHRINK_FACTOR,
        description="Multiplier applied to the discharge step size when a trial scenario is rejected for producing too large a change",
        examples=[0.5],
    )
    adaptive_step_algorithm_grow_factor: float = Field(
        default=ADAPTIVE_STEP_ALGORITHM_GROW_FACTOR,
        description="Multiplier applied to the discharge step size when a trial scenario is accepted or rejected for producing too small a change",
        examples=[1.5],
    )
    ld_q_max_depth_increase_range: tuple[float, float] = Field(
        default=LD_Q_MAX_DEPTH_INCREASE_RANGE,
        description="[min, max] increase in max depth (m) between consecutive discharge scenarios. Below min the step is too small and grows; above max it is too large and shrinks.",
        examples=[(0.75, 1.25)],
    )
    ld_q_median_depth_increase_range: tuple[float, float] = Field(
        default=LD_Q_MEDIAN_DEPTH_INCREASE_RANGE,
        description="[min, max] increase in median depth (m) between consecutive discharge scenarios.",
        examples=[(0.25, 0.5)],
    )
    ld_q_flooded_area_prcnt_increase_range: tuple[float, float] = Field(
        default=LD_Q_FLOODED_AREA_PRCNT_INCREASE_RANGE,
        description="[min, max] percent increase in flooded area between consecutive discharge scenarios, where 10 means 10 percent.",
        examples=[(10.0, 15.0)],
    )

    @field_validator(*_MIN_RANGE_SPAN)
    @classmethod
    def range_is_wide_enough(cls, v: tuple[float, float], info) -> tuple[float, float]:
        low, high = v
        span = _MIN_RANGE_SPAN[info.field_name]
        if high - low < span:
            raise ValueError(
                f"{info.field_name} must span at least {span}, got {low} to {high}"
            )
        return v

    @field_validator("save_velocity")
    @classmethod
    def save_velocity_not_implemented(cls, v: bool) -> bool:
        if v is True:
            raise NotImplementedError("save_velocity is not yet implemented")
        return v


class AdaptiveStepComparisonResults(BaseModel):
    ref_scenario_manifest: str | None = Field(
        examples=[
            "s3://twod-fim/version=v1/results/1257410937935512/fceb20c6_N164S214E230W107/results/nd=1.0E02/q=1000/scenario.json"
        ]
    )
    trial_scenario_manifest: str = Field(
        examples=[
            "s3://twod-fim/version=v1/results/1257410937935512/fceb20c6_N164S214E230W107/results/nd=1.0E02/q=1200/scenario.json"
        ]
    )
    max_depth_increase: float = Field(examples=[1.15])
    median_depth_increase: float = Field(examples=[1.03])
    flooded_area_prcnt_increase: float = Field(examples=[12.0])
    result: Literal["reject_high", "reject_low", "accept"] = Field(examples=["accept"])


class RunNDScenariosResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_comparison_results: list[AdaptiveStepComparisonResults | None] = Field(
        description="Adaptive step comparison results for each accepted scenario; None for the baseline and max-discharge scenarios",
        examples=[[None]],
    )
    warnings: list[JobWarning] = Field(examples=[[]])
