# build_model job

## Overview

Initialize a model for a single reach by generating the terrain, roughness, geometry, and boundary-condition artifacts required by downstream workflow steps.

## Inputs

### Required

| Name             | Type | Description                                                                     |
| ---------------- | ---- | ------------------------------------------------------------------------------- |
| reach_id         | int  | Reach identifier                                                                |
| db_uri           | str  | Connection information for external database to query reach geom and parameters |
| base_output_path | str  | Model output location                                                           |

### Optional

| Name                      | Type      | Description                                                                        |
| ------------------------- | --------- | ---------------------------------------------------------------------------------- |
| dem_source                | str       | DEM data source (may need to add more info later)                                  |
| lulc_source               | str       | Land cover data source (may need to add more info later)                           |
| other_geometries          | list[str] | text representations of geometries that must be included in the reach bounding box |
| domain_buffer             | float     | Domain buffer distance applied to bounding box generated from all geometries       |
| grid_resolution           | int       | resolution to resample DEM and roughness to                                        |
| walk_us_dist_pct          | float     | Upstream search distance parameter                                                 |
| epsg_code                 | int       | Output coordinate reference system                                                 |
| bankfull_width_multiplier | float     | Multiplier applied to estimated bankfull width                                     |
| lulc_lookup               | dict      | Mapping from land cover classes to roughness values                                |

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

| Artifact                                     | Description                             |
| -------------------------------------------- | --------------------------------------- |
| base_output_path/model_hash/dem.tif          | Terrain raster                          |
| base_output_path/model_hash/roughness.tif    | Roughness raster                        |
| base_output_path/model_hash/cl.geojson       | Stream centerline geometry              |
| base_output_path/model_hash/inflow.geojson   | Upstream boundary geometry              |
| base_output_path/model_hash/centroid.geojson | Reach centerline centroid               |
| base_output_path/model_hash/domain.geojson   | Reach bounding box                      |
| base_output_path/model_hash/model.json       | Model definition and artifact inventory |

## Response

- model_identity - str - see guide.md
- model_hash - str - model identity + domain offset

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