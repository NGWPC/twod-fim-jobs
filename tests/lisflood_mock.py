"""Utilities for generating synthetic LISFLOOD outputs in tests."""

from pathlib import Path

import numpy as np
import rasterio
import subprocess


def parse_parfile(parfile_path: Path) -> dict[str, str]:
    par = {}
    for line in parfile_path.read_text().splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            par[parts[0]] = parts[1].strip()
        elif len(parts) == 1:
            par[parts[0]] = ""
    return par


def read_qfix_sum(bci_path: Path) -> float:
    # BCI row format: <type> <x0> <x1> QFIX <value>  (QFIX is always at index 3)
    total = 0.0
    for line in Path(bci_path).read_text().splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[3] == "QFIX":
            total += float(parts[4])
    return total


def read_dem_profile(dem_path: Path) -> dict:
    with rasterio.open(dem_path) as src:
        return src.profile.copy()


def make_depth_grid(nrows: int, ncols: int, depth: float) -> np.ndarray:
    grid = np.zeros((nrows, ncols), dtype=np.float32)
    grid[1:-1, 1:-1] = depth
    return grid


def write_asc(path: Path, grid: np.ndarray, profile: dict) -> None:
    asc_profile = {
        "driver": "AAIGrid",
        "dtype": grid.dtype,
        "nodata": profile.get("nodata", -9999),
        "width": grid.shape[1],
        "height": grid.shape[0],
        "count": 1,
        "crs": profile.get("crs"),
        "transform": profile["transform"],
    }
    with rasterio.open(path, "w", **asc_profile) as dst:
        dst.write(grid, 1)


def write_mock_lisflood_outputs(parfile_path: Path) -> None:
    par = parse_parfile(parfile_path)
    qfix_sum = read_qfix_sum(Path(par["bcifile"]))
    dem_profile = read_dem_profile(Path(par["DEMfile"]))

    nrows = dem_profile["height"]
    ncols = dem_profile["width"]
    inundated_area = max((nrows - 2) * (ncols - 2), 1)
    depth1 = (qfix_sum * float(par["saveint"])) / inundated_area

    grids = [
        make_depth_grid(nrows, ncols, depth1),
        make_depth_grid(nrows, ncols, depth1 * 2),
        make_depth_grid(nrows, ncols, depth1 * 2),
    ]
    dirroot = Path(par["dirroot"])
    resroot = par["resroot"]
    for i, grid in enumerate(grids, start=1):
        write_asc(dirroot / f"{resroot}-{i:04d}.wd", grid, dem_profile)


def fake_run_lisflood(
    parfile_path: Path, pipe_out_logs: bool = True
) -> subprocess.Popen:
    write_mock_lisflood_outputs(parfile_path)
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc
