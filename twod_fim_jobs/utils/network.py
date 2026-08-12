"""Network-modification algorithms for the modify_network job.

Pure transforms over GeoDataFrames plus plain numpy topology arrays — no
manifest or storage concerns (those live in jobs/modify_network.py). Every
function is deterministic: iteration orders are sorted, split ids derive from
the reach they came from, and finalize_network() sorts the output canonically,
so identical inputs produce byte-identical artifacts.

Reach ids are TEXT, not integers. A pass-through split names its new piece
after its parent (``8`` -> ``8`` and ``8_1``), which keeps the lineage legible
and makes collision with a source id impossible by construction — including
with reaches the stream-order filter removed, which a numeric high-water mark
would have to be told about.

Assumptions, stated once:
- Flowpath geometries are digitized upstream -> downstream (first vertex is
  the upstream end), matching NHF convention.
- The network CRS is projected with meter units; lengths and the negative
  lake buffer depend on it (checked at load).

Counter discipline (see modify_network_specs.md Metrics/Accounting): every
removal is counted in exactly one branch, so the reconciliation identity
holds by construction. Trims, strands, and splits keep their rows.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
import shapely
from shapely import Point
from shapely.ops import substring

from twod_fim_jobs.consts import (
    COAST_ID_FIELD,
    COAST_TO_ID_FIELD,
    DA_FIELD,
    FLOWPATHS_LAYER,
    FP_ID_FIELD,
    FP_TO_ID_FIELD,
    IS_HEADWATER_FIELD,
    IS_TERMINAL_FIELD,
    IS_TRIMMED_FIELD,
    LAKE_ID_FIELD,
    LAKE_INLET_FIELD,
    LAKE_OUTLET_FIELD,
    LAKE_TO_ID_FIELD,
    LENGTH_KM_FIELD,
    OUTPUT_COLUMNS,
    REACH_ID_FIELD,
    REACH_TO_ID_FIELD,
    STREAM_ORDER_FIELD,
    TERMINAL_REASON_COAST,
    TERMINAL_REASON_FIELD,
    TERMINAL_REASON_LAKE,
    TERMINAL_REASON_OUTLET,
)
from twod_fim_jobs.exceptions import DatasetUnavailableError

logger = logging.getLogger(__name__)

_FLAG_FIELDS = (
    IS_HEADWATER_FIELD,
    IS_TERMINAL_FIELD,
    LAKE_INLET_FIELD,
    LAKE_OUTLET_FIELD,
    IS_TRIMMED_FIELD,
)
_REQUIRED_SOURCE_FIELDS = (
    FP_ID_FIELD,
    FP_TO_ID_FIELD,
    DA_FIELD,
    LENGTH_KM_FIELD,
)


@dataclass
class NetworkCounters:
    """Accumulated per-branch counters; field names match models.Properties."""

    n_reaches_input: int | None = None
    n_reaches_below_stream_order_removed: int | None = None
    n_reaches_encompassed_removed_lake: int | None = None
    n_reaches_encompassed_removed_coastal: int | None = None
    n_reaches_trimmed_inlet_lake: int | None = None
    n_reaches_trimmed_outlet_lake: int | None = None
    n_reaches_trimmed_inlet_coastal: int | None = None
    n_reaches_dropped_coastal_cascade: int | None = None
    n_reaches_stranded_coastal: int | None = None
    n_reaches_split_passthrough_lake: int | None = None
    n_reaches_trimmed_between_lakes: int | None = None
    n_reaches_orphaned_lake: int | None = None
    n_reaches_merged: int | None = None
    n_reaches_output: int | None = None
    n_headwater_reaches: int | None = None
    n_terminal_reaches: int | None = None


### LOADING ###


def load_reach_network(
    path: str, stream_order_filter_threshold: int | None
) -> tuple[gpd.GeoDataFrame, NetworkCounters]:
    """Load the flowpaths layer, filtering by stream order at read time.

    Reaches below the threshold never materialize (pushed down to GDAL).
    When the threshold is None no filter is applied and the whole network
    loads; n_reaches_below_stream_order_removed stays None.
    """
    counters = NetworkCounters()
    try:
        n_input = int(pyogrio.read_info(path, layer=FLOWPATHS_LAYER)["features"])
        where = None
        if stream_order_filter_threshold is not None:
            where = f"{STREAM_ORDER_FIELD} >= {int(stream_order_filter_threshold)}"
        gdf = gpd.read_file(path, layer=FLOWPATHS_LAYER, where=where)
        if n_input < 0:  # driver without a fast feature count
            n_input = (
                len(gdf)
                if where is None
                else len(
                    pyogrio.read_dataframe(
                        path,
                        layer=FLOWPATHS_LAYER,
                        columns=[FP_ID_FIELD],
                        read_geometry=False,
                    )
                )
            )
    except DatasetUnavailableError:
        raise
    except Exception as exc:
        raise DatasetUnavailableError(
            f"Cannot read reach network layer '{FLOWPATHS_LAYER}' at {path}: {exc}"
        ) from exc

    missing = [f for f in _REQUIRED_SOURCE_FIELDS if f not in gdf.columns]
    if stream_order_filter_threshold is not None and STREAM_ORDER_FIELD not in gdf:
        missing.append(STREAM_ORDER_FIELD)
    if missing:
        raise DatasetUnavailableError(
            f"Reach network layer '{FLOWPATHS_LAYER}' at {path} is missing required "
            f"field(s): {', '.join(missing)}. Found: {', '.join(gdf.columns)}"
        )

    if gdf.crs is None or not gdf.crs.is_projected:
        raise ValueError(
            "Reach network CRS must be projected with meter units; lengths and "
            f"the negative lake buffer depend on it (got {gdf.crs})."
        )

    counters.n_reaches_input = n_input
    if stream_order_filter_threshold is not None:
        counters.n_reaches_below_stream_order_removed = n_input - len(gdf)

    return _init_columns(gdf), counters


def load_vector_layer(path: str, layer: str, target_crs) -> gpd.GeoDataFrame:
    """Load a lakes/coastal vector layer and reproject to the network CRS."""
    try:
        gdf = gpd.read_file(path, layer=layer)
    except Exception as exc:
        raise DatasetUnavailableError(
            f"Cannot read layer '{layer}' at {path}: {exc}"
        ) from exc
    return gdf.to_crs(target_crs).reset_index(drop=True)


def _init_columns(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Set up working topology and the contract's output tag columns."""
    multi = gdf.geom_type == "MultiLineString"
    if multi.any():
        merged = shapely.line_merge(gdf.geometry.to_numpy()[multi.to_numpy()])
        gdf.loc[multi, gdf.geometry.name] = merged
        still = gdf.geom_type == "MultiLineString"
        if still.any():
            logger.warning(
                "%d reaches remain MultiLineString after line_merge; endpoint "
                "classification will skip them",
                int(still.sum()),
            )

    gdf[REACH_ID_FIELD] = _as_id(gdf[FP_ID_FIELD])
    gdf[REACH_TO_ID_FIELD] = _as_id(gdf[FP_TO_ID_FIELD])
    if gdf[REACH_ID_FIELD].duplicated().any():
        dupes = gdf.loc[gdf[REACH_ID_FIELD].duplicated(), REACH_ID_FIELD].tolist()
        raise DatasetUnavailableError(
            f"{FP_ID_FIELD} must be unique; repeated: {sorted(set(dupes))[:10]}"
        )
    for field in _FLAG_FIELDS:
        gdf[field] = False
    gdf[TERMINAL_REASON_FIELD] = pd.Series(pd.NA, index=gdf.index, dtype="string")
    gdf[LAKE_TO_ID_FIELD] = pd.Series(pd.NA, index=gdf.index, dtype="string")
    gdf[COAST_TO_ID_FIELD] = pd.Series(pd.NA, index=gdf.index, dtype="string")
    return gdf.reset_index(drop=True)


def _as_id(series: pd.Series) -> pd.Series:
    """Render source ids as text without a float detour.

    A nullable integer column read from GPKG can arrive as float64, where a
    plain astype(str) would render 12 as '12.0' and silently break every
    downstream join.
    """
    if pd.api.types.is_float_dtype(series):
        series = series.astype("Int64")
    return series.astype("string")


### TERMINAL / HEADWATER TAGGING ###


def tag_terminal_reaches(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Null downstream pointer -> terminal outlet (spec step 2)."""
    mask = gdf[REACH_TO_ID_FIELD].isna()
    gdf.loc[mask, IS_TERMINAL_FIELD] = True
    gdf.loc[mask, TERMINAL_REASON_FIELD] = TERMINAL_REASON_OUTLET
    return gdf


def tag_headwater_reaches(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """A headwater is a reach that is no other reach's downstream (spec step 3).

    Evaluated after the stream-order filter, so reaches whose feeders were
    filtered out become apparent headwaters by design.
    """
    mask = ~gdf[REACH_ID_FIELD].isin(gdf[REACH_TO_ID_FIELD].dropna())
    gdf.loc[mask, IS_HEADWATER_FIELD] = True
    return gdf


### LAKES PREPROCESSING ###


def prepare_lakes(
    lakes_gdf: gpd.GeoDataFrame,
    lake_area_threshold_sqkm: float,
    negative_lake_buffer_meters: float,
) -> gpd.GeoDataFrame:
    """Filter lakes to the area threshold, then shrink to the dead-pool extent.

    Spec order matters: filter FIRST on raw polygon area, then buffer inward
    (DR-034 ALT-A). Polygons that vanish under the negative buffer are
    dropped. lake_id is carried through so reaches can record which lake they
    meet; explode() preserves it, so a multipart lake's parts share an id.
    """
    g = lakes_gdf[lakes_gdf.geometry.notna() & ~lakes_gdf.geometry.is_empty]
    g = g[g.geometry.area > lake_area_threshold_sqkm * 1e6]
    g = g.assign(**{g.geometry.name: g.geometry.buffer(-negative_lake_buffer_meters)})
    g = g[~g.geometry.is_empty]
    g = g.explode(ignore_index=True)
    g = g[g.geometry.area > 0]
    return g.reset_index(drop=True)


### SHARED GEOMETRY HELPERS ###


def _waterbody_ids(polys: gpd.GeoDataFrame, id_field: str) -> np.ndarray:
    """Positional lookup of a waterbody layer's id column, as text.

    Falls back to the positional index when the layer has no id column, so a
    layer without one still yields a stable, if less meaningful, reference.
    """
    if id_field in polys.columns:
        return _as_id(polys[id_field]).to_numpy(dtype=object)
    logger.warning(
        "Waterbody layer has no '%s' column; recording positional index instead",
        id_field,
    )
    return np.array([str(i) for i in range(len(polys))], dtype=object)


def _topology(gdf: gpd.GeoDataFrame) -> tuple[np.ndarray, np.ndarray]:
    """(ids, ds_pos): downstream pointer as positional index, -1 for none.

    Always derived from the LIVE reach_to_id column — never fp_to_id — so
    waterbody edits (nulled pointers, splits, deletions) are respected.
    """
    ids = gdf[REACH_ID_FIELD].to_numpy(dtype=object)
    ds = gdf[REACH_TO_ID_FIELD].fillna("").to_numpy(dtype=object)
    ds_pos = pd.Index(ids).get_indexer(ds)
    return ids, ds_pos


def _downstream_closure(ds_pos: np.ndarray, seeds: np.ndarray) -> np.ndarray:
    """Boolean mask of positions reachable downstream from seeds (inclusive).

    Out-degree <= 1 makes this a pointer walk with a visited set: amortized
    O(n) even when many seeds share a downstream trunk.
    """
    visited = np.zeros(len(ds_pos), dtype=bool)
    for s in np.sort(seeds):
        cur = int(s)
        while cur >= 0 and not visited[cur]:
            visited[cur] = True
            cur = int(ds_pos[cur])
    return visited


def _classify_crossings(gdf: gpd.GeoDataFrame, polys: gpd.GeoDataFrame):
    """Spatially classify reaches against waterbody polygons.

    Returns (within_pos, crossing_pos, up_poly, dn_poly, poly_map): positions
    of fully-encompassed reaches, positions of boundary-crossing reaches, the
    polygon position containing each crossing reach's upstream/downstream
    endpoint (-1 when outside every polygon), and crossing position ->
    intersecting polygon positions for boundary construction.
    """
    geom = gdf.geometry.name
    pairs = gpd.sjoin(
        gdf[[geom]], polys[[polys.geometry.name]], predicate="intersects", how="inner"
    )
    within = gpd.sjoin(
        gdf[[geom]], polys[[polys.geometry.name]], predicate="within", how="inner"
    )
    within_pos = np.sort(within.index.unique().to_numpy())
    crossing_pos = np.sort(
        pairs.index.unique().difference(within.index.unique()).to_numpy()
    )

    poly_map: dict[int, list[int]] = {
        int(k): sorted(int(v) for v in grp)
        for k, grp in pairs.loc[pairs.index.isin(crossing_pos), "index_right"].groupby(
            level=0
        )
    }

    lines = gdf.geometry.to_numpy()[crossing_pos]
    up_poly = _containing_polygon(shapely.get_point(lines, 0), polys, gdf.crs)
    dn_poly = _containing_polygon(shapely.get_point(lines, -1), polys, gdf.crs)
    return within_pos, crossing_pos, up_poly, dn_poly, poly_map


def _containing_polygon(points, polys: gpd.GeoDataFrame, crs) -> np.ndarray:
    """Position of the polygon containing each point, or -1 if none does."""
    out = np.full(len(points), -1, dtype=int)
    pts = gpd.GeoDataFrame(geometry=points, crs=crs)
    hit = gpd.sjoin(
        pts, polys[[polys.geometry.name]], predicate="within", how="inner"
    )
    # A point inside overlapping polygons keeps the lowest position, so the
    # choice is deterministic rather than sjoin-order dependent.
    for pos, poly_pos in hit["index_right"].groupby(level=0).min().items():
        out[int(pos)] = int(poly_pos)
    return out


def _crossing_distances(line, boundary) -> np.ndarray:
    """Sorted projected distances of every line/boundary crossing.

    Robust to degenerate intersections: a collinear overlap contributes its
    endpoints' distances instead of raising.
    """
    inter = line.intersection(boundary)
    if inter.is_empty:
        return np.array([])
    coords = shapely.get_coordinates(inter)
    return np.unique([line.project(Point(*c)) for c in coords])


def _boundary_for(polys: gpd.GeoDataFrame, poly_positions: list[int]):
    return shapely.union_all(polys.geometry.boundary.to_numpy()[poly_positions])


### COASTAL PASS (spec step 4) ###


def apply_coastal(
    gdf: gpd.GeoDataFrame, coastal_gdf: gpd.GeoDataFrame, counters: NetworkCounters
) -> tuple[gpd.GeoDataFrame, set[str]]:
    """Coastal classification, cascade removal, and the stranded sweep.

    Cases (spec step 4): fully inside -> dropped with everything downstream;
    downstream end inside -> trimmed to the upstream portion and made
    terminal ('coast'), everything downstream dropped. Coastal has no
    outlet/pass-through case — such reaches are left untouched (logged).
    Trimmed reaches record the polygon they met in coast_to_id; stranded
    reaches leave it null, since they never touched the coast layer.
    """
    gdf = gdf.reset_index(drop=True)
    within_pos, crossing_pos, up_poly, dn_poly, poly_map = _classify_crossings(
        gdf, coastal_gdf
    )
    coast_ids = _waterbody_ids(coastal_gdf, COAST_ID_FIELD)

    is_inlet = (dn_poly >= 0) & (up_poly < 0)
    inlet_pos = crossing_pos[is_inlet]
    inlet_poly = dn_poly[is_inlet]
    other = int(len(crossing_pos) - len(inlet_pos))
    if other:
        logger.info(
            "%d coastal-crossing reaches are not inlet-classified (coastal has "
            "no outlet/pass-through case); left untouched",
            other,
        )

    # Trim inlets first so a degenerate trim can escalate to encompassed
    # before any counting happens.
    encompassed = np.zeros(len(gdf), dtype=bool)
    encompassed[within_pos] = True
    trimmed_pos: list[int] = []
    for p, poly_pos in sorted(zip(inlet_pos.tolist(), inlet_poly.tolist())):
        line = gdf.geometry.iloc[p]
        dists = _crossing_distances(line, _boundary_for(coastal_gdf, poly_map[p]))
        if len(dists) == 0 or dists.min() <= 0 or dists.min() >= line.length:
            encompassed[p] = True  # effectively fully inside
            continue
        cut = float(dists.min())
        gdf.loc[p, gdf.geometry.name] = substring(line, 0, cut)
        gdf.loc[p, LENGTH_KM_FIELD] = cut / 1000.0
        gdf.loc[p, IS_TRIMMED_FIELD] = True
        gdf.loc[p, COAST_TO_ID_FIELD] = coast_ids[poly_pos]
        trimmed_pos.append(p)

    # Cascade: everything strictly downstream of an encompassed or trimmed
    # reach is removed. Deletion wins over trim if a trimmed reach is itself
    # downstream of another break (counted once, as cascade).
    ids, ds_pos = _topology(gdf)
    flagged = np.union1d(np.flatnonzero(encompassed), np.array(trimmed_pos, dtype=int))
    seeds = ds_pos[flagged.astype(int)] if len(flagged) else np.array([], dtype=int)
    closure = _downstream_closure(ds_pos, seeds[seeds >= 0])
    deletion = encompassed | closure

    counters.n_reaches_encompassed_removed_coastal = int(encompassed.sum())
    counters.n_reaches_dropped_coastal_cascade = int((closure & ~encompassed).sum())

    surviving_trims = [p for p in trimmed_pos if not deletion[p]]
    gdf.loc[surviving_trims, IS_TERMINAL_FIELD] = True
    gdf.loc[surviving_trims, TERMINAL_REASON_FIELD] = TERMINAL_REASON_COAST
    gdf.loc[surviving_trims, REACH_TO_ID_FIELD] = pd.NA
    counters.n_reaches_trimmed_inlet_coastal = len(surviving_trims)

    touched = {str(i) for i in ids[surviving_trims]}
    gdf = gdf.loc[~deletion].reset_index(drop=True)

    # Stranded sweep: tributaries that flowed into a cascade-deleted reach
    # without themselves intersecting the coast layer. Made terminal in
    # place, geometry untouched, coast_to_id left null. Lakes need no
    # equivalent — they have no cascade, and anything pointing into a
    # lake-encompassed reach has its own downstream end inside the lake, so
    # the inlet rule nulls it.
    stranded = gdf[REACH_TO_ID_FIELD].notna() & ~gdf[REACH_TO_ID_FIELD].isin(
        gdf[REACH_ID_FIELD]
    )
    gdf.loc[stranded, IS_TERMINAL_FIELD] = True
    gdf.loc[stranded, TERMINAL_REASON_FIELD] = TERMINAL_REASON_COAST
    gdf.loc[stranded, REACH_TO_ID_FIELD] = pd.NA
    counters.n_reaches_stranded_coastal = int(stranded.sum())

    return gdf, touched


### LAKE PASS (spec step 5) ###


def apply_lakes(
    gdf: gpd.GeoDataFrame, lakes_gdf: gpd.GeoDataFrame, counters: NetworkCounters
) -> tuple[gpd.GeoDataFrame, set[str]]:
    """Lake classification: encompassed / inlet / outlet / pass-through split.

    Pass-through splits keep the original reach_id on the upstream/inlet
    piece and name the downstream/outlet piece after it (``8`` -> ``8_1``),
    so lineage is readable and no source id can be reused. Every reach that
    meets a lake records it in lake_to_id.

    Finishes with an orphan sweep — see the comment at the end — which drops
    reaches left connected to nothing once their neighbors were removed.
    """
    gdf = gdf.reset_index(drop=True)
    within_pos, crossing_pos, up_poly, dn_poly, poly_map = _classify_crossings(
        gdf, lakes_gdf
    )
    lake_ids = _waterbody_ids(lakes_gdf, LAKE_ID_FIELD)

    encompassed = np.zeros(len(gdf), dtype=bool)
    encompassed[within_pos] = True

    # Both ends inside the water is NOT an inlet. A reach that starts and
    # ends in the same lake lies in that lake; it is only excluded from
    # `within` because it crosses an island, or a gap between the lake's
    # exploded parts. Treating it as an inlet trimmed it to the stub between
    # its start and the island shore, and left that stub attached to nothing
    # once the water either side was encompassed. Classify it as encompassed,
    # which is what it is.
    both_ends_in = (up_poly >= 0) & (dn_poly >= 0)
    is_inlet = (dn_poly >= 0) & (up_poly < 0)
    is_outlet = (up_poly >= 0) & (dn_poly < 0)
    is_pass = (up_poly < 0) & (dn_poly < 0)
    # Same lake at both ends means the reach lies in that lake. Different
    # lakes at each end means a real channel running between two waterbodies,
    # with dry land in the middle: keep that middle. Compare lake_id, not
    # polygon position — prepare_lakes explodes multipart lakes, so one lake
    # can be several polygons and an island crossing lands in two of them.
    same_lake_mask = np.zeros(len(crossing_pos), dtype=bool)
    if both_ends_in.any():
        idx = np.flatnonzero(both_ends_in)
        same = lake_ids[up_poly[idx]] == lake_ids[dn_poly[idx]]
        same_lake_mask[idx[same]] = True
        encompassed[crossing_pos[idx[same]]] = True
        if int(same.sum()):
            logger.info(
                "%d reaches start and end in the same lake (crossing an "
                "island or a gap between its parts); dropped as encompassed",
                int(same.sum()),
            )
    between_lakes = sorted(
        (int(crossing_pos[i]), int(up_poly[i]), int(dn_poly[i]))
        for i in np.flatnonzero(both_ends_in & ~same_lake_mask)
    )
    inlet = sorted(zip(crossing_pos[is_inlet].tolist(), dn_poly[is_inlet].tolist()))
    outlet = sorted(zip(crossing_pos[is_outlet].tolist(), up_poly[is_outlet].tolist()))
    passthrough = sorted(crossing_pos[is_pass].tolist())

    n_inlet = n_outlet = n_split = n_between = 0
    new_rows: list[dict] = []
    geom = gdf.geometry.name

    for p, poly_pos in inlet:
        line = gdf.geometry.iloc[p]
        dists = _crossing_distances(line, _boundary_for(lakes_gdf, poly_map[p]))
        if len(dists) == 0 or dists.min() <= 0 or dists.min() >= line.length:
            encompassed[p] = True
            continue
        cut = float(dists.min())
        gdf.loc[p, geom] = substring(line, 0, cut)
        gdf.loc[p, LENGTH_KM_FIELD] = cut / 1000.0
        gdf.loc[p, LAKE_INLET_FIELD] = True
        gdf.loc[p, IS_TERMINAL_FIELD] = True
        gdf.loc[p, TERMINAL_REASON_FIELD] = TERMINAL_REASON_LAKE
        gdf.loc[p, REACH_TO_ID_FIELD] = pd.NA
        gdf.loc[p, IS_TRIMMED_FIELD] = True
        gdf.loc[p, LAKE_TO_ID_FIELD] = lake_ids[poly_pos]
        n_inlet += 1

    for p, poly_pos in outlet:
        line = gdf.geometry.iloc[p]
        dists = _crossing_distances(line, _boundary_for(lakes_gdf, poly_map[p]))
        if len(dists) == 0 or dists.max() <= 0 or dists.max() >= line.length:
            encompassed[p] = True
            continue
        cut = float(dists.max())
        gdf.loc[p, geom] = substring(line, cut, line.length)
        gdf.loc[p, LENGTH_KM_FIELD] = (line.length - cut) / 1000.0
        gdf.loc[p, LAKE_OUTLET_FIELD] = True
        gdf.loc[p, IS_HEADWATER_FIELD] = True
        gdf.loc[p, IS_TRIMMED_FIELD] = True
        gdf.loc[p, LAKE_TO_ID_FIELD] = lake_ids[poly_pos]
        n_outlet += 1

    # Between two lakes: the inverse of a pass-through split. Both ends are
    # inside water, so the pieces to discard are the two ends and the piece to
    # keep is the middle. The survivor emerges from one lake and enters the
    # other, so it is a headwater and a terminal at once.
    for p, up_pos, dn_pos in between_lakes:
        line = gdf.geometry.iloc[p]
        dists = _crossing_distances(line, _boundary_for(lakes_gdf, poly_map[p]))
        dists = dists[(dists > 0) & (dists < line.length)]
        if len(dists) < 2:
            encompassed[p] = True  # no dry middle to keep
            continue
        d_exit, d_enter = float(dists.min()), float(dists.max())
        gdf.loc[p, geom] = substring(line, d_exit, d_enter)
        gdf.loc[p, LENGTH_KM_FIELD] = (d_enter - d_exit) / 1000.0
        gdf.loc[p, LAKE_OUTLET_FIELD] = True
        gdf.loc[p, LAKE_INLET_FIELD] = True
        gdf.loc[p, IS_HEADWATER_FIELD] = True
        gdf.loc[p, IS_TERMINAL_FIELD] = True
        gdf.loc[p, TERMINAL_REASON_FIELD] = TERMINAL_REASON_LAKE
        gdf.loc[p, REACH_TO_ID_FIELD] = pd.NA
        gdf.loc[p, IS_TRIMMED_FIELD] = True
        # lake_to_id names the lake the reach flows INTO, matching the column.
        # The upstream lake is not recorded; see the spec's Open Questions.
        gdf.loc[p, LAKE_TO_ID_FIELD] = lake_ids[dn_pos]
        n_between += 1

    for p in passthrough:
        line = gdf.geometry.iloc[p]
        dists = _crossing_distances(line, _boundary_for(lakes_gdf, poly_map[p]))
        dists = dists[(dists > 0) & (dists < line.length)]
        if len(dists) < 2:
            logger.debug(
                "reach %s: tangent lake contact, left untouched",
                gdf[REACH_ID_FIELD].iloc[p],
            )
            continue
        d_first, d_last = float(dists.min()), float(dists.max())
        parent = str(gdf[REACH_ID_FIELD].iloc[p])
        lake_ref = lake_ids[poly_map[p][0]]

        # Downstream/outlet piece: a new reach named after its parent, which
        # inherits the original's downstream connectivity and terminal state.
        outlet_row = gdf.iloc[p].to_dict()
        outlet_row[geom] = substring(line, d_last, line.length)
        outlet_row[REACH_ID_FIELD] = f"{parent}_1"
        outlet_row[LENGTH_KM_FIELD] = (line.length - d_last) / 1000.0
        outlet_row[LAKE_OUTLET_FIELD] = True
        outlet_row[LAKE_INLET_FIELD] = False
        outlet_row[IS_HEADWATER_FIELD] = True
        outlet_row[IS_TRIMMED_FIELD] = True
        outlet_row[LAKE_TO_ID_FIELD] = lake_ref
        new_rows.append(outlet_row)

        # Upstream/inlet piece keeps the original reach_id, so upstream
        # neighbors' pointers stay valid.
        gdf.loc[p, geom] = substring(line, 0, d_first)
        gdf.loc[p, LENGTH_KM_FIELD] = d_first / 1000.0
        gdf.loc[p, LAKE_INLET_FIELD] = True
        gdf.loc[p, IS_TERMINAL_FIELD] = True
        gdf.loc[p, TERMINAL_REASON_FIELD] = TERMINAL_REASON_LAKE
        gdf.loc[p, REACH_TO_ID_FIELD] = pd.NA
        gdf.loc[p, IS_TRIMMED_FIELD] = True
        gdf.loc[p, LAKE_TO_ID_FIELD] = lake_ref
        n_split += 1

    all_ids = gdf[REACH_ID_FIELD].to_numpy(dtype=object)
    touched = {str(i) for i in all_ids[encompassed]}
    touched |= {
        str(all_ids[p])
        for p in (
            *[i for i, _ in inlet],
            *[i for i, _ in outlet],
            *[i for i, _, _ in between_lakes],
            *passthrough,
        )
        if not encompassed[p]
    }
    touched |= {str(r[REACH_ID_FIELD]) for r in new_rows}

    counters.n_reaches_encompassed_removed_lake = int(encompassed.sum())
    counters.n_reaches_trimmed_inlet_lake = n_inlet
    counters.n_reaches_trimmed_outlet_lake = n_outlet
    counters.n_reaches_split_passthrough_lake = n_split
    counters.n_reaches_trimmed_between_lakes = n_between

    gdf = gdf.loc[~encompassed]
    if new_rows:
        additions = gpd.GeoDataFrame(new_rows, geometry=geom, crs=gdf.crs)
        gdf = pd.concat([gdf, additions], ignore_index=True)
    gdf = _normalize_dtypes(gdf.reset_index(drop=True))

    # Orphan sweep. A reach can pass all four cases above and still be left
    # connected to nothing, when lake removal takes its upstream AND its
    # downstream neighbor. The shape that produces it is a reach crossing an
    # island inside a lake: the upstream end sits outside the polygon (on the
    # island) so the reach classifies as an inlet and is trimmed to the island
    # width, while the water on both sides is encompassed and dropped. What
    # survives is a stub the width of the island, attached to nothing.
    #
    # is_headwater is the discriminator, and needs no special-casing: it is
    # False only for reaches that HAD an upstream neighbor at step 3. Genuine
    # one-reach watersheds draining into a lake are headwaters from step 3,
    # and lake outlets are marked headwater by the outlet rule above, so
    # neither is ever swept.
    #
    # One pass suffices: an orphan has nothing pointing at it and points at
    # nothing, so removing it cannot orphan anything else.
    has_upstream = gdf[REACH_ID_FIELD].isin(gdf[REACH_TO_ID_FIELD].dropna())
    orphaned = (
        ~has_upstream & gdf[REACH_TO_ID_FIELD].isna() & ~gdf[IS_HEADWATER_FIELD]
    )
    counters.n_reaches_orphaned_lake = int(orphaned.sum())
    if orphaned.any():
        logger.info(
            "Dropped %d reaches orphaned by lake removal (no upstream, no "
            "downstream, not an original headwater)",
            int(orphaned.sum()),
        )
    gdf = gdf.loc[~orphaned].reset_index(drop=True)
    return gdf, touched


### MERGE (spec step 7) ###


def merge_short_reaches(
    gdf: gpd.GeoDataFrame,
    drainage_area_threshold_percent: float,
    min_length_threshold_km: float,
    counters: NetworkCounters,
) -> gpd.GeoDataFrame:
    """Chain-merge short reaches walking upstream from each chain start.

    Spec step 7: starting downstream, absorb the upstream neighbor while (a)
    the chain is still SHORTER than min_length_threshold_km, (b) the
    neighbor's drainage-area difference from the CHAIN START is under the
    threshold, and (c) the current reach has exactly one upstream neighbor
    (junctions never merge).

    The length threshold is a floor, not a ceiling. Short reaches are the
    problem being solved, so merging continues until the chain is long enough
    to be worth modeling and then stops. Consequences: no output reach is
    shorter than the threshold unless topology or drainage area prevented it,
    and a merged chain cannot exceed the threshold by more than the length of
    the single reach that crossed it.

    Drainage area means total_da_sqkm, the cumulative accumulation — not
    area_sqkm, which is the local catchment. The rule asks whether two reaches
    carry the same flow, which only the cumulative value answers; on local
    area a 5% threshold would test roughly the opposite and silently merge
    almost nothing.

    Runs on post-waterbody topology (live reach_to_id). O(n) over numpy
    arrays; geometry is only touched once per merged chain at the end.
    Merged rows keep the chain start's attributes — it is the most
    downstream reach of the chain, so its total_da_sqkm is the merged reach's
    accumulation and carries through untouched — plus summed length, the top member's is_headwater /
    lake_outlet / lake_to_id, and any member's is_trimmed. Tributary pointers
    into absorbed members are re-pointed at the surviving reach_id.
    """
    gdf = gdf.reset_index(drop=True)
    n = len(gdf)
    ids, ds_pos = _topology(gdf)
    da = gdf[DA_FIELD].to_numpy(dtype=float)
    ln = gdf[LENGTH_KM_FIELD].to_numpy(dtype=float)

    # Reverse adjacency (CSR) and in-degree over live topology.
    src = np.flatnonzero(ds_pos >= 0)
    tgt = ds_pos[src]
    indeg = np.bincount(tgt, minlength=n)
    order = np.argsort(tgt, kind="stable")
    tgt_s, src_s = tgt[order], src[order]
    indptr = np.searchsorted(tgt_s, np.arange(n + 1))

    def parents(j: int) -> np.ndarray:
        return src_s[indptr[j] : indptr[j + 1]]

    # Downstream-first traversal from roots (terminals), deterministic order.
    roots = np.flatnonzero(ds_pos < 0)
    roots = roots[np.argsort(ids[roots].astype(str), kind="stable")]
    topo: list[int] = []
    seen = np.zeros(n, dtype=bool)
    seen[roots] = True
    stack = list(roots[::-1])
    while stack:
        v = int(stack.pop())
        topo.append(v)
        ps = parents(v)
        ps = ps[np.argsort(ids[ps].astype(str), kind="stable")][::-1]
        for u in ps:
            if not seen[u]:
                seen[u] = True
                stack.append(int(u))
    if not seen.all():
        leftovers = np.flatnonzero(~seen)
        logger.warning(
            "%d reaches unreachable from any terminal (cycle?)", len(leftovers)
        )
        topo.extend(
            int(v) for v in leftovers[np.argsort(ids[leftovers].astype(str))]
        )

    chains: dict[int, list[int]] = {}
    assigned = np.zeros(n, dtype=bool)
    for v in topo:
        if assigned[v]:
            continue
        assigned[v] = True
        members = [v]
        chain_len = ln[v]
        start_da = da[v]
        cur = v
        while True:
            # The threshold is a FLOOR, not a ceiling: keep absorbing until the
            # chain is long enough to model, then stop. A reach that already
            # clears it never merges at all.
            if chain_len >= min_length_threshold_km:
                break
            if indeg[cur] != 1:
                break
            u = int(parents(cur)[0])
            if assigned[u] or start_da <= 0:
                break
            if (
                abs(da[u] - start_da) / start_da * 100.0
                >= drainage_area_threshold_percent
            ):
                break
            assigned[u] = True
            members.append(u)
            chain_len += ln[u]
            cur = u
        if len(members) > 1:
            chains[v] = members

    counters.n_reaches_merged = sum(len(m) - 1 for m in chains.values())
    if not chains:
        return gdf

    geom = gdf.geometry.name
    absorbed: list[int] = []
    remap: dict[str, str] = {}
    for start, members in chains.items():
        merged_geom = shapely.line_merge(
            shapely.union_all(gdf.geometry.to_numpy()[members])
        )
        top = members[-1]
        gdf.loc[start, geom] = merged_geom
        gdf.loc[start, LENGTH_KM_FIELD] = float(ln[members].sum())
        gdf.loc[start, IS_HEADWATER_FIELD] = bool(gdf[IS_HEADWATER_FIELD].iloc[top])
        gdf.loc[start, LAKE_OUTLET_FIELD] = bool(gdf[LAKE_OUTLET_FIELD].iloc[top])
        gdf.loc[start, LAKE_TO_ID_FIELD] = gdf[LAKE_TO_ID_FIELD].iloc[top]
        gdf.loc[start, IS_TRIMMED_FIELD] = bool(
            gdf[IS_TRIMMED_FIELD].iloc[members].any()
        )
        for m in members[1:]:
            absorbed.append(m)
            remap[str(ids[m])] = str(ids[start])

    gdf = gdf.drop(index=absorbed)
    # Tributaries that pointed into an absorbed member follow it into the
    # surviving reach.
    rt = gdf[REACH_TO_ID_FIELD]
    gdf[REACH_TO_ID_FIELD] = rt.map(remap).fillna(rt).astype("string")
    return gdf.reset_index(drop=True)


### FINALIZE ###


def finalize_network(
    gdf: gpd.GeoDataFrame, counters: NetworkCounters
) -> gpd.GeoDataFrame:
    """Canonical ordering, output column selection, and final-artifact counters.

    Source columns outside OUTPUT_COLUMNS are dropped here — fp_id/fp_to_id
    are superseded by reach_id/reach_to_id, and unlisted NHF attributes are
    not part of the published contract.
    """
    gdf = _normalize_dtypes(gdf)
    gdf = gdf.iloc[_natural_order(gdf[REACH_ID_FIELD])].reset_index(drop=True)

    missing = [c for c in OUTPUT_COLUMNS if c not in gdf.columns]
    if missing:
        raise ValueError(f"output is missing contract column(s): {missing}")
    gdf = gdf[[*OUTPUT_COLUMNS, gdf.geometry.name]]

    counters.n_reaches_output = len(gdf)
    counters.n_headwater_reaches = int(gdf[IS_HEADWATER_FIELD].sum())
    counters.n_terminal_reaches = int(gdf[IS_TERMINAL_FIELD].sum())
    return gdf


def _natural_order(ids: pd.Series) -> np.ndarray:
    """Positions sorting '2' before '10', and '8' immediately before '8_1'."""
    parsed = [
        (int(m.group(1)), int(m.group(2) or 0), s)
        if (m := re.fullmatch(r"(\d+)(?:_(\d+))?", s))
        else (np.iinfo(np.int64).max, 0, s)
        for s in ids.astype(str)
    ]
    return np.array(sorted(range(len(parsed)), key=lambda i: parsed[i]), dtype=int)


def _normalize_dtypes(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Concats can widen dtypes; pin the contract columns back down."""
    for field in _FLAG_FIELDS:
        gdf[field] = gdf[field].astype(bool)
    for field in (
        REACH_ID_FIELD,
        REACH_TO_ID_FIELD,
        TERMINAL_REASON_FIELD,
        LAKE_TO_ID_FIELD,
        COAST_TO_ID_FIELD,
    ):
        gdf[field] = gdf[field].astype("string")
    return gdf
