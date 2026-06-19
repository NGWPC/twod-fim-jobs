from pathlib import Path

import pytest
from pydantic import ValidationError

from twod_fim_jobs.exceptions import (
    DuplicateReachError,
    InvalidAttributeError,
    InvalidWKTGeometryError,
    ReachDatasetUnavailable,
    ReachNotFoundError,
)
from twod_fim_jobs.jobs.build_model import BuildModelJob
from twod_fim_jobs.jobs.build_model import _normalize_href
from twod_fim_jobs.models.build_model import BuildModelInputs

ROOT = Path(__file__).parent
SMALL_NETWORK = ROOT / "data" / "reach_network.gpkg"
SMALL_NETWORK_BAD_ATTRIBUTES = ROOT / "data" / "reach_network_bad_attr.gpkg"
SMALL_NETWORK_DUPLICATE_ID = ROOT / "data" / "reach_network_duplicates.gpkg"
ADDITIONAL_GEOMETRY_STR = (
    "LineString (-2061815.1006158 2807427.37585646, -2055051.17482305 2811274.51580549)"
)


### FIXTURES ###


@pytest.fixture
def build_model_input():
    return BuildModelInputs(
        reach_id=1257410962372414,
        db_uri=f"sqlite:///{SMALL_NETWORK.resolve()}",
        base_output_path="/tmp/test-output",
    )


@pytest.fixture
def build_model_input_bad_connection():
    return BuildModelInputs(
        reach_id=1257410962372414,
        db_uri="sqlite:////FAKE_PATH",
        base_output_path="/tmp/test-output",
    )


@pytest.fixture
def build_model_input_bad_attributes():
    return BuildModelInputs(
        reach_id=1257410962372414,
        db_uri=f"sqlite:///{SMALL_NETWORK_BAD_ATTRIBUTES.resolve()}",
        base_output_path="/tmp/test-output",
    )


@pytest.fixture
def build_model_input_missing_reach():
    return BuildModelInputs(
        reach_id=1,
        db_uri=f"sqlite:///{SMALL_NETWORK.resolve()}",
        base_output_path="/tmp/test-output",
    )


@pytest.fixture
def build_model_input_duplicate_ids():
    return BuildModelInputs(
        reach_id=1257410962372414,
        db_uri=f"sqlite:///{SMALL_NETWORK_DUPLICATE_ID.resolve()}",
        base_output_path="/tmp/test-output",
    )


@pytest.fixture
def build_model_input_w_extra_geometries():
    return BuildModelInputs(
        reach_id=1257410962372414,
        db_uri=f"sqlite:///{SMALL_NETWORK.resolve()}",
        base_output_path="/tmp/test-output",
        other_geometries=[ADDITIONAL_GEOMETRY_STR],
    )


@pytest.fixture
def build_model_input_w_bad_extra_geometries():
    return BuildModelInputs(
        reach_id=1257410962372414,
        db_uri=f"sqlite:///{SMALL_NETWORK.resolve()}",
        base_output_path="/tmp/test-output",
        other_geometries=["BAD"],
    )


### TESTS ###


def test_end_to_end(build_model_input):
    """End to end test that should run without failure."""
    workflow = BuildModelJob()
    workflow.run(build_model_input)


def test_end_to_end_w_other_geom(build_model_input_w_extra_geometries):
    """End to end test that should run without failure."""
    workflow = BuildModelJob()
    workflow.run(build_model_input_w_extra_geometries)


def test_inputs_missing_required_arg_raises():
    """Build_model input validation fails when required args are omitted."""
    with pytest.raises(ValidationError):
        BuildModelInputs(
            reach_id=1257410962372414,
            db_uri=f"sqlite:///{SMALL_NETWORK.resolve()}",
        )


def test_bad_db_connection_raises(build_model_input_bad_connection):
    """Unreachable database raises DatasetUnavailableError."""
    workflow = BuildModelJob()
    with pytest.raises(ReachDatasetUnavailable):
        workflow.run(build_model_input_bad_connection)


def test_bad_attributes_raises(build_model_input_bad_attributes):
    """Database missing required fields raises InvalidAttributeError."""
    workflow = BuildModelJob()
    with pytest.raises(InvalidAttributeError):
        workflow.run(build_model_input_bad_attributes)


def test_missing_reach_raises(build_model_input_missing_reach):
    """Reach ID not present in database raises ReachNotFoundError."""
    workflow = BuildModelJob()
    with pytest.raises(ReachNotFoundError):
        workflow.run(build_model_input_missing_reach)


def test_duplicate_reach_raises(build_model_input_duplicate_ids):
    """Reach ID not present in database raises ReachNotFoundError."""
    workflow = BuildModelJob()
    with pytest.raises(DuplicateReachError):
        workflow.run(build_model_input_duplicate_ids)


def test_bad_other_geometries_raises(build_model_input_w_bad_extra_geometries):
    """Reach ID not present in database raises ReachNotFoundError."""
    workflow = BuildModelJob()
    with pytest.raises(InvalidWKTGeometryError):
        workflow.run(build_model_input_w_bad_extra_geometries)


@pytest.mark.parametrize(
    "href, new_base_path, expected",
    [
        # S3 URIs
        ("s3://bucket/prefix/dem.tif", "s3://bucket/output", "s3://bucket/output/dem.tif"),
        ("s3://bucket/prefix/dem.tif", "s3://bucket/output/", "s3://bucket/output/dem.tif"),
        ("s3://bucket/a/b/c/dem.tif", "s3://other-bucket/x/y", "s3://other-bucket/x/y/dem.tif"),
        # Linux absolute paths
        ("/tmp/working/dem.tif", "/data/output", "/data/output/dem.tif"),
        ("/tmp/working/dem.tif", "/data/output/", "/data/output/dem.tif"),
        # Linux relative paths
        ("dem.tif", "/data/output", "/data/output/dem.tif"),
        ("subdir/dem.tif", "/data/output", "/data/output/dem.tif"),
        # Filenames with multiple dots
        ("s3://bucket/prefix/my.model.v2.tif", "s3://bucket/out", "s3://bucket/out/my.model.v2.tif"),
        # Mixed: local href into S3 base
        ("/tmp/dem.tif", "s3://bucket/output", "s3://bucket/output/dem.tif"),
        # Mixed: S3 href into local base
        ("s3://bucket/prefix/dem.tif", "/data/output", "/data/output/dem.tif"),
        # Deep nesting — only the filename should be carried over
        ("s3://bucket/a/b/c/d/e/reach.geojson", "s3://bucket/out", "s3://bucket/out/reach.geojson"),
    ],
)
def test_normalize_href(href, new_base_path, expected):
    assert _normalize_href(href, new_base_path) == expected
