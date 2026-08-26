# run_kwse_scenarios

## Overview

Run a hydraulic solver for a set of user-defined scenarios.  A scenario is defined by an upstream discharge, a (previously-run) downstream scenario, and optionally, a depth grid to hot-start the scenario.  Water surface elevations are enforced on a cell-by-cell basis along the stage transfer line of the downstream scenario onto the current reach's domain.  Scenarios are run in serial, such that scenarios earlier in the user-defined list can serve as hot-starts for later scenarios.  Any scenario that already exists on storage is skipped instead of being re-run.

## Inputs

<!-- AUTO:inputs_table -->
### Required

| Name | Type | Description |
| --- | --- | --- |
| `model_manifest_path` | `string` | Path where the model manifest json is saved |
| `model_results_base_path` | `string` | Path where results will be saved |
| `scenarios` | `list[KWSEScenario]` | A list of KWSE scenarios to run.  If hot start files will be needed  |

### Optional

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `volume_convergence_tolerance` | `number` | 0.001 | Volume increase in the reach as a percent of inflow below which model is considered steady |
| `allow_water_on_edges` | `boolean` | false | Whether to ignore or terminate when water pools on an invalid edge |
| `max_simulation_length_seconds` | `number` | 86400 | Maximum time (in model seconds) that a model will be allowed to run before it is forcefully terminated |
| `save_interval_seconds` | `number` | 3600.0 | Frequency (in model seconds) with which a model will export depth rasters |
| `max_simulation_wall_time_seconds` | `number` | 10000000000.0 | Maximum time (in wall time) that a model will be allowed to run before it is forcefully terminated |
| `save_velocity` | `boolean` | false | Whether or not to generate and save velocity tifs |
| `save_zarr` | `boolean` | false | Whether or not to generate and save a zarr file with wse and depth at each print interval |
<!-- /AUTO:inputs_table -->

### `KWSEScenario`

<!-- AUTO:kwse_scenario_table -->
#### Required

| Name | Type | Description |
| --- | --- | --- |
| `upstream_discharge` | `number` | Flows applied at the top of the reach in cms |
| `bc_value` | `number` | Nominal water surface elevation at the bottom of the reach |
| `downstream_Scenario` | `string` | Path to the scenario manifest json for the model providing downstrem WSE forcing |

#### Optional

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `hotstart` | `HotStart` | null | Scenario used for initial water depths in the simulation. |
<!-- /AUTO:kwse_scenario_table -->

### `HotStart`

<!-- AUTO:hotstart_table -->
#### Required

| Name | Type | Description |
| --- | --- | --- |
| `upstream_discharge` | `number` | Flows applied at the top of the reach in cms |
| `bc_value` | `number` | Nominal water surface elevation at the bottom of the reach |

#### Optional

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `identity_hash` | `string` | "0c24be7a" | Hash of the run identity object. If none, assumed to be same as current scenario's. |
<!-- /AUTO:hotstart_table -->


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
| `manifests` | `list[string]` | Paths to all generated scenario assets. |
| `warnings` | `list[JobWarning]` |  |
<!-- /AUTO:result_table -->



## Processing Scope

 - Retrieve the model manifest for the reach to be modeled.
 - Iterate over all scenarios. For each scenario,
   - Check if a scenario with the same inputs has already been run, skip if it has
   - Create an inflow boundary condition using the provided discharge
   - Apply a steep normal depth outflow condition along domain edges intersecting the downstream model's inundated area (slope = 0.5 m/m)
   - Apply water surface elevations from a downstream scenario on a cell-by-cell basis along the downstream scenario's stage transfer line.
   - If a hot start is specified, locate the depth tif for that scenario and set as the initial model state.
   - Solve the scenario
   - Post-process the results to derive a depth grid, inundated area polygon, stage transfer line, and optional zarr for timestep-specific depth/wse.
   - Publish results to final storage location
 - Assemble list of generated scenario manifests and warnings, then return


## Out of Scope

- Building the Model (`build_model`'s job).
- Deciding which `(q, z)` scenario points to run (`plan_scenarios`, orchestrator-owned).
- ND runs (`run_nd_scenarios`) — a KWSE run may consume an ND run's `depth.tif` as a hotstart, but doesn't produce one.
- Composite/library post-processing.
- Determining hotstart and stage transfer.

## Dependencies

- Python
- GDAL
- AWS CLI
- LISFLOOD or SFINCS

## Errors

- No custom errors are currently defined for this job

## Checks

- If a scenario with the same inputs has already been run and exists on storage, that scenario is skipped.
- Solver exit code is 0


## Performance

Typical runtime: ~3 minutes × number of scenarios in the batch.
