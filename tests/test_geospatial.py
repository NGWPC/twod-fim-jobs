from pathlib import Path

import pytest

from twod_fim_jobs.exceptions import DatasetUnavailableError
from twod_fim_jobs.utils.geospatial import extract_raster


def test_extract_raster_missing_source_raises_dataset_unavailable(tmp_path: Path):
    """Missing source raster is mapped to DatasetUnavailableError."""
    missing_src = tmp_path / "does_not_exist.tif"
    out_path = tmp_path / "out.tif"

    with pytest.raises(DatasetUnavailableError):
        extract_raster(
            src_path=missing_src,
            out_path=out_path,
            bbox=(0.0, 0.0, 1.0, 1.0),
            cols=1,
            rows=1,
            dst_crs="EPSG:4326",
        )
