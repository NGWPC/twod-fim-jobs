# run_nd_scenarios job

## Overview

Iteratively runs the model for a reach using a normal depth downstream boundary condition to generate a range of discharges at regular intervals.

## Inputs

<!-- AUTO:inputs_table -->
### Required

| Name | Type | Description |
| --- | --- | --- |
| `model_manifest_path` | `string` | Path where the model manifest json is saved |
| `model_results_base_path` | `string` | Path where results will be saved |
| `min_upstream_inflow` | `number` | Minimum of the target discharge range in cms |
| `max_upstream_inflow` | `number` | Maximum of the target discharge range in cms |
| `delta_upstream_inflow` | `number` | Discharge increment for adaptive step algorithm in cms |
| `ds_slope` | `number` | Slope value to apply for the downstream boundary condition in m/m |
| `outflow_area_polygon_path` | `string` | Path to a polygon that determines where normal depth boundary condition will be applied. |

### Optional

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `solver` | `string` | "lisflood" | Hydraulic solver used (e.g., lisflood or sfincs) |
| `volume_convergence_tolerance` | `number` | 0 | Volume increase in the reach as a percent of inflow below which model is considered steady |
| `allow_water_on_edges` | `boolean` | false | Whether to ignore or terminate when water pools on an invalid edge |
| `max_simulation_length_seconds` | `number` | 86400 | Maximum time (in model seconds) that a model will be allowed to run before it is forcefully terminated |
| `save_interval_seconds` | `number` | 3600.0 | Frequency (in model seconds) with which a model will export depth rasters |
| `max_simulation_wall_time_minutes` | `number` | null | Maximum time (in wall time) that a model will be allowed to run before it is forcefully terminated |
| `save_velocity` | `boolean` | false | Whether or not to generate and save velocity tifs |
| `save_zarr` | `boolean` | false | Whether or not to generate and save a zarr file with wse and depth at each print interval |
<!-- /AUTO:inputs_table -->

## Artifacts

<!-- AUTO:artifacts_table -->
| Name | Description |
| --- | --- |
| `depth` | Path of the depth grid at the final timestep |
| `inundation_polygon` | Path of the inundated area polygon at the final timestep |
| `stage_transfer_line` | Path of the stage transfer line |
| `zarr_store` | Path of the zarr with depths at each print interval |
<!-- /AUTO:artifacts_table -->

## Response

<!-- AUTO:result_table -->
| Name | Type | Description |
| --- | --- | --- |
| `scenario_manifest_paths` | `list[string]` | Paths to the scenario_manifest.json file for each completed scenario |
| `scenario_comparison_results` | `list[AdaptiveStepComparisonResults]` | Adaptive step comparison results for each accepted scenario; None for the baseline and max-discharge scenarios |
| `warnings` | `list[JobWarning]` |  |
<!-- /AUTO:result_table -->

## Processing Scope

- Load and localize model assets from the model manifest.
- Convert terrain and roughness rasters to solver input format.
- Load inflow line, outflow area, and upstream/downstream centerline points.
- Run an adaptive step algorithm across the configured discharge range.
- Compare successive scenarios to accept or reject each discharge step.
- Publish completed scenario artifacts and manifests to storage.

## Out of Scope

- Model building or domain generation.
- Known water surface elevation (KWSE) scenario generation.
- Post-processing or aggregation of results across multiple reaches.
- Anything with STLs.

## Dependencies

- Python
- GDAL
- Hydraulic solver (lisflood or sfincs)
- AWS CLI

## Errors

- Source raster datasets are unavailable - raises DatasetUnavailableError
- Raster processing fails - raises RasterProcessingError
- Output artifacts cannot be written - raises WriteFailureError
- Water reaches an invalid domain edge - terminates the adaptive step algorithm and returns an empty result

## Checks

- If a scenario terminates due to an edge error, the adaptive step algorithm is aborted and an empty scenario list is returned.

## Adaptive Step Algorithm

The job establishes a library of hydraulically distinct discharge scenarios by walking the reach's actual response curve rather than sampling at a fixed interval. This produces dense sampling at hydraulic transitions (e.g., overtopping, floodplain spillover) and sparse sampling where successive maps would be near-identical.

### State Variables

Two state variables evolve independently throughout the algorithm:

- **`q_current`** — the discharge used to warm-start the next trial run. Advances on `accept` and `reject_low`; holds on `reject_high`.
- **`q_accepted`** (the reference snapshot) — the comparison baseline for every trial. Advances only on `accept`.

This separation means the acceptance criteria measure *cumulative* change since the last accepted library entry, not the incremental change between consecutive runs.

### Step Sequence

1. Cold-start the model at `min_upstream_inflow` → save as the first accepted snapshot.
2. Propose `q_trial = q_current + Δq`. Run the model warm-started from `q_current`'s depth raster.
3. Compare the trial's response to the last accepted snapshot on three criteria:

| Criterion | Quantity | Acceptance band |
| --- | --- | --- |
| Max stage | 95th-percentile depth difference over wet cells | `MAX_STAGE_MIN_ACCEPTABLE` ≤ Δ ≤ `MAX_STAGE_MAX_ACCEPTABLE` |
| Median stage | Median depth difference over wet cells | `MEDIAN_STAGE_MIN_ACCEPTABLE` ≤ Δ ≤ `MEDIAN_STAGE_MAX_ACCEPTABLE` |
| Extent | Fractional change in flooded cell count relative to accepted snapshot | `EXTENT_MIN_ACCEPTABLE` ≤ Δ ≤ `EXTENT_MAX_ACCEPTABLE` |

4. Apply the combined verdict — `reject_high` takes priority:
   - **`reject_high`** (any criterion above its ceiling): hold `q_current`, shrink `Δq` by `SHRINK_FACTOR`, retry.
   - **`accept`** (any criterion in band, none above ceiling): save snapshot, advance both `q_current` and `q_accepted` to `q_trial`, grow `Δq` by `GROW_FACTOR`.
   - **`reject_low`** (all criteria below their floor): advance `q_current` to `q_trial` without saving a snapshot, grow `Δq` by `GROW_FACTOR`.
5. Repeat until `q_trial` reaches `max_upstream_inflow`, then run that discharge unconditionally as the final scenario.

The baseline (`min_upstream_inflow`) and final (`max_upstream_inflow`) scenarios are always included in the output regardless of the acceptance verdict.

### Threshold Constants

All acceptance thresholds and grow/shrink factors are defined in `twod_fim_jobs/consts.py` and prefixed `ADAPTIVE_STEP_ALGORITHM_`.

### Limitations

- Runs are strictly sequential — each simulation must finish before the next is proposed, so parallelism is not possible.
- The method does not guarantee dense sampling through all floodplain-spillover transitions; those are governed by the normal-depth downstream boundary condition, which may not capture all backwater effects.
- Strong safeguards against runaway iterations are required; an edge-error termination aborts the full algorithm.

## Performance

- Minutes to hours.  Execution is serial and models can take a while to run.
