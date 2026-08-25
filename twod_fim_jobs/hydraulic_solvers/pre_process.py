from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

import numpy as np
import rasterio
import rasterio.transform
from affine import Affine
from shapely import LineString, MultiLineString, MultiPolygon, Point, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import linemerge, unary_union
import geopandas as gpd
from twod_fim_jobs.exceptions import NonIntersectingKWSELine
from twod_fim_jobs.models.build_model import Domain, GridProperties
from twod_fim_jobs.models.common import Asset
from twod_fim_jobs.utils.geospatial import Raster, rasterize_geometry, tif_to_asc
from twod_fim_jobs.models.solvers import (
    BoundaryConditionElement,
    QFixBC,
    RunConfig,
    RunScenarioInputs,
    BoundaryCondition,
    TransferBC,
)
from twod_fim_jobs.consts import (
    DEFAULT_RESROOT_LISFLOOD,
    SCENARIO_SOLVER,
    SupportedSolver,
)
from twod_fim_jobs.utils.storage import ASSET_CACHE

logger = logging.getLogger(__name__)


### CLASSES ###


class LisfloodWriter:
    def export(self, run_scenario_inputs: RunScenarioInputs, working_dir: Path) -> Path:
        bc_elements = process_bc_lines(
            run_scenario_inputs.boundary_conditions,
            run_scenario_inputs.domain,
            run_scenario_inputs.grid_properties,
        )
        working_dir.mkdir(parents=True, exist_ok=True)
        bci_path = self.write_bci_file(bc_elements, working_dir)
        par_path = self.write_par_file(
            run_scenario_inputs.terrain,
            run_scenario_inputs.roughness,
            run_scenario_inputs.run_config,
            bci_path,
            working_dir,
            run_scenario_inputs.hot_start,
        )
        return par_path

    def write_bci_file(
        self,
        bcs: list[BoundaryConditionElement],
        output_dir: Path,
    ) -> Path:
        bci_path = output_dir / "lisflood.bci"
        lines = [
            f"{bc.element_type} {round(bc.x_coord, 4)} {round(bc.y_coord, 4)} {bc.bc_type} {bc.value}\n"
            for bc in bcs
        ]
        bci_path.write_text("".join(lines))
        return bci_path

    def write_par_file(
        self,
        terrain: Asset,
        roughness: Asset,
        run_config: RunConfig,
        bci_path: Path,
        output_dir: Path,
        hot_start: Asset | None,
    ) -> Path:
        par_path = output_dir / "lisflood.par"

        resolved_terrain = ASSET_CACHE.materialize_path(terrain)
        resolved_roughness = ASSET_CACHE.materialize_path(roughness)
        resolved_terrain = tif_to_asc(resolved_terrain)
        resolved_roughness = tif_to_asc(resolved_roughness)

        cfg: dict[str, object] = {
            "resroot": DEFAULT_RESROOT_LISFLOOD,
            "dirroot": str(output_dir),
            "DEMfile": resolved_terrain,
            "manningfile": resolved_roughness,
            "bcifile": str(bci_path),
            "saveint": run_config.save_interval_seconds,
            "massint": run_config.mass_interval_seconds,
            "sim_time": run_config.sim_time_seconds,
            "initial_tstep": run_config.initial_tstep_seconds,
            "acceleration": "",
        }
        if run_config.use_cuda:
            cfg["cuda"] = ""
        if run_config.use_elevoff:
            cfg["elevoff"] = ""
        if hot_start is not None:
            resolved_hot_start = ASSET_CACHE.materialize_path(hot_start)
            resolved_hot_start = tif_to_asc(resolved_hot_start)
            cfg["startfile"] = resolved_hot_start
        with open(par_path, mode="w") as f:
            for k, v in cfg.items():
                f.write(f"{k} {v}\n")
        return par_path


class SfincsWriter:
    def export(
        self,
        bcs: list[BoundaryConditionElement],
        terrain_path: str | Path,
        roughness_path: str | Path,
        domain: Domain,
        grid: GridProperties,
        epsg_code: int,
        run_config: RunConfig,
        output_dir: Path,
    ) -> None:
        raise NotImplementedError
        output_dir.mkdir(parents=True, exist_ok=True)
        resolution = _compute_resolution(domain, grid)
        msk = self._write_msk(output_dir / "sfincs.msk", grid, bcs)
        self._write_ind(output_dir / "sfincs.ind", msk)
        self._write_dep(output_dir / "sfincs.dep", terrain_path, msk)
        self._write_man(output_dir / "sfincs.man", roughness_path, msk)
        self._write_src(output_dir / "sfincs.src", bcs)
        self._write_dis(output_dir / "sfincs.dis", bcs, run_config, resolution)
        self._write_bnd(output_dir / "sfincs.bnd", bcs)
        self._write_bzs(output_dir / "sfincs.bzs", bcs, run_config)
        self._write_bdr(output_dir / "sfincs.bdr", bcs, domain)
        self._write_inp(
            output_dir / "sfincs.inp", domain, grid, epsg_code, run_config, resolution
        )

    def _write_msk(
        self,
        fn_msk: Path,
        grid: GridProperties,
        bcs: list[BoundaryConditionElement],
    ) -> np.ndarray:
        rows = grid.rows
        cols = grid.cols
        msk = np.full((rows, cols), 1, dtype=np.uint8)
        for bc in bcs:
            if bc.bc_type == "FREE":
                btype = 5
            elif bc.bc_type in ["TRANSFER", "HFIX"]:
                btype = 2
            else:
                continue
            msk[bc.x_ind, bc.y_ind] = btype
        msk = np.flipud(msk)
        self._write_binary_file(fn_msk, msk, np.ones_like(msk), "uint8")
        return msk

    def _write_ind(self, fn_ind: Path, msk: np.ndarray) -> None:
        # Lifted from hydromt: https://github.com/Deltares/hydromt_sfincs/blob/d8514d644f297b6b3982c249c3c233dfdf5076fb/hydromt_sfincs/utils.py#L82
        iok = np.where(np.transpose(msk) > 0)
        iok = (iok[1], iok[0])
        ind = np.ravel_multi_index(iok, msk.shape, order="F")
        indices_ = np.array(np.hstack([np.array(len(ind)), ind + 1]), dtype="u4")
        indices_.tofile(fn_ind)

    def _write_dep(self, fn: Path, terrain_path: str | Path, msk: np.ndarray) -> None:
        self._raster_2_bin(terrain_path, fn, msk)

    def _write_man(self, fn: Path, roughness_path: str | Path, msk: np.ndarray) -> None:
        self._raster_2_bin(roughness_path, fn, msk)

    def _write_src(self, fn: Path, bcs: list[BoundaryConditionElement]) -> None:
        with open(fn, mode="w") as f:
            for bc in bcs:
                if bc.bc_type == "QFIX":
                    f.write(f"{bc.x_coord} {bc.y_coord}\n")

    def _write_dis(
        self,
        fn: Path,
        bcs: list[BoundaryConditionElement],
        run_config: RunConfig,
        resolution: float,
    ) -> None:
        forcing_str = (
            " ".join(
                str(
                    float(bc.value) * resolution
                )  # TODO: switch so LISFLOOD divides instead
                for bc in bcs
                if bc.bc_type == "QFIX"
            )
            + "\n"
        )
        with open(fn, mode="w") as f:
            f.write("0.0 " + forcing_str)
            f.write(str(run_config.sim_time_seconds) + " " + forcing_str)

    def _write_bnd(self, fn: Path, bcs: list[BoundaryConditionElement]) -> None:
        with open(fn, mode="w") as f:
            for bc in bcs:
                if bc.bc_type in ["TRANSFER", "HFIX"]:
                    f.write(f"{int(bc.x_coord)} {int(bc.y_coord)}\n")

    def _write_bzs(
        self,
        fn: Path,
        bcs: list[BoundaryConditionElement],
        run_config: RunConfig,
    ) -> None:
        # SFINCS will error on negative WSE values
        forcing_str = (
            " ".join(
                str(max(float(bc.value), 0))
                for bc in bcs
                if bc.bc_type in ["TRANSFER", "HFIX"]
            )
            + "\n"
        )
        with open(fn, mode="w") as f:
            f.write("0.0 " + forcing_str)
            f.write(str(run_config.sim_time_seconds) + " " + forcing_str)

    def _write_bdr(
        self,
        fn: Path,
        bcs: list[BoundaryConditionElement],
        domain: Domain,
    ) -> None:
        free_bcs = [bc for bc in bcs if bc.bc_type == "FREE"]
        if not free_bcs:
            return
        xmin, ymin, xmax, ymax = domain.bbox
        ax, ay = free_bcs[0].x_coord, free_bcs[0].y_coord
        bx, by = free_bcs[-1].x_coord, free_bcs[-1].y_coord
        mx = (ax + bx) / 2.0
        my = (ay + by) / 2.0
        cx = (xmax + xmin) / 2
        cy = (ymax + ymin) / 2
        tmp_str = f"{round(mx, 1)} {round(my, 1)} {round(cx, 1)} {round(cy, 1)} {round(float(free_bcs[0].value), 6)} -1"
        with open(fn, mode="w") as f:
            f.write(tmp_str)

    def _write_inp(
        self,
        fn: Path,
        domain: Domain,
        grid: GridProperties,
        epsg_code: int,
        run_config: RunConfig,
        resolution: float,
    ) -> None:
        from datetime import datetime, timedelta

        xmin, ymin, _, _ = domain.bbox
        # SFINCS uses a fixed epoch of 2000-01-01 for time references
        tstop = datetime(2000, 1, 1) + timedelta(seconds=run_config.sim_time_seconds)
        cfg: dict[str, object] = {
            "x0": xmin,
            "y0": ymin,
            "mmax": grid.cols,
            "nmax": grid.rows,
            "dx": resolution,
            "dy": resolution,
            "epsg": epsg_code,
            "latitude": 0,
            "rotation": 0,
            "tref": "20000101 000000",
            "tstart": "20000101 000000",
            "tstop": tstop.strftime("%Y%m%d %H%M%S"),
            "depfile": "sfincs.dep",
            "manningfile": "sfincs.man",
            "mskfile": "sfincs.msk",
            "srcfile": "sfincs.src",
            "disfile": "sfincs.dis",
            "bndfile": "sfincs.bnd",
            "bzsfile": "sfincs.bzs",
            "bdrfile": "sfincs.bdr",
            "indexfile": "sfincs.ind",
            "dtout": run_config.save_interval_seconds,
            "inputformat": "bin",
            "crsgeo": 0,
        }
        with open(fn, mode="w") as f:
            for k, v in cfg.items():
                f.write(f"{k.ljust(16)}= {v}\n")

    def _raster_2_bin(
        self,
        raster_path: str | Path,
        out_path: Path,
        msk: np.ndarray,
    ) -> None:
        with rasterio.open(raster_path, mode="r") as src:
            data = src.read(1)
            data = np.flipud(data)
        self._write_binary_file(out_path, data, msk)

    def _write_binary_file(
        self,
        fn: Path,
        data: np.ndarray,
        msk: np.ndarray,
        dtype: str | np.dtype = "f4",
    ) -> None:
        # Lifted from hydromt: https://github.com/Deltares/hydromt_sfincs/blob/d8514d644f297b6b3982c249c3c233dfdf5076fb/hydromt_sfincs/utils.py#L119
        data_out = np.asarray(data.transpose()[msk.transpose() > 0], dtype=dtype)
        data_out.tofile(fn)


### METHODS ###


def write_model_files(
    run_scenario_inputs: RunScenarioInputs, working_dir: Path
) -> Path:
    if SCENARIO_SOLVER == SupportedSolver.LISFLOOD:
        return LisfloodWriter().export(run_scenario_inputs, working_dir)
    elif SCENARIO_SOLVER == SupportedSolver.SFINCS:
        raise NotImplementedError(
            "Tried to generate model files for SFINCS, but this solver is not yet supported."
        )
    else:
        return Path()


def process_bc_lines(
    boundary_conditions: list[BoundaryCondition],
    domain: Domain,
    grid_properties: GridProperties,
) -> list[BoundaryConditionElement]:
    bcs = []
    for i in boundary_conditions:
        bcs.extend(process_bc_line(i, domain, grid_properties))
    return bcs


def process_bc_line(
    boundary_condition: BoundaryCondition,
    domain: Domain,
    grid_properties: GridProperties,
) -> list[BoundaryConditionElement]:
    """Convert a geometry and boundary condition type into a list of BoundaryConditionElements."""
    # Get geometry
    resolved_geom = ASSET_CACHE.materialize_path(boundary_condition.vector)
    bc_geom = gpd.read_file(resolved_geom).geometry.iloc[0]
    if isinstance(boundary_condition, TransferBC) and isinstance(
        bc_geom, (Polygon, MultiPolygon)
    ):
        raise ValueError(
            "TransferBC boundary conditions cannot use Polygon geometries; "
            "use LineString, MultiLineString, or Point instead."
        )

    transform = _build_transform(domain, grid_properties)
    pts = geometry_to_bc_points(bc_geom, grid_properties, transform, domain)

    if not pts:
        return []

    resolution = _compute_resolution(domain, grid_properties)

    if isinstance(boundary_condition, QFixBC):
        q_per_cell = float(boundary_condition.value) / (resolution * len(pts))
        tagged = [[*pt, "QFIX", q_per_cell] for pt in pts]
    elif isinstance(boundary_condition, TransferBC):
        tagged = process_transfer_bc_line(boundary_condition, pts)
        if len(tagged) == 0:
            raise NonIntersectingKWSELine()
    else:
        tagged = [
            [*pt, boundary_condition.bc_type, boundary_condition.value] for pt in pts
        ]

    out = []
    for item in tagged:
        if item[0] == "P":
            _row, _col = rasterio.transform.rowcol(transform, item[1], item[2])
            row, col = int(_row), int(_col)
        else:
            row, col = 0, 0  # cardinal BCs represent extents, not single cells
        out.append(
            BoundaryConditionElement(
                element_type=item[0],
                bc_type=item[3],
                value=item[4],
                x_coord=item[1],
                y_coord=item[2],
                x_ind=row,
                y_ind=col,
            )
        )

    return out


def process_transfer_bc_line(
    bc: TransferBC, pts: list[list[str | float]]
) -> list[tuple[str, float, float, Literal["HFIX"], float]]:
    # Get WSE data
    resolved_depth = ASSET_CACHE.materialize_path(bc.transfer_depths)
    resolved_el = ASSET_CACHE.materialize_path(bc.transfer_els)
    depth = Raster(resolved_depth).data
    el = Raster(resolved_el).data
    wse = depth + el

    # Build transform
    transform = _build_transform(bc.domain, bc.grid_properties)

    # Iterate over cells
    bc_pts = []
    for _, x, y in pts:
        _row, _col = rasterio.transform.rowcol(transform, x, y)
        row, col = int(_row), int(_col)
        val = wse[row, col]
        if val > 0:
            bc_pts.append(["P", x, y, "HFIX", val])
    return bc_pts


def geometry_to_bc_points(
    geometry: BaseGeometry,
    grid_properties: GridProperties,
    transform: Affine,
    domain: Domain,
) -> list[list[str | float]]:
    """Dispatch a geometry to the appropriate rasterization function and return BC point lists."""
    if isinstance(geometry, Point):
        return [["P", geometry.x, geometry.y]]
    elif isinstance(geometry, MultiLineString):
        geometry = linemerge(geometry)
        pts = rasterize_geometry(
            geometry, grid_properties.rows, grid_properties.cols, transform
        )
        return [["P", pt[0], pt[1]] for pt in pts]
    elif isinstance(geometry, LineString):
        pts = rasterize_geometry(
            geometry, grid_properties.rows, grid_properties.cols, transform
        )
        return [["P", pt[0], pt[1]] for pt in pts]
    elif isinstance(geometry, Polygon):
        return _poly_to_edge_bc_points(geometry, domain)
    elif isinstance(geometry, MultiPolygon):
        merged = unary_union(geometry)
        if not isinstance(merged, Polygon):
            raise ValueError(
                f"geometry_to_bc_points: MultiPolygon union produced a {type(merged).__name__}, not a Polygon; "
                "parts are likely non-contiguous or non-overlapping."
            )
        return _poly_to_edge_bc_points(merged, domain)
    else:
        raise ValueError(
            f"Unsupported geometry type '{geometry.geom_type}'; "
            "expected Point, LineString, MultiLineString, Polygon, or MultiPolygon."
        )


def _poly_to_edge_bc_points(
    poly: Polygon,
    domain: Domain,
) -> list[list[str | float]]:
    """Intersect a polygon with each domain edge and return N/S/E/W cardinal BC points."""
    xmin, ymin, xmax, ymax = domain.bbox
    edges = {
        "N": LineString([(xmin, ymax), (xmax, ymax)]),
        "S": LineString([(xmin, ymin), (xmax, ymin)]),
        "W": LineString([(xmin, ymin), (xmin, ymax)]),
        "E": LineString([(xmax, ymin), (xmax, ymax)]),
    }
    pts = []
    for cardinal, edge in edges.items():
        clipped = edge.intersection(poly)
        if not clipped.is_empty:
            bounds = clipped.bounds
            if cardinal in ("N", "S"):
                pts.append([cardinal, bounds[0], bounds[2]])  # xmin, xmax
            else:
                pts.append([cardinal, bounds[1], bounds[3]])  # ymin, ymax
    return pts


def _build_transform(domain: Domain, grid_properties: GridProperties) -> Affine:
    """Build an affine transform from domain bounds and grid dimensions."""
    xmin, ymin, xmax, ymax = domain.bbox
    return rasterio.transform.from_bounds(
        xmin, ymin, xmax, ymax, grid_properties.cols, grid_properties.rows
    )


def _compute_resolution(domain: Domain, grid_properties: GridProperties) -> float:
    """Compute the grid cell resolution in the x-direction."""
    xmin, _, xmax, _ = domain.bbox
    return (xmax - xmin) / grid_properties.cols
