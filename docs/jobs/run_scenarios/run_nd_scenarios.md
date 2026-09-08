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
| `adaptive_step_min_delta_q` | `integer` | 10 | Discharge step below which refining stops being worth another simulation. Not a minimum step: a finer step is run if that is where the acceptance window falls. But when the window asks for less than this and the trial still rejects high, it is accepted rather than narrowing again. |
| `save_velocity` | `boolean` | false | Whether or not to generate and save velocity tifs |
| `save_zarr` | `boolean` | false | Whether or not to generate and save a zarr file with wse and depth at each print interval |
| `ld_q_max_depth_increase_range` | `list[any]` | [0.75, 1.25] | [min, max] increase in max depth (m) between consecutive library entries. Under min the step was too small, over max it was too large. |
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

The job builds a library of hydraulically distinct discharges by walking the reach's own response instead of sampling at a fixed interval, so sampling is dense where the reach response changes quickly (overtopping, floodplain spillover), and sparse where consecutive maps would be near-identical.

First some vocabular and definitions:

### State

- **reference:** the last scenario published to the library. Comparison is measured from it
- **position:** the last discharge simulated. The next run hotstarts from its depth grid, and the next discharge is measured out from here.
- **scenario points:** every scenario simulated during the job, published or not, held in memory

### The response curves

Each simulated scenario gives us three readings: maximum depth, median depth and inundated area. The algorithm accumulates them as **three separate curves.** These curves are used to calculate next point to evaluate. Following rules for curves are maintained:

```
1. Straight line segments between simulated discharges aka data/scenario points (no fitting).

2. Values can not decrease i.e. if a point value is lower than the one before it, the earlier
   value is held

3. Above the highest data point on the chart, the last segment's slope is carried on (for extrapolation)
```

Rule 2 is to nevigate a physical error that is more water mean less flooding.

### How the Next Discharge is Chosen

Each criterion is read off its own curve for the discharge at which the change from the reference reaches its floor, and the discharge at which it reaches its ceiling. Acceptance needs only one criterion at its floor but every criterion under its ceiling, so the two ends of the window are minima over different criteria:

```
  window opens at   the EARLIEST floor crossing
  window closes at  the EARLIEST ceiling crossing

  q ──────────●──────────[════════════]──────────────►
           reference    opens      closes
                             ▲
                        take the middle
```

The window is never empty. The criterion that reaches a floor first must do so before any criterion reaches a ceiling, because that same criterion's own ceiling comes later still. The next discharge is the middle of the window, rounded to whole cms — the top edge would give larger steps and a smaller library, but it is where a straight-line reading of a concave curve is least trustworthy.

### When the window is out of reach

The window can land somewhere the sweep cannot run. Where it landed is itself a finding, so neither case is a silent correction:

| Case                                                         | What happens                                                                                         |
| ------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------- |
| Window is within `adaptive_step_min_delta_q` of the position | Run exactly what the window asked for — the step is **not** widened. But the band is finer than this is worth chasing, so a `reject_high` is accepted rather than narrowing again. |
| Window opens beyond `max_upstream_inflow`                    | Run `max_upstream_inflow`.                                                                           |

`adaptive_step_min_delta_q` is not a minimum step. A four cms step is run if that is where the window falls; the setting marks only where refinement stops earning another simulation.

If all three curves have gone flat, no criterion reaches a floor at any discharge, so the window opens at infinity — which is the second case, and it becomes "go and check the top." Flatness therefore needs no test of its own and raises no error.

### Step Sequence

The sweep cold-starts at `min_upstream_inflow` and publishes it as the first library entry. One point is not a curve, so the opening step is the authored `delta_upstream_inflow`; every step after that is read off the curves. Then it repeats:

1. Choose the next discharge from the curves (above).
2. Simulate it, warm-started from the position's depth grid, and keep it in memory without publishing.
3. Compare it against the **reference** on three criteria, each an increase between the two scenarios' published metrics:

| Criterion    | Quantity                                                             | Accepted when                                   |
| ------------ | -------------------------------------------------------------------- | ----------------------------------------------- |
| Max depth    | increase in maximum depth over wet cells, m                          | inside `ld_q_max_depth_increase_range`          |
| Median depth | increase in median depth over wet cells, m                           | inside `ld_q_median_depth_increase_range`       |
| Flooded area | percent increase in inundated area, against the reference's own area | inside `ld_q_flooded_area_prcnt_increase_range` |

4. Apply the verdict, where `reject_high` takes priority because any criterion over its ceiling means the step was too large:

   - **`reject_high`** — keep the point, which pulls the curves down and lowers the next window. Nothing is published.
   - **`accept`** — publish the scenario, advance the reference and the position to it, then re-judge the finished runs (below).
   - **`reject_low`** — advance the position without publishing.

### Reaching the maximum

`max_upstream_inflow` is a library entry whatever its comparison says: it is the top of the discharge envelope, and the KWSE stage grid is built from it. It is published on that basis alone. It is still judged, though, and the verdict is recorded honestly — a `reject_high` there means the response resumed somewhere below, so the sweep carries on and fills the gap underneath rather than leaving a hole no one can see.

Because the maximum is published the moment it runs and may later be re-judged and accepted by the free pass, publishing is idempotent: a discharge already uploaded is never uploaded twice.

### Reusing finished runs

Whenever the reference advances, every finished run above it is re-judged against the new reference before anything else is simulated. A comparison is arithmetic over two manifests, so this costs nothing, and a discharge that was too large a step from one reference sometimes sits inside the bands for the next one — in which case it becomes a library entry that has already been paid for and is published retroactively.

```
accept 300, then re-judge what is already in memory, highest first

  in memory   290  300  301  312  334  379  468  647
  eligible                   ---  ---  ---  ---  ---   above the reference

  647   too large   →  keep scanning down
  468   too large   →  keep scanning down
  379   too large   →  keep scanning down
  334   IN BAND     →  furthest free advance, and the scan stops here

  301 and 312 are never judged: they sit below a discharge already
  accepted, so nothing they could say would advance the reference further
```

Because response rises with discharge, the outcomes above the reference fall in the order too small, in band, too large. The scan runs downward from the highest simulated discharge for that reason: an accept is the furthest advance available the moment it is found, and everything below it is either a smaller advance or too small a step.

### Why this is safe

**The curves only ever propose. Every verdict is measured.** Nothing is published because a curve predicted it, so a wrong curve costs one simulation and buys a real point exactly where the curve was least accurate. It also bounds the damage from a feature the curve cannot see: a sharp floodplain spillover between two widely spaced samples reads as a gentle slope, but the trial on the far side of it rejects high, and the next window comes back and finds it.

### What is published

Only the scenarios the sweep accepts, plus the baseline and the final maximum discharge, are uploaded. A rejected trial is search overhead rather than a library entry, and publishing it would leave the library denser than it was asked to be and pay storage for every artifact. Rejected runs stay in the working directory for the life of the job, where they still serve as warm-start sources and as free candidates for later re-judging.

### Threshold Constants

The acceptance ranges are job inputs, defaulting to the values in `twod_fim_jobs/consts.py`.

### Limitations

- Runs are strictly sequential, since each simulation warm-starts from the previous one, so the sweep cannot be parallelised.
- Dense sampling through floodplain-spillover transitions is not guaranteed, because those are governed by the normal-depth downstream boundary condition, which may not capture all backwater effects.
- An edge-error termination aborts the whole sweep, and the reach is left with whatever it had published up to that point.
- The flooded-area criterion is scale-dependent, because its denominator is the reference's own area while the two depth criteria are absolute. Low in the range the wetted area is small and spreading quickly, so a step finer than `adaptive_step_min_delta_q` can be needed to stay inside the band; high in the range the area has largely saturated, so a step of several hundred cms can fall below the floor and acceptance passes to the depth criteria. One band therefore does not describe the same thing at both ends of a reach.

## Performance

- Minutes to hours.  Execution is serial and models can take a while to run.