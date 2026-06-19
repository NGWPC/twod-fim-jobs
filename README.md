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
├── cli.py                   # Entry point; parses job names and JSON payloads to dispatch workflows
├── consts.py                # Shared constants (DEM/LULC sources, field names, processing thresholds)
├── exceptions.py            # Custom exception classes for reach database and data processing errors
├── jobs
│   ├── __init__.py
│   ├── build_model.py       # Job: initializes a 2D FIM model for a single reach
│   ├── common.py            # Abstract base Job class with input validation, logging, and temp directory management
│   └── health.py            # Job: health check that imports all modules to verify container environment
├── models
│   ├── __init__.py
│   ├── build_model.py       # Pydantic models for BuildModelInputs, BuildModelResult, and related types
│   └── common.py            # Shared Pydantic models: Asset (file references) and JobWarning base class
└── utils
    ├── geospatial.py        # Utilities for line intersections, domain building, and raster clipping/reprojection
    ├── hashing.py           # SHA256 hash helpers for dicts, strings, geometries, and files
    └── storage.py           # Database utilities for querying reach geometries from PostgreSQL/SQLite hydrofabric DBs
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

```bash
git clone https://github.com/NGWPC/twod-fim-jobs.git
cd twod-fim-jobs
docker build --target prod -t twod-fim-runner .
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
docker run -v ./:/mount twod-fim-runner health {}
```

Expected output:

```bash
Health check passed.
```

## Configuration

This package requires AWS credentials in order to obtain MRLC LULC data from USGS. Those AWS credentials are also used if results should be read and/or written to AWS S3.  See `.env.template` for an example env file or the table below.

### Environment Variables

### Environment Variables

| Variable                  | Description                                                    | Allowed Values                | Required |
|---------------------------|----------------------------------------------------------------|-------------------------------|----------|
| `AWS_REQUEST_PAYER`       | Enables access to requester-pays S3 buckets.                   | `requester`                   | Yes      |
| `AWS_ACCESS_KEY_ID`       | AWS access key ID used for authentication.                     | Any valid AWS access key ID   | Yes      |
| `AWS_SECRET_ACCESS_KEY`   | AWS secret access key used for authentication.                 | Any valid AWS secret key      | Yes      |


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

Example command:

```bash
twod_fim_jobs build_model '{
  "reach_id": "12345",
  "db_uri": "sqlite:////path/to/hydrofabric.gpkg",
  "base_output_path": "output/",
}'
```

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

### Export json schemas

```bash
python twod_fim_jobs/models/export_schemas.py
```
