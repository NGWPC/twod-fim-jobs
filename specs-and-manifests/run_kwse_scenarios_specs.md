# run_kwse_scenarios

Inputs match `run.schema.json#/properties/inputs` (see conventions.md). Writes one `run.json` per scenario in the batch — see conventions.md's "one manifest per object" rule. Shares its manifest type with `run_nd_scenarios` (see conventions.md's cardinality note).

## Overview

Run a hydraulic solver for a batch of (discharge, downstream-stage) scenarios against one Model, using the downstream reach's own Run results to build each scenario's boundary condition, and write one Run manifest plus depth/STL rasters per scenario.

## Inputs

One call solves a **batch** of scenarios for one reach against one downstream reach, in parallel — not one scenario per call. The Dagster partition boundary is the batch call; scenarios inside a batch are not individually retriable units.

### Required

| Name              | Type         | Description                                                                                          |
| ----------------- | ------------ | ---------------------------------------------------------------------------------------------------- |
| model_id          | str          | model_id of the reach that will be modeled                                                           |
| ds_model_id       | str          | model_id of the reach immediately downstream                                                         |
| models_base_path  | str          | Path where models are read from                                                                      |
| results_base_path | str          | Path where run artifacts are written                                                                 |
| scenarios         | list[object] | One entry per scenario: `{q, bc_value, downstream_scenario, hotstart (optional)}` — `hotstart` uses `run.schema.json`'s `scenarioRef` shape (reach_id/run_identity/scenario_point, not a resolved href — paths are self-documenting and rebuilt by the job, valid because it's a same-model reference). `downstream_scenario` crosses reach/model boundaries, so it's a resolved S3 URI string instead, not `scenarioRef` coordinates |

### Optional

| Name                             | Type  | Description                                                  |
| -------------------------------- | ----- | ------------------------------------------------------------ |
| max_simulation_length_hours      | float | Max sim-clock time before a scenario is forcibly terminated  |
| max_simulation_wall_time_minutes | float | Max wall-clock time before a scenario is forcibly terminated |
| save_velocity                    | bool  | Whether to generate and save a velocity raster asset         |

`solver` is **not** a call argument. Each solver (LISFLOOD, SFINCS, ...) is its own job/container image — installing two solvers on one image isn't done — so which solver ran is fixed by deployment, not requested per call. It's still recorded in each scenario's `run.json` under `identity.solver`, since it's output-determining.

## Processing Scope

- Retrieve the Model's artifacts (`dem.tif`, `roughness.tif`, `domain.geojson`, `cl.geojson`) from `model_id`.
- For each scenario, retrieve the downstream reach's Run referenced by `downstream_scenario` (from `ds_model_id`'s results).
- Extract the downstream stage and sample it onto the STL for a cell-by-cell boundary condition (DR-031).
- For each scenario, in parallel: build the solver input deck, apply `hotstart` if supplied, invoke the solver, watch for convergence.
- Compute per-scenario diagnostic metrics (see Metrics).
- Post-process each scenario's native solver output into twod-fim spec results (EPSG:5070 COG).
- Generate each scenario's own `stl.geojson` (DR-025, DR-026 — regenerated per run, not shared, so it can be handed to whichever reach is upstream of this one).
- Write each scenario's `run.json` last, recording `downstream_scenario` so the orchestrator can later detect staleness via `runs.kwse_transfer_run_identity` (triggers-and-propagation.md).

## Artifacts

| Artifact                                                                                           | Description                                                                     |
| -------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------- |
| `results/reach=<reach_id>/<model identity_hash>/<run identity_hash>/z=<value>/q=<value>/depth.tif` | Depth raster, COG, EPSG:5070                                                    |
| `.../velocity.tif`                                                                                 | Velocity raster — present only if `save_velocity`                               |
| `.../stl.geojson`                                                                                  | This scenario's Stage Transfer Line                                             |
| `.../metadata.csv` / `.parquet`                                                                    | Metadata on artifacts                                                           |
| `.../run.json`                                                                                     | Run definition and artifact inventory for this scenario — see `run.schema.json` |

## Response

- `runs` — list[object] — one `{id, identity_hash}` per scenario solved, same order as the `scenarios` input. It will have same order as input list.

A list, not a single pointer — one call produces N Run objects. `run.json` per scenario is the durable record; this is a pointer into each.

## Out of Scope

- Building the Model (`build_model`'s job).
- Deciding which `(q, z)` scenario points to run (`plan_scenarios`, orchestrator-owned).
- ND runs (`run_nd_scenarios`) — a KWSE run may consume an ND run's `depth.tif` as a hotstart, but doesn't produce one.
- Composite/library post-processing.
- Determining hotstart and stage transfer

## Dependencies

- Python
- GDAL
- AWS CLI
- LISFLOOD or SFINCS — exactly one per job image; see the solver note under Inputs.

## Errors

- Model data not available at `model_id` — raises `MissingModelError`
- Downstream model results not available at `ds_model_id` — raises `MissingDownstreamResultsError`
- Water on an invalid boundary cell — raises `WaterPoolingOnEdgeError` (specifies which edge(s) was wet)
- Solver does not converge within max runtime — raises `NonConvergenceError`
- Output artifacts cannot be written — raises `WriteFailureError`

## Checks

- results already exist for a scenario — skip that scenario, warning `run_exists` in its `run.json` (same idempotency pattern as `build_model`)
- `model_id != ds_model_id`
- `scenarios` not empty; every `q` finite, not NaN, and > 0
- downstream domain overlaps current domain
- median downstream stage is finite and non-negative
- solver exit code is 0

## Metrics

Recorded per scenario, in that scenario's `run.json` under `properties` (see `run.schema.json`):

- `achieved_bc_value` — the stage the solve actually produced, vs. the nominal target in `scenario.bc_value`
- `depth_stats` — min/max/mean/median/std of the depth raster
- `stl_stats` — sampled elevation min/max/mean/median/std, `n_sampled_cells`
- `volume_convergence`, `iterations`
- `sim_clock_time_s`, `wall_clock_time_s`

## Performance

Typical runtime: ~3 minutes × number of scenarios in the batch, solved in parallel — run on AWS Batch (contrast with `build_model_job_contract.md`'s local-run recommendation). Also waits on the downstream reach's Run to finish before `downstream_scenario` is available — see guide.md's Open Questions ("How does worker wait for last scenario of downstream reach to finish?").