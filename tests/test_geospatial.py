from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.crs import CRS
from rasterio.transform import from_bounds

from twod_fim_jobs.exceptions import DatasetUnavailableError
from twod_fim_jobs.utils.geospatial import _extract_raster, extract_raster


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def tiny_src_tif(tmp_path_factory) -> Path:
    """A minimal 8x8 float32 GeoTIFF used as a real source raster for _extract_raster tests."""
    path = tmp_path_factory.mktemp("geo_data") / "src.tif"
    rows, cols = 8, 8
    bbox = (-2_100_000.0, 2_750_000.0, -2_050_000.0, 2_800_000.0)
    profile = {
        "driver": "GTiff",
        "dtype": "float32",
        "width": cols,
        "height": rows,
        "count": 1,
        "crs": CRS.from_epsg(5070),
        "transform": from_bounds(*bbox, cols, rows),
        "nodata": -9999.0,
    }
    data = np.arange(rows * cols, dtype="float32").reshape(rows, cols)
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data, 1)
    return path


# ---------------------------------------------------------------------------
# extract_raster (public wrapper) error mapping
# ---------------------------------------------------------------------------


def test_extract_raster_missing_source_raises_dataset_unavailable(tmp_path: Path):
    """Missing source raster is mapped to DatasetUnavailableError."""
    with pytest.raises(DatasetUnavailableError):
        extract_raster(
            src_path=tmp_path / "does_not_exist.tif",
            out_path=tmp_path / "out.tif",
            bbox=(0.0, 0.0, 1.0, 1.0),
            cols=1,
            rows=1,
            dst_crs="EPSG:4326",
        )


# ---------------------------------------------------------------------------
# _extract_raster unit tests (no network, tiny local raster)
# ---------------------------------------------------------------------------


def test_extract_raster_writes_output(tiny_src_tif: Path, tmp_path: Path):
    """Output file is created with the requested dimensions."""
    out = tmp_path / "out.tif"
    _extract_raster(
        src_path=tiny_src_tif,
        out_path=out,
        bbox=(-2_100_000.0, 2_750_000.0, -2_050_000.0, 2_800_000.0),
        cols=4,
        rows=4,
        dst_crs="EPSG:5070",
    )
    assert out.exists()
    with rasterio.open(out) as ds:
        assert ds.width == 4
        assert ds.height == 4
        assert ds.dtypes[0] == "float32"


def test_extract_raster_reprojects(tiny_src_tif: Path, tmp_path: Path):
    """Output CRS matches the requested dst_crs."""
    out = tmp_path / "reprojected.tif"
    _extract_raster(
        src_path=tiny_src_tif,
        out_path=out,
        bbox=(-94.5, 38.5, -94.0, 39.0),
        cols=4,
        rows=4,
        dst_crs="EPSG:4326",
    )
    with rasterio.open(out) as ds:
        assert CRS.from_string(ds.crs.to_string()) == CRS.from_epsg(4326)


def test_extract_raster_value_transform_applied(tiny_src_tif: Path, tmp_path: Path):
    """value_transform is applied to every pixel before writing."""
    out = tmp_path / "transformed.tif"
    _extract_raster(
        src_path=tiny_src_tif,
        out_path=out,
        bbox=(-2_100_000.0, 2_750_000.0, -2_050_000.0, 2_800_000.0),
        cols=4,
        rows=4,
        dst_crs="EPSG:5070",
        value_transform=lambda d: d * 0.0,  # zero everything out
    )
    with rasterio.open(out) as ds:
        data = ds.read(1)
    assert np.all(data == 0.0)


def test_extract_raster_returns_asset_with_correct_href(
    tiny_src_tif: Path, tmp_path: Path
):
    """Returned Asset href points to the output file."""
    out = tmp_path / "out.tif"
    asset = _extract_raster(
        src_path=tiny_src_tif,
        out_path=out,
        bbox=(-2_100_000.0, 2_750_000.0, -2_050_000.0, 2_800_000.0),
        cols=4,
        rows=4,
        dst_crs="EPSG:5070",
    )
    assert asset.href == str(out)
