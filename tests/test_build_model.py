from pathlib import Path
import json

import geopandas as gpd
import pandas as pd
import pytest
from pydantic import ValidationError
from shapely.geometry import LineString

from twod_fim_jobs.exceptions import (
    DuplicateReachError,
    InvalidAttributeError,
    InvalidWKTGeometryError,
    ReachDatasetUnavailable,
    ReachNotFoundError,
)
from twod_fim_jobs.jobs.build_model import BuildModelJob, _check_inflow_cl_intersection
from twod_fim_jobs.jobs.build_model import _normalize_href
from twod_fim_jobs.models.build_model import BuildModelInputs
from twod_fim_jobs.models.warnings import (
    LargeDomainAreaWarning,
    CenterlineInflowMultiIntersectionWarning,
)
from datetime import datetime
import numpy as np
import pytest_mock
import rasterio
from rasterio.crs import CRS
from rasterio.transform import from_bounds

from twod_fim_jobs.models.common import Asset
from twod_fim_jobs.utils.storage import read_json


ROOT = Path(__file__).parent
SMALL_NETWORK = ROOT / "test_data" / "reference_data" / "reach_network.parquet"
SMALL_NETWORK_BAD_ATTRIBUTES = (
    ROOT / "test_data" / "reference_data" / "reach_network_bad_attr.parquet"
)
SMALL_NETWORK_DUPLICATE_ID = (
    ROOT / "test_data" / "reference_data" / "reach_network_duplicates.parquet"
)
ADDITIONAL_GEOMETRY_STR = (
    "LineString (-2061815.1006158 2807427.37585646, -2055051.17482305 2811274.51580549)"
)


### FIXTURES ###


@pytest.fixture(scope="session")
def tiny_geotiff(tmp_path_factory) -> Path:
    """A minimal 10x10 float32 GeoTIFF in EPSG:5070 used as a stand-in source raster."""
    path = tmp_path_factory.mktemp("raster_data") / "tiny.tif"
    rows, cols = 10, 10
    bbox = (-2_100_000.0, 2_750_000.0, -2_050_000.0, 2_800_000.0)
    transform = from_bounds(*bbox, cols, rows)
    profile = {
        "driver": "GTiff",
        "dtype": "float32",
        "width": cols,
        "height": rows,
        "count": 1,
        "crs": CRS.from_epsg(5070),
        "transform": transform,
        "nodata": -9999.0,
    }
    data = np.arange(rows * cols, dtype="float32").reshape(rows, cols)
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data, 1)
    return path


def _make_extract_raster_mock(tmp_path: Path):
    """Return a mock that writes a valid GeoTIFF to out_path and returns an Asset."""

    def _fake_extract_raster(src_path, out_path, bbox, cols, rows, dst_crs, **kwargs):
        transform = from_bounds(*bbox, cols, rows)
        profile = {
            "driver": "GTiff",
            "dtype": "float32",
            "width": cols,
            "height": rows,
            "count": 1,
            "crs": CRS.from_string(dst_crs),
            "transform": transform,
            "nodata": -9999.0,
        }
        data = np.zeros((rows, cols), dtype="float32")
        with rasterio.open(out_path, "w", **profile) as dst:
            dst.write(data, 1)
        return Asset.from_file(str(out_path), str(src_path), datetime.now())

    return _fake_extract_raster


@pytest.fixture
def mock_extract_raster(mocker: pytest_mock.MockerFixture, tmp_path: Path):
    """Patch extract_raster at the jobs.build_model import site to avoid remote I/O."""
    fake = _make_extract_raster_mock(tmp_path)
    return mocker.patch(
        "twod_fim_jobs.utils.geospatial.extract_raster",
        side_effect=fake,
    )


@pytest.fixture
def build_model_input():
    return BuildModelInputs(
        reach_id=1257410962372414,
        reach_network_path=str(SMALL_NETWORK.resolve()),
        base_output_path="/tmp/test-output",
    )


@pytest.fixture
def build_model_input_headwater():
    return BuildModelInputs(
        reach_id=1257411073114277,
        reach_network_path=str(SMALL_NETWORK.resolve()),
        base_output_path="/tmp/test-output",
    )


@pytest.fixture
def build_model_input_bad_connection():
    return BuildModelInputs(
        reach_id=1257410962372414,
        reach_network_path="FAKE_PATH",
        base_output_path="/tmp/test-output",
    )


@pytest.fixture
def build_model_input_bad_attributes():
    return BuildModelInputs(
        reach_id=1257410962372414,
        reach_network_path=str(SMALL_NETWORK_BAD_ATTRIBUTES.resolve()),
        base_output_path="/tmp/test-output",
    )


@pytest.fixture
def build_model_input_missing_reach():
    return BuildModelInputs(
        reach_id=1,
        reach_network_path=str(SMALL_NETWORK.resolve()),
        base_output_path="/tmp/test-output",
    )


@pytest.fixture
def build_model_input_duplicate_ids():
    return BuildModelInputs(
        reach_id=1257410962372414,
        reach_network_path=str(SMALL_NETWORK_DUPLICATE_ID.resolve()),
        base_output_path="/tmp/test-output",
    )


@pytest.fixture
def build_model_input_w_extra_geometries():
    return BuildModelInputs(
        reach_id=1257410962372414,
        reach_network_path=str(SMALL_NETWORK.resolve()),
        base_output_path="/tmp/test-output",
        other_geometries=[ADDITIONAL_GEOMETRY_STR],
    )


@pytest.fixture
def build_model_input_w_bad_extra_geometries():
    return BuildModelInputs(
        reach_id=1257410962372414,
        reach_network_path=str(SMALL_NETWORK.resolve()),
        base_output_path="/tmp/test-output",
        other_geometries=["BAD"],
    )


### TESTS ###


def test_end_to_end(build_model_input, tmp_path, mock_extract_raster):
    """Build a model without warnings and include the centerline buffer in its domain."""
    workflow = BuildModelJob()
    model_input = build_model_input.model_copy(
        update={"base_output_path": str(tmp_path)}
    )
    result = workflow.run(model_input)
    assert result.warnings == [], f"Unexpected warnings: {result.warnings}"
    manifest_path = tmp_path / result.model_id / "model_manifest.json"
    manifest = json.loads(read_json(manifest_path))

    domain_bbox = manifest["domain"]["bbox"]
    centerline_bbox = manifest["assets"]["centerline"]["href"]
    inflow_bbox = manifest["assets"]["inflow_line"]["href"]
    centerline = gpd.read_file(centerline_bbox)
    inflow = gpd.read_file(inflow_bbox)
    without_buffer = gpd.GeoDataFrame(
        geometry=pd.concat([centerline.geometry, inflow.geometry], ignore_index=True),
        crs=centerline.crs,
    ).total_bounds

    assert domain_bbox[0] < without_buffer[0]
    assert domain_bbox[1] < without_buffer[1]
    assert domain_bbox[2] > without_buffer[2]
    assert domain_bbox[3] > without_buffer[3]


def test_end_to_end_w_other_geom(
    build_model_input_w_extra_geometries, mock_extract_raster
):
    """End to end test that should run without failure."""
    workflow = BuildModelJob()
    workflow.run(build_model_input_w_extra_geometries)


def test_end_to_end_headwater(build_model_input_headwater, mock_extract_raster):
    """End to end test that should run without failure and produce no warnings."""
    workflow = BuildModelJob()
    result = workflow.run(build_model_input_headwater)
    assert result.warnings == [], f"Unexpected warnings: {result.warnings}"


def test_inputs_missing_required_arg_raises():
    """Build_model input validation fails when required args are omitted."""
    with pytest.raises(ValidationError):
        BuildModelInputs(
            reach_id=1257410962372414,
            reach_network_path=f"sqlite:///{SMALL_NETWORK.resolve()}",
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
        (
            "s3://bucket/prefix/dem.tif",
            "s3://bucket/output",
            "s3://bucket/output/dem.tif",
        ),
        (
            "s3://bucket/prefix/dem.tif",
            "s3://bucket/output/",
            "s3://bucket/output/dem.tif",
        ),
        (
            "s3://bucket/a/b/c/dem.tif",
            "s3://other-bucket/x/y",
            "s3://other-bucket/x/y/dem.tif",
        ),
        # Linux absolute paths
        ("/tmp/working/dem.tif", "/data/output", "/data/output/dem.tif"),
        ("/tmp/working/dem.tif", "/data/output/", "/data/output/dem.tif"),
        # Linux relative paths
        ("dem.tif", "/data/output", "/data/output/dem.tif"),
        ("subdir/dem.tif", "/data/output", "/data/output/dem.tif"),
        # Filenames with multiple dots
        (
            "s3://bucket/prefix/my.model.v2.tif",
            "s3://bucket/out",
            "s3://bucket/out/my.model.v2.tif",
        ),
        # Mixed: local href into S3 base
        ("/tmp/dem.tif", "s3://bucket/output", "s3://bucket/output/dem.tif"),
        # Mixed: S3 href into local base
        ("s3://bucket/prefix/dem.tif", "/data/output", "/data/output/dem.tif"),
        # Deep nesting — only the filename should be carried over
        (
            "s3://bucket/a/b/c/d/e/reach.geojson",
            "s3://bucket/out",
            "s3://bucket/out/reach.geojson",
        ),
    ],
)
def test_normalize_href(href, new_base_path, expected):
    assert _normalize_href(href, new_base_path) == expected


def test_large_domain_area_warning_emitted(
    build_model_input, mock_extract_raster, monkeypatch
):
    """When domain area exceeds the threshold a LargeDomainAreaWarning is in the result."""
    # Force threshold to zero so any domain triggers the warning.
    monkeypatch.setattr(
        "twod_fim_jobs.jobs.build_model.LARGE_DOMAIN_AREA_THRESHOLD", 0.0
    )
    result = BuildModelJob().run(build_model_input)
    large_domain_warnings = [
        w for w in result.warnings if isinstance(w, LargeDomainAreaWarning)
    ]
    assert len(large_domain_warnings) == 1
    w = large_domain_warnings[0]
    assert w.threshold == 0.0
    assert w.domain_area > 0.0


def _make_cl_gdf(coords: list[tuple]) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(geometry=[LineString(coords)])


def test_check_inflow_cl_single_intersection_returns_none():
    """A simple crossing at one point should produce no warning."""
    centerline = _make_cl_gdf([(0, 0), (10, 0)])
    inflow = _make_cl_gdf([(5, -1), (5, 1)])
    assert _check_inflow_cl_intersection(centerline, inflow) is None


def test_check_inflow_cl_multiple_intersections_returns_warning():
    """An inflow that crosses the centerline twice should return the warning with both points."""
    centerline = _make_cl_gdf([(0, 0), (10, 0)])
    # This inflow dips below, crosses up, then back down — two crossings of y=0.
    inflow = _make_cl_gdf([(2, -1), (4, 1), (6, -1)])
    warning = _check_inflow_cl_intersection(centerline, inflow)
    assert isinstance(warning, CenterlineInflowMultiIntersectionWarning)
    assert len(warning.intersection_points) == 2
