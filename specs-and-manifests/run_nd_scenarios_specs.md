# run_nd_scenarios

Inputs match `run.schema.json#/properties/inputs` (see conventions.md). Writes one `run.json` per scenario in the batch — see conventions.md's "one manifest per object" rule. Shares its manifest type with `run_kwse_scenarios` (see conventions.md's cardinality note).

## Overview

Run a hydraulic solver for a batch of discharges against one Model, using a normal-depth (ND) boundary condition derived from the reach itself, and write one Run manifest plus depth/STL rasters per scenario.

## Inputs

One call solves a **batch** of discharges for one reach, in parallel — not one scenario per call. The Dagster partition boundary is the batch call; scenarios inside a batch are not individually retriable units.

### Required

| Name | Type | Description |
| --- | --- | --- |
| model_id | str | model_id of the reach that will be modeled |
| models_base_path | str | Path where models are read from |
| results_base_path | str | Path where run artifacts are written |
| scenarios | list[object] | One entry per scenario: `{q, hotstart (optional)}` — `hotstart` uses `run.schema.json`'s `scenarioRef` shape (run_identity/scenario_point, not a resolved href — paths are self-documenting and rebuilt by the job) |

### Optional

| Name | Type | Description |
| --- | --- | --- |
| max_simulation_length_hours | float | Max sim-clock time before a scenario is forcibly terminated |
| max_simulation_wall_time_minutes | float | Max wall-clock time before a scenario is forcibly terminated |
| save_velocity | bool | Whether to generate and save a velocity raster asset |

`solver` is **not** a call argument, same as `run_kwse_scenarios` — fixed by which job/container image ran, recorded in each scenario's `run.json` under `identity.solver`.

`bc_value` (the ND slope) is likewise **not** a call argument — it's derived from the Model's own `model.json` (`properties.slope`), not caller-supplied. It still ends up in each scenario's `run.json` under `scenario.bc_value`.

## Processing Scope

- Retrieve the Model's artifacts (`dem.tif`, `roughness.tif`, `domain.geojson`, `cl.geojson`) from `model_id`.
- Derive the normal-depth boundary condition from the Model's `properties.slope`.
- For each scenario, in parallel: build the solver input deck for `q` at the derived slope, apply `hotstart` if supplied, invoke the solver, watch for convergence.
- Compute per-scenario diagnostic metrics (see Metrics).
- Post-process each scenario's native solver output into twod-fim spec results (EPSG:5070 COG).
- Generate each scenario's own `stl.geojson` (DR-025, DR-026 — regenerated per run, not shared, so it can be handed to whichever reach is upstream of this one).
- Write each scenario's `run.json` last.

## Artifacts

| Artifact | Description |
| --- | --- |
| `results/reach=<reach_id>/<model identity_hash>/<run identity_hash>/nd=<value>/q=<value>/depth.tif` | Depth raster, COG, EPSG:5070 |
| `.../velocity.tif` | Velocity raster — present only if `save_velocity` |
| `.../stl.geojson` | This scenario's Stage Transfer Line |
| `.../metadata.csv` / `.parquet` | Metadata on artifacts |
| `.../run.json` | Run definition and artifact inventory for this scenario — see `run.schema.json` |

## Response

- `runs` — list[object] — one `{id, identity_hash}` per scenario solved, same order as the `scenarios` input.

A list, not a single pointer — one call produces N Run objects (see conventions.md). `run.json` per scenario is the durable record; this is a pointer into each.

## Out of Scope

- Building the Model (`build_model`'s job).
- Deciding which discharges to run (`q_set` is desired_state / orchestrator concern).
- KWSE scenario planning (`plan_scenarios`, orchestrator-owned) or KWSE runs (`run_kwse_scenarios`).
- Composite/library post-processing.

## Dependencies

- Python
- GDAL
- AWS CLI
- LISFLOOD or SFINCS — exactly one per job image; see the solver note under Inputs.

## Errors

- Model data not available at `model_id` — raises `MissingModelError`
- Model's `properties.slope` missing or invalid — raises `InvalidAttributeError`
- Water on an invalid boundary cell — raises `WaterPoolingOnEdgeError` (specifies which edge(s) was wet)
- Solver does not converge within max runtime — raises `NonConvergenceError`
- Output artifacts cannot be written — raises `WriteFailureError`

## Checks

- results already exist for a scenario — skip that scenario, warning `run_exists` in its `run.json` (same idempotency pattern as `build_model`)
- `scenarios` not empty; every `q` finite, not NaN, and > 0
- solver exit code is 0

## Metrics

Recorded per scenario, in that scenario's `run.json` under `properties` (see `run.schema.json`):

- `depth_stats` — min/max/mean/median/std of the depth raster
- `stl_stats` — sampled elevation min/max/mean/median/std, `n_sampled_cells`
- `volume_convergence`, `iterations`
- `sim_clock_time_s`, `wall_clock_time_s`

`achieved_bc_value` doesn't apply here — ND's boundary is derived, not targeted, so there's no nominal value to compare against.

## Performance

Typical runtime: ~3 minutes × number of scenarios in the batch, solved in parallel — run on AWS Batch (contrast with `build_model_job_contract.md`'s local-run recommendation).
