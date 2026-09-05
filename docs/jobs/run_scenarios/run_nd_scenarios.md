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
| `min_upstream_inflow` | `integer` | Minimum of the target discharge range in whole cms. Must be greater than 0 |
| `max_upstream_inflow` | `integer` | Maximum of the target discharge range in whole cms |
| `delta_upstream_inflow` | `integer` | Discharge increment for adaptive step algorithm in whole cms. Must be greater than 0 |

### Optional

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `outflow_area_polygon_path` | `string` | null | Path to a polygon that determines where normal depth boundary condition will be applied. |
| `volume_convergence_tolerance` | `number` | 0.001 | Volume increase in the reach as a percent of inflow below which model is considered steady |
| `allow_water_on_edges` | `boolean` | false | Whether to ignore or terminate when water pools on an invalid edge |
| `max_simulation_length_seconds` | `number` | 86400 | Maximum time (in model seconds) that a model will be allowed to run before it is forcefully terminated |
| `save_interval_seconds` | `number` | 3600.0 | Frequency (in model seconds) with which a model will export depth rasters |
| `max_simulation_wall_time_seconds` | `number` | 10000000000.0 | Maximum time (in wall time) that a model will be allowed to run before it is forcefully terminated |
| `adaptive_step_min_delta_q` | `integer` | 10 | Minimum sensitivity for Q in adaptive step algorithm.  If delta_q at the min and algorithm would reject high, trial is accepted instead. |
| `save_velocity` | `boolean` | false | Whether or not to generate and save velocity tifs |
| `save_zarr` | `boolean` | false | Whether or not to generate and save a zarr file with wse and depth at each print interval |
| `adaptive_step_algorithm_shrink_factor` | `number` | 0.5 | Multiplier applied to the discharge step size when a trial scenario is rejected for producing too large a change |
| `adaptive_step_algorithm_grow_factor` | `number` | 1.5 | Multiplier applied to the discharge step size when a trial scenario is accepted or rejected for producing too small a change |
| `ld_q_max_depth_increase_range` | `list[any]` | [0.75, 1.25] | [min, max] increase in max depth (m) between consecutive discharge scenarios. Below min the step is too small and grows; above max it is too large and shrinks. |
| `ld_q_median_depth_increase_range` | `list[any]` | [0.25, 0.5] | [min, max] increase in median depth (m) between consecutive discharge scenarios. |
| `ld_q_flooded_area_prcnt_increase_range` | `list[any]` | [10.0, 15.0] | [min, max] percent increase in flooded area between consecutive discharge scenarios, where 10 means 10 percent. |
<!-- /AUTO:inputs_table -->

## Artifacts

<!-- AUTO:artifacts_table -->
| Name | Description |
| --- | --- |
| `depth` | Depth grid at the final timestep |
| `inundation_polygon` | Inundated area polygon at the final timestep |
| `stage_transfer_line` | Stage transfer line |
| `zarr_store` | Zarr store with depths at each print interval |
<!-- /AUTO:artifacts_table -->

## Response

<!-- AUTO:result_table -->
| Name | Type | Description |
| --- | --- | --- |
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

The job builds a library of hydraulically distinct discharges by walking the reach's own response curve instead of sampling at a fixed interval, so that sampling is dense where the reach changes quickly — overtopping, floodplain spillover — and sparse where consecutive maps would be near-identical. Because every trial is a full simulation, the algorithm is written to spend simulations only on discharges whose outcome is not already implied by something it has run.

### State

Four pieces of state evolve through the sweep:

- **reference** — the accepted scenario the acceptance bands are measured from. It advances only on `accept`, so the criteria always describe cumulative change since the last library entry rather than the change between consecutive runs.
- **position** — the discharge the next step is measured from, and the depth grid the next run warm-starts from. It advances on `accept` and on `reject_low`, because a run that was too small a step is still the closest starting point available.
- **ceiling** — the lowest discharge proven too high for the current reference. Response rises with discharge, so a rejection at one discharge proves every larger one is also too high, and the ceiling is reset whenever the reference advances.
- **finished runs** — every scenario simulated during the job, published or not, held in memory.

  q ────────●──────────────────────●──────────────────────○───────────►
           136                    150                    200
        reference              position                ceiling
                                   ├──────────────────────┤
                                    the next run goes here

  136   accepted, published, and what every comparison is measured from
  150   too small a step from 136, so it moved the position and now
        warm-starts the next run — but it was not published
  200   too large a step from 136, so nothing at or above it is worth
        simulating until the reference advances past it

  The next discharge is at least min_delta_q above 150 and strictly
  below 200. When no discharge satisfies both, the bracket has closed.


### Step Sequence

The sweep cold-starts at `min_upstream_inflow`, publishes it as the first library entry, and then repeats the following until it reaches `max_upstream_inflow`, which is always run and always kept.

1. Choose the next discharge from the position, the current step and the ceiling (below).
2. Simulate it, warm-started from the position's depth grid, and keep it in memory without publishing.
3. Compare it against the **reference** on three criteria, each an increase between the two scenarios' published metrics:

| Criterion | Quantity | Accepted when |
| --- | --- | --- |
| Max depth | increase in maximum depth over wet cells, m | inside `ld_q_max_depth_increase_range` |
| Median depth | increase in median depth over wet cells, m | inside `ld_q_median_depth_increase_range` |
| Flooded area | percent increase in inundated area | inside `ld_q_flooded_area_prcnt_increase_range` |

4. Apply the verdict, where `reject_high` takes priority because any criterion over its ceiling means the step was too large:
   - **`reject_high`** — record the discharge as the ceiling and shrink the step.
   - **`accept`** — publish the scenario, advance the reference and the position to it, clear the ceiling, then re-judge the finished runs (below).
   - **`reject_low`** — advance the position without publishing, and grow the step.

### Choosing the next discharge

A proposal is the position plus the current step, except that it may never reach the ceiling. Proposing a discharge at or above a known-too-high one would spend a simulation learning what monotonicity already guarantees, so such a proposal is bisected into the bracket instead, and never placed closer than `adaptive_step_min_delta_q` to the position.

```
proposal = position + Δq

  below the ceiling        take it
  at or above the ceiling  bisect (position, ceiling), floored at min_delta_q
  nothing fits             the bracket has closed
```

When the bracket closes there is no untried discharge between the position and something already proven too high, which means no step at least `min_delta_q` wide lands inside the bands. The sweep then accepts the ceiling run itself, since it is the smallest step known to clear the band and is already simulated. That library therefore contains one step wider than the bands allow, which is not a defect in the sweep but a sign that the bands cannot be met at this reach without a smaller minimum step.

### Sizing the step

Rather than halving or growing by a constant, each criterion is asked what factor would place it on the middle of its band, and the smallest answer is used. One rule serves both directions: a step that was too large has some criterion over its ceiling and therefore a factor below one, while a step that was too small has every criterion under its floor, where the smallest factor is the least growth that reaches any band — which is what acceptance needs, since only one criterion must clear its floor. Taking the minimum is also what keeps the correction safe, because scaling by it moves the binding criterion to its midpoint and leaves every other at or below its own.

```
                         max depth   median  flooded area     step
  max depth too big           0.67     1.00          1.00   x 0.67
  median too big              1.00     0.62          1.00   x 0.62
  flooded area too big        1.00     1.00          0.62   x 0.62
  all three too small         2.00     1.88          1.56   x 1.50   clamped

  Every criterion is asked every time, and a different one binds in each
  row. The last row is the one worth reading twice: all three want to
  grow, and the SMALLEST growth wins, because acceptance needs only one
  criterion above its floor. Growing by 2.00, which max depth asked for,
  would carry the other two straight past their ceilings.
```

A criterion whose measured increase is zero or negative is skipped rather than divided by, and if that is true of all three there is no signal to size from, so the step simply grows by the grow factor.

The bounds matter because the response curve is concave, so a linear estimate under-corrects, and a single comparison is thin evidence for a large jump.

### Reusing finished runs

Whenever the reference advances, every finished run above it is re-judged against the new reference before anything else is simulated. A comparison is arithmetic over two manifests, so this costs nothing, and a discharge that was too large a step from one reference frequently sits inside the bands for the next one — in which case it becomes a library entry that has already been paid for and is published retroactively.

```
accept 300, then re-judge what is already in memory, highest first

  in memory   290  300  301  312  334  379  468  647
  eligible                   ---  ---  ---  ---  ---   above the reference

  647   too large   →  ceiling 647
  468   too large   →  ceiling 468
  379   too large   →  ceiling 379
  334   IN BAND     →  furthest free advance, and the scan stops here

  301 and 312 are never judged: they sit below a discharge already
  accepted, so nothing they could say would advance the reference further
```

Because response rises with discharge, the outcomes above the reference always fall in the order too small, in band, too large. The scan runs downward from the highest simulated discharge for that reason: an accept is the furthest advance available the moment it is found, and everything below it is either a smaller advance or too small a step, so neither can change the answer. Each too-large verdict on the way down lowers the ceiling, and the first verdict that is not too large ends the pass — as an acceptance, or, if nothing clears the floor, as the new position.

### What is published

Only the scenarios the sweep accepts, plus the baseline and the final maximum discharge, are uploaded. A rejected trial is search overhead rather than a library entry, and publishing it would leave the library denser than it was asked to be and pay storage for every artifact. Rejected runs stay in the working directory for the life of the job, where they still serve as warm-start sources and as free candidates for later re-judging.

### Threshold Constants

The acceptance ranges and the shrink and grow factors are job inputs, defaulting to the values in `twod_fim_jobs/consts.py`.

### Limitations

- Runs are strictly sequential, since each simulation warm-starts from the previous one, so the sweep cannot be parallelised.
- Dense sampling through floodplain-spillover transitions is not guaranteed, because those are governed by the normal-depth downstream boundary condition, which may not capture all backwater effects.
- An edge-error termination aborts the whole sweep, and the reach is left with whatever it had published up to that point.

## Performance

- Minutes to hours.  Execution is serial and models can take a while to run.
