# build_model job

## Overview

Initialize a model for a single reach by generating the terrain, roughness, geometry, and boundary-condition artifacts required by downstream workflow steps.

## Inputs

<!-- AUTO:inputs_table -->
### Required

| Name | Type | Description |
| --- | --- | --- |
| `reach_id` | `integer` | Primary key for the reach in the reach db |
| `db_uri` | `string` | Connection string for the refactored hydrofabric |
| `base_output_path` | `string` | Path where output artifacts will be written |

### Optional

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `dem_source` | `string` | "https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/13/TIFF/USGS_Seamless_DEM_13.vrt" | Connection string for the DEM dataset |
| `lulc_source` | `string` | "/vsis3/usgs-landcover/annual-nlcd/c1/v0/cu/mosaic/Annual_NLCD_LndCov_2023_CU_C1V0.tif" | Connection string for the LULC source dataset |
| `other_geometries` | `list[string]` |  | A list of geometries that will be included when making the model domain bounding box |
| `domain_buffer` | `number` | 0.0 | How far to buffer the bounding box on model geometries |
| `grid_resolution` | `number` | 10 | Resolution that grid will snap to and that DEM and roughness will resample to |
| `walk_us_dist_pct` | `number` | 0.1 | How far to walk up the upstream mainstem centerline to place the inflow boundary condition, as percent of upstream centerline length |
| `epsg_code` | `integer` | 5070 | EPSG integer for all georeferenced output artifacts |
| `bankfull_width_multiplier` | `number` | 1.0 | How much to multiply bankfull width to arrive at inflow line width |
| `lulc_lookup` | `dict[str, number]` | {"11": 0.04, "21": 0.04, "22": 0.1, "23": 0.08, "24": 0.15, "31": 0.025, "41": 0.16, "42": 0.16, "43": 0.16, "52": 0.1, "71": 0.035, "81": 0.03, "82": 0.035, "90": 0.12, "95": 0.07} | A dictionary mapping land use codes to Manning's roughness values |
<!-- /AUTO:inputs_table -->

## Processing Scope

- Retrieve reach and upstream reach geometries from the hydrofabric.
- Estimate bankfull width.
- Generate inflow geometry.
- Define the model domain.
- Acquire and clip DEM and land cover data.
- Convert land cover data to roughness values.
- Generate model metadata.
- Write model artifacts to storage.

## Artifacts

<!-- AUTO:artifacts_table -->
| Name | Description |
| --- | --- |
| `terrain` | Terrain raster used by the hydraulic model. |
| `roughness` | Manning's n raster used by the hydraulic model. |
| `centerline` | River centerline for this model's reach. |
| `inflow_line` | Inflow boundary condition line for this model's reach. |
| `reach_centroid` | Centroid of the river centerline for this model's reach. |
| `domain` | Derived polygon of the full model domain. |
<!-- /AUTO:artifacts_table -->

## Response

<!-- AUTO:result_table -->
| Name | Type | Description |
| --- | --- | --- |
| `identity_hash` | `string` |  |
| `model_id` | `string` |  |
| `model_dir` | `string` |  |
| `warnings` | `list[JobWarning]` |  |
<!-- /AUTO:result_table -->

## Out of Scope

- Model expansion.
- Scenario generation.
- Solver input generation.
- Hydraulic simulation.
- Post-processing.
- Anything with STLs

## Dependencies

- Python
- GDAL
- AWS CLI

## Errors

- Source raster datasets are unavailable - raises DatasetUnavailableError
- Raster processing fails - raises RasterProcessingError
- Output artifacts cannot be written - raises WriteFailureError
- Drainage area missing or invalid in reach db - raises InvalidAttributeError

## Checks

- check if model exists at output path - return immediately with warning that model already exists
- checks if inflow line only crosses reach at one point - if multiple crosses, complete job as normal and return warning
- check if model domain is large - if large, return warning
- check if all roughness values are similar - if very similar, return warning

## Performance

- Typical runtime: ~10 seconds per reach.

Given the short execution time, AWS batch overhead would drastically increase cost.  Run this locally instead.