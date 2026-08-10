# Deployment Configuration

This package is configured through environment variables. This makes it straightforward to deploy in containers, cloud environments, or locally.

## Quick Setup

Copy `.env.template` to `.env` and fill in your AWS credentials:

```bash
cp .env.template .env
```

Then edit `.env`:

```bash
AWS_REQUEST_PAYER=requester
AWS_ACCESS_KEY_ID=your-access-key-id
AWS_SECRET_ACCESS_KEY=your-secret-access-key
```


For Docker deployments, pass the file directly:

```bash
docker run --env-file .env twod-fim-jobs:build_model '...'
```

---

## Required Variables

These must be set for the package to function.

| Variable                | Description                                          | Value          |
|-------------------------|------------------------------------------------------|----------------|
| `AWS_REQUEST_PAYER`     | Enables access to requester-pays S3 buckets.         | `requester`    |
| `AWS_ACCESS_KEY_ID`     | AWS access key ID used for authentication.           | Your key ID    |
| `AWS_SECRET_ACCESS_KEY` | AWS secret access key used for authentication.       | Your secret key |

AWS credentials are used for two purposes:
1. Downloading LULC (land use/land cover) data from a requester-pays USGS S3 bucket.
2. Reading/writing job inputs and outputs from S3 (if S3 URIs are used).

---

## Optional Variables

All optional variables have sensible defaults and only need to be set if you want to override them.

### Data Sources

Control where terrain (DEM) and land cover (LULC) data are fetched from. Override these to point at local copies, alternative datasets, or cached mirrors.

| Variable             | Default                                                                                                 | Description                                             |
|----------------------|---------------------------------------------------------------------------------------------------------|---------------------------------------------------------|
| `DEFAULT_DEM_SOURCE` | `https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/13/TIFF/USGS_Seamless_DEM_13.vrt`           | GDAL-readable URI for the digital elevation model.      |
| `DEFAULT_LULC_SOURCE`| `/vsis3/usgs-landcover/annual-nlcd/c1/v0/cu/mosaic/Annual_NLCD_LndCov_2023_CU_C1V0.tif`                | GDAL-readable URI for the land use/land cover raster.   |
| `DEFAULT_EPSG_CODE`  | `5070`                                                                                                  | EPSG code for the output coordinate reference system. The default (EPSG:5070) is Conus Albers, suitable for CONUS-wide work. |

### Database Schema

These control how the package reads reach geometry and attributes from a hydrofabric database. Override these if your database uses different table or column names than the defaults.

| Variable            | Default          | Description                                     |
|---------------------|------------------|-------------------------------------------------|
| `REACH_TABLE`       | `reach_network`  | Table name containing reach geometries.         |
| `REACH_ID_FIELD`    | `reach_id`       | Column name for the reach identifier.           |
| `REACH_TO_ID_FIELD` | `reach_to_id`    | Column name for the downstream reach identifier.|
| `DA_FIELD`          | `total_da_sqkm`  | Column name for drainage area (km²).            |
| `STREAM_ORDER_FIELD`| `stream_order`   | Column name for Strahler stream order.          |
| `SLOPE_FIELD`       | `slope`          | Column name for channel slope.                  |

### Output Printing

| Variable                    | Default | Description                                                                 |
|-----------------------------|---------|-----------------------------------------------------------------------------|
| `PRINT_SEPEX_STYLE_RESULTS` | `true`  | Print "plugin_results" block at the end of each job run. |

### Solver Settings

Fine-grained controls for the hydraulic solver's time-stepping behavior. These are advanced settings; the defaults are tuned for typical production use and should not need to be changed in most deployments.

| Variable                                          | Default | Description                                                                  |
|---------------------------------------------------|---------|------------------------------------------------------------------------------|
| `USE_CUDA`                                        | `true`  | Enable GPU acceleration. Set to `false` on CPU-only hosts.                   |
| `STABILITY_WAIT`                                  | `0.1`   | Time window (seconds) used for solver stability checks.                      |
| `ADAPTIVE_STEP_ALGORITHM_SHRINK_FACTOR`           | `0.5`   | Multiplier applied to the discharge step size when a trial scenario is rejected for producing too large a change. |
| `ADAPTIVE_STEP_ALGORITHM_GROW_FACTOR`             | `1.1`   | Multiplier applied to the discharge step size when a trial scenario is accepted or rejected for producing too small a change. |
| `ADAPTIVE_STEP_ALGORITHM_MAX_STAGE_MIN_ACCEPTABLE`| `0.75`  | Minimum 95th-percentile depth difference (m) between consecutive discharge scenarios required to accept the step. Steps below this threshold are rejected as too small. |
| `ADAPTIVE_STEP_ALGORITHM_MAX_STAGE_MAX_ACCEPTABLE`| `1.25`  | Maximum 95th-percentile depth difference (m) between consecutive discharge scenarios before the step is rejected as too large. |
| `ADAPTIVE_STEP_ALGORITHM_MEDIAN_STAGE_MIN_ACCEPTABLE` | `0.25` | Minimum median depth difference (m) between consecutive discharge scenarios required to accept the step. |
| `ADAPTIVE_STEP_ALGORITHM_MEDIAN_STAGE_MAX_ACCEPTABLE` | `0.75` | Maximum median depth difference (m) between consecutive discharge scenarios before the step is rejected as too large. |
| `ADAPTIVE_STEP_ALGORITHM_EXTENT_MIN_ACCEPTABLE`   | `0.075` | Minimum fractional change in inundated area between consecutive discharge scenarios required to accept the step. |
| `ADAPTIVE_STEP_ALGORITHM_EXTENT_MAX_ACCEPTABLE`   | `0.125` | Maximum fractional change in inundated area between consecutive discharge scenarios before the step is rejected as too large. |
