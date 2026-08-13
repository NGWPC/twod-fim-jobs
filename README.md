# Two-Dimensional Flood Inundation Modeling (2D-FIM) Pipeline Jobs

Jobs to support automated generation of reach-scale 2D hydraulic flood inundation model libraries at continental scale.

This library provides the three core jobs within a continental-scale flood inundation modeling pipeline. An external orchestrator is responsible for sequencing them, tracking what has been computed, and deciding what needs to run.  Each job takes a JSON payload and writes its outputs to S3 — no database access, no internal state.

## Features

 * `build_model` — Takes a reach ID and build parameters; produces terrain, roughness, geometry artifacts.
 * `run_nd_scenarios` — Takes a model and a discharge range; runs normal-depth simulations across that range and writes depth rasters.
 * `run_kwse_scenarios` — Takes a model and a set of boundary condition scenarios (downstream condition + upstream discharge) and runs the full combination of all conditions.

## Package Architecture

Brief description of the major components.

```text
twod_fim_jobs
├── __init__.py
├── cli.py                    # Entry point; parses job names and JSON payloads to dispatch workflows
├── consts.py                 # Shared constants (DEM/LULC sources, field names, processing thresholds)
├── exceptions.py             # Custom exception classes for reach database and data processing errors
├── jobs
│   ├── __init__.py
│   ├── build_model.py        # Job: initializes a 2D FIM model for a single reach
│   ├── run_nd_scenarios.py   # Job: runs normal depth process for a single reach
│   ├── run_kwse_scenarios.py # Job: runs a set of KWSE runs for a single reach
│   ├── common.py             # Abstract base Job class with input validation, logging, and temp directory management
│   └── health.py             # Job: health check that imports all modules to verify container environment
├── models
│   ├── __init__.py
│   ├── build_model.py        # Pydantic models for BuildModelInputs, BuildModelResult, and related types
│   ├── run_nd_scenarios.py   # Pydantic models for RunNDInputs, RunNDResult, and related types
│   ├── run_kwse_scenarios.py # Pydantic models for RunKWSEModelInputs, RunKWSEResult, and related types
│   ├── common.py             # Shared Pydantic models: Asset (file references) and JobWarning base class
│   ├── warnings.py           # JobWarning subclasses for domain-specific warning codes
│   └── generate_docs.py      # Generates schemas, example JSON, and markdown docs for each job
└── utils
    ├── geospatial.py         # Utilities for line intersections, domain building, and raster clipping/reprojection
    ├── hashing.py            # SHA256 hash helpers for dicts, strings, geometries, and files
    └── storage.py            # Database utilities for querying reach geometries from PostgreSQL/SQLite hydrofabric DBs
```


## Installation

### Installation With pip

```bash
git clone https://github.com/NGWPC/twod-fim-jobs.git
cd twod-fim-jobs
python -m venv .venv
source .venv/bin/activate
pip install .
```

### Installation With pixi

```bash
git clone https://github.com/NGWPC/twod-fim-jobs.git
cd twod-fim-jobs
pixi install
```

### Installation With docker

The Dockerfile defines several named stages. Build the stage that matches the job you want to run:

```bash
git clone https://github.com/NGWPC/twod-fim-jobs.git
cd twod-fim-jobs

# Base image (includes all jobs via the generic entrypoint)
docker build --target two-dim-fim-base -t twod-fim-jobs:base .

# Job-specific images
docker build --target health -t twod-fim-jobs:health .
docker build --target build_model -t twod-fim-jobs:build_model .
docker build --target run_kwse_scenarios-sfincs -t twod-fim-jobs:run_kwse_scenarios . # (not yet implemented)
docker build --target run_nd_scenarios-lisflood -t twod-fim-jobs:run_nd_scenarios .

```

## Quick Start

Check that the package has installed correctly.

```bash
twod_fim_jobs health {}
```

```bash
pixi run twod_fim_jobs health {}
```

```bash
docker run -v ./:/mount twod-fim-jobs:health {}
```

Expected output:

```bash
Health check passed.
```

## Configuration

This package requires AWS credentials in order to obtain MRLC LULC data from USGS. Those AWS credentials are also used if results should be read and/or written to AWS S3.

Copy `.env.template` to `.env` and fill in your credentials:

```bash
AWS_REQUEST_PAYER=requester
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
```

Then load the variables into your shell before running:

```bash
source load_env.sh
```

### Environment Variables

| Variable                  | Description                                                    | Allowed Values                | Required |
|---------------------------|----------------------------------------------------------------|-------------------------------|----------|
| `AWS_REQUEST_PAYER`       | Enables access to requester-pays S3 buckets.                   | `requester`                   | Yes      |
| `AWS_ACCESS_KEY_ID`       | AWS access key ID used for authentication.                     | Any valid AWS access key ID   | Yes      |
| `AWS_SECRET_ACCESS_KEY`   | AWS secret access key used for authentication.                 | Any valid AWS secret key      | Yes      |

For a full list of optional environment variables — including data source overrides, database schema settings, and solver tuning parameters — see [docs/deployment_configuration.md](docs/deployment_configuration.md).


## Command Line Interface

The command line interface is consistent across all jobs.  It takes a first argument for the name of the job and a second argument of a json of job inputs.

### `health` Job

This job is set up as a simple check for whether the package was installed correctly. It has no data requirements.

Example command:

```bash
twod_fim_jobs health {}
```

The job has an optional feature to test write access.

```bash
twod_fim_jobs health '{"test_write_uri": "output/copier.txt"}'
twod_fim_jobs health '{"test_write_uri": "s3://bucket/copier.txt"}'
```

### `build_model` Job

Given a reach ID and build parameters, this job queries an NHD-based hydrofabric database for the target reach's geometry and attributes, constructs a 2D model domain (bounding box, inflow line, anchor point), and downloads clipped/reprojected terrain (DEM) and roughness (LULC-derived Manning's n) rasters aligned to the requested grid resolution. It then writes all geospatial vector artifacts and a JSON model manifest to the specified output path (local or S3).

Example command (local install):

```bash
twod_fim_jobs build_model '{
  "reach_id": "12345",
  "db_uri": "sqlite:////path/to/hydrofabric.gpkg",
  "base_output_path": "output/"
}'
```

Example command (Docker w/ test data and S3 write):

```bash
docker run --rm \
  -v "$(pwd)":/mount \
  --env-file .env \
  twod-fim-jobs:build_model \
  '{
    "reach_id": 1257410962372414,
    "db_uri": "sqlite:////mount/tests/build_model/data/reach_network.gpkg",
    "base_output_path": "s3://bucket/prefix"
  }'
```

#### Outputs

The job writes a self-contained model directory under `base_output_path`. The directory name is derived from the computed `model_id` (`<identity_hash>_<domain_code>`).

```text
<base_output_path>/
└── <model_id>/               # e.g. 5f14368c_N350S296E449W355/
    ├── model.json            # Model manifest (inputs, domain, identity, properties, asset references)
    ├── dem.tif               # Clipped and reprojected terrain raster
    ├── roughness.tif         # Manning's n raster derived from LULC
    ├── reach.geojson         # River centerline geometry
    ├── anchor.geojson        # Centroid of the river centerline
    └── domain.geojson        # Derived model domain polygon
```

The job returns a JSON result payload on stdout:

```json
{
  "identity_hash": "5f14368c",
  "model_id": "5f14368c_N350S296E449W355",
  "model_dir": "output/5f14368c_N350S296E449W355",
  "warnings": []
}
```

### `run_nd_scenarios` Job

Given a previously built model and a discharge range, this job runs a series of normal-depth hydraulic simulations across that range using an adaptive step algorithm. It reads the model manifest, pre-processes solver inputs, and writes depth rasters and scenario manifests to the specified output path (local or S3).

Example command (local install):

```bash
twod_fim_jobs run_nd_scenarios '{
  "model_manifest_path": "example/path/model.json",
  "model_results_base_path": "example/path/results/",
  "min_upstream_inflow": 10.0,
  "max_upstream_inflow": 500.0,
  "delta_upstream_inflow": 50.0,
  "ds_slope": 0.001,
  "outflow_area_polygon_path": "common/path/outflow_area.geojson"
}'
```

#### Outputs

The job writes one subdirectory per scenario under `model_results_base_path`, grouped first by a solver+sdr-specific run identity hash and then by the downstream slope and discharge values.

```text
<model_results_base_path>/
└── <run_identity_hash>/          # e.g. a1b2c3d4/ — hash of solver identity + sdr commit id
    └── nd=<slope>/               # e.g. nd=1E-03/
        └── q=<discharge>/        # e.g. q=150.000/
            ├── scenario_manifest.json  # Scenario manifest (inputs, properties, asset references)
            ├── depth.tif               # Water depth raster at final timestep
            ├── inundation.geojson      # Inundated area polygon at final timestep
            └── stage_transfer_line.geojson  # Stage transfer line geometry
```

The job returns a JSON result payload on stdout:

```json
{
  "scenario_manifest_paths": [
    "output/.../results/a1b2c3d4/nd=1E-03/q=150.000/scenario_manifest.json"
  ],
  "scenario_comparison_results": [
    null,
    {
      "ref_us_discharge": 100.0,
      "trial_us_discharge": 150.0,
      "max_stage_diff": 0.05,
      "median_stage_diff": 0.02,
      "extent_diff": 0.03,
      "result": "accept"
    }
  ],
  "warnings": []
}
```

Each entry in `scenario_comparison_results` corresponds to the entry at the same index in `scenario_manifest_paths`. The first scenario (baseline) and the max-discharge scenario are always `null`. For all others, the object records the discharge of the reference and trial scenarios, the maximum and median water surface elevation difference between them, the normalized difference in inundation extent, and the adaptive step algorithm's `result` (`"accept"`, `"reject_high"`, or `"reject_low"`).

## Development

### Running Tests

```bash
pytest
```

### Linting

```bash
ruff check .
ruff format .
```

### Type Checking

```bash
pyright
```

### Export JSON Schemas

To autogenerate tables, examples, and schemas for the docs folder, run one of the following commands

```bash
python -m twod_fim_jobs.models.generate_docs

pixi run generate_docs
```
