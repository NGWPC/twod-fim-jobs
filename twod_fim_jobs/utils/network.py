"""Network-modification algorithms for the modify_network job.

Pure transforms over GeoDataFrames plus plain numpy topology arrays — no
manifest or storage concerns (those live in jobs/modify_network.py). Every
function is deterministic: iteration orders are sorted, split ids are minted
sequentially in reach_id order, and finalize_network() sorts the output
canonically, so identical inputs produce byte-identical artifacts.

Assumptions, stated once:
- Flowpath geometries are digitized upstream -> downstream (first vertex is
  the upstream end), matching NHF convention.
- The network CRS is projected with meter units; lengths and the negative
  lake buffer depend on it (checked at load).
- reach ids are non-negative integers (-1 is used as a null sentinel).

Counter discipline (see modify_network_specs.md Metrics/Accounting): every
removal is counted in exactly one branch, so the reconciliation identity
holds by construction. Trims, strands, and splits keep their rows.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
import shapely
from shapely import Point
from shapely.ops import substring

from twod_fim_jobs.consts import (
    AREA_SQKM_FIELD,
    FLOWPATHS_LAYER,
    FP_ID_FIELD,
    FP_TO_ID_FIELD,
    IS_HEADWATER_FIELD,
    IS_TERMINAL_FIELD,
    IS_TRIMMED_FIELD,
    LAKE_INLET_FIELD,
    LAKE_OUTLET_FIELD,
    LENGTH_KM_FIELD,
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
    n_reaches_merged: int | None = None
    n_reaches_output: int | None = None
    n_headwater_reaches: int | None = None
    n_terminal_reaches: int | None = None


### LOADING ###


def load_reach_network(
    path: str, stream_order_filter_threshold: int | None
) -> tuple[gpd.GeoDataFrame, NetworkCounters, int]:
    """Load the flowpaths layer, filtering by stream order at read time.

    Reaches below the threshold never materialize (pushed down to GDAL).
    When the threshold is None no filter is applied and the whole network
    loads; n_reaches_below_stream_order_removed stays None.

    Also returns the maximum reach id present in the SOURCE, before
    filtering. Split reaches must be numbered above that: a filtered-out
    reach still exists in the hydrofabric, so minting from the loaded
    maximum would silently reuse its id.
    """
    counters = NetworkCounters()
    try:
        n_input = int(pyogrio.read_info(path, layer=FLOWPATHS_LAYER)["features"])
        where = None
        if stream_order_filter_threshold is not None:
            where = f"{STREAM_ORDER_FIELD} >= {int(stream_order_filter_threshold)}"
        gdf = gpd.read_file(path, layer=FLOWPATHS_LAYER, where=where)
        if n_input < 0:  # driver without fast feature count
            if where is None:
                n_input = len(gdf)
            else:
                n_input = len(
                    pyogrio.read_dataframe(
                        path, layer=FLOWPATHS_LAYER, columns=[], read_geometry=False
                    )
                )
    except DatasetUnavailableError:
        raise
    except Exception as exc:
        raise DatasetUnavailableError(
            f"Cannot read reach network layer '{FLOWPATHS_LAYER}' at {path}: {exc}"
        ) from exc

    if gdf.crs is None or not gdf.crs.is_projected:
        raise ValueError(
            "Reach network CRS must be projected with meter units; lengths and "
            f"the negative lake buffer depend on it (got {gdf.crs})."
        )

    counters.n_reaches_input = n_input
    if stream_order_filter_threshold is not None:
        counters.n_reaches_below_stream_order_removed = n_input - len(gdf)
        # One id column, no geometry: cheap even on a national hydrofabric.
        source_ids = pyogrio.read_dataframe(
            path, layer=FLOWPATHS_LAYER, columns=[FP_ID_FIELD], read_geometry=False
        )[FP_ID_FIELD]
        max_source_reach_id = int(source_ids.max()) if len(source_ids) else 0
    else:
        max_source_reach_id = int(gdf[FP_ID_FIELD].max()) if len(gdf) else 0

    return _init_columns(gdf), counters, max_source_reach_id


def load_vector_layer(path: str, layer: str, target_crs) -> gpd.GeoDataFrame:
    """Load a lakes/coastal vector layer and reproject to the network CRS."""
    try:
        gdf = gpd.read_file(path, layer=layer)
    except Exception as exc:
        raise DatasetUnavailableError(
            f"Cannot read layer '{layer}' at {path}: {exc}"
        ) from exc
    return gdf.to_crs(target_crs)


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

    ids = gdf[FP_ID_FIELD].astype("int64")
    if (ids < 0).any():
        raise ValueError("fp_id must be non-negative (-1 is the null sentinel)")
    gdf[REACH_ID_FIELD] = ids
    gdf[REACH_TO_ID_FIELD] = gdf[FP_TO_ID_FIELD].astype("Int64")
    for field in _FLAG_FIELDS:
        gdf[field] = False
    gdf[TERMINAL_REASON_FIELD] = pd.Series([None] * len(gdf), dtype=object)
    return gdf.reset_index(drop=True)


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
    dropped. No union across lakes — polygons stay individual, and endpoint
    tests are 'within any polygon'.
    """
    g = lakes_gdf[lakes_gdf.geometry.notna() & ~lakes_gdf.geometry.is_empty]
    g = g[g.geometry.area > lake_area_threshold_sqkm * 1e6]
    g = g.assign(**{g.geometry.name: g.geometry.buffer(-negative_lake_buffer_meters)})
    g = g[~g.geometry.is_empty]
    g = g.explode(ignore_index=True)
    g = g[g.geometry.area > 0]
    return g.reset_index(drop=True)


### SHARED GEOMETRY HELPERS ###


def _topology(gdf: gpd.GeoDataFrame) -> tuple[np.ndarray, np.ndarray]:
    """(ids, ds_pos): downstream pointer as positional index, -1 for none.

    Always derived from the LIVE reach_to_id column — never fp_to_id — so
    waterbody edits (nulled pointers, splits, deletions) are respected.
    """
    ids = gdf[REACH_ID_FIELD].to_numpy(dtype="int64")
    ds = gdf[REACH_TO_ID_FIELD].fillna(-1).to_numpy(dtype="int64")
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


def _classify_crossings(
    gdf: gpd.GeoDataFrame, polys: gpd.GeoDataFrame
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, dict[int, list[int]]]:
    """Spatially classify reaches against waterbody polygons.

    Returns (within_pos, crossing_pos, upstream_in, downstream_in, poly_map):
    positions of fully-encompassed reaches, positions of boundary-crossing
    reaches, per-crossing endpoint containment, and crossing position ->
    intersecting polygon positions (for boundary construction).
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
        for k, grp in pairs.loc[
            pairs.index.isin(crossing_pos), "index_right"
        ].groupby(level=0)
    }

    lines = gdf.geometry.to_numpy()[crossing_pos]
    up_pts = shapely.get_point(lines, 0)
    dn_pts = shapely.get_point(lines, -1)

    def within_any(points: np.ndarray) -> np.ndarray:
        pts = gpd.GeoDataFrame(geometry=points, crs=gdf.crs)
        hit = gpd.sjoin(
            pts, polys[[polys.geometry.name]], predicate="within", how="inner"
        )
        mask = np.zeros(len(points), dtype=bool)
        mask[hit.index.unique().to_numpy()] = True
        return mask

    upstream_in = within_any(up_pts)
    downstream_in = within_any(dn_pts)
    return within_pos, crossing_pos, upstream_in, downstream_in, poly_map


def _crossing_distances(line, boundary) -> np.ndarray:
    """Sorted projected distances of every line/boundary crossing.

    Robust to degenerate intersections: a collinear overlap contributes its
    endpoints' distances instead of raising (the prototype crashed here).
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
) -> tuple[gpd.GeoDataFrame, set[int]]:
    """Coastal classification, cascade removal, and the stranded sweep.

    Cases (spec step 4): fully inside -> dropped with everything downstream;
    downstream end inside -> trimmed to the upstream portion and made
    terminal ('coast'), everything downstream dropped. Coastal has no
    outlet/pass-through case — such reaches are left untouched (logged).
    Returns the surviving frame and the reach_ids the coastal pass touched
    but kept (for the ambiguous-classification check against the lake pass).
    """
    gdf = gdf.reset_index(drop=True)
    within_pos, crossing_pos, us_in, ds_in, poly_map = _classify_crossings(
        gdf, coastal_gdf
    )

    inlet_pos = crossing_pos[ds_in & ~us_in]
    other = int(len(crossing_pos) - len(inlet_pos))
    if other:
        logger.info(
            "%d coastal-crossing reaches are not inlet-classified (coastal has "
            "no outlet/pass-through case); left untouched",
            other,
        )

    # Trim inlets first so degenerate trims can escalate to encompassed
    # before any counting happens.
    encompassed = np.zeros(len(gdf), dtype=bool)
    encompassed[within_pos] = True
    trimmed_pos: list[int] = []
    for p in sorted(int(p) for p in inlet_pos):
        line = gdf.geometry.iloc[p]
        dists = _crossing_distances(line, _boundary_for(coastal_gdf, poly_map[p]))
        if len(dists) == 0 or dists.min() <= 0 or dists.min() >= line.length:
            encompassed[p] = True  # effectively fully inside
            continue
        cut = float(dists.min())
        gdf.loc[p, gdf.geometry.name] = substring(line, 0, cut)
        gdf.loc[p, LENGTH_KM_FIELD] = cut / 1000.0
        gdf.loc[p, IS_TRIMMED_FIELD] = True
        trimmed_pos.append(p)

    # Cascade: everything strictly downstream of an encompassed or trimmed
    # reach is removed. Deletion wins over trim if a trimmed reach is itself
    # downstream of another break (counted once, as cascade).
    ids, ds_pos = _topology(gdf)
    flagged = np.flatnonzero(encompassed)
    flagged = np.union1d(flagged, np.array(trimmed_pos, dtype=int))
    seeds = ds_pos[flagged]
    closure = _downstream_closure(ds_pos, seeds[seeds >= 0])
    deletion = encompassed | closure

    counters.n_reaches_encompassed_removed_coastal = int(encompassed.sum())
    counters.n_reaches_dropped_coastal_cascade = int((closure & ~encompassed).sum())

    surviving_trims = [p for p in trimmed_pos if not deletion[p]]
    gdf.loc[surviving_trims, IS_TERMINAL_FIELD] = True
    gdf.loc[surviving_trims, TERMINAL_REASON_FIELD] = TERMINAL_REASON_COAST
    gdf.loc[surviving_trims, REACH_TO_ID_FIELD] = pd.NA
    counters.n_reaches_trimmed_inlet_coastal = len(surviving_trims)

    touched = set(int(i) for i in ids[surviving_trims])
    gdf = gdf.loc[~deletion].reset_index(drop=True)

    # Stranded sweep: tributaries that flowed into a cascade-deleted reach
    # without themselves intersecting the coast layer. Made terminal in
    # place, geometry untouched. Lakes need no equivalent — they have no
    # cascade, and anything pointing into a lake-encompassed reach has its
    # own downstream end inside the lake, so the inlet rule nulls it.
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
    gdf: gpd.GeoDataFrame,
    lakes_gdf: gpd.GeoDataFrame,
    counters: NetworkCounters,
    max_source_reach_id: int,
) -> tuple[gpd.GeoDataFrame, set[int]]:
    """Lake classification: encompassed / inlet / outlet / pass-through split.

    Pass-through splits keep the original reach_id on the upstream/inlet
    piece and mint a new reach_id for the downstream/outlet piece (spec step
    5). A reach crossing multiple lakes is cut at its first entry and last
    exit across all of them — still two pieces, per the spec's 'split into
    two reaches'. Returns the frame and all reach_ids the lake pass touched.
    """
    gdf = gdf.reset_index(drop=True)
    within_pos, crossing_pos, us_in, ds_in, poly_map = _classify_crossings(
        gdf, lakes_gdf
    )

    encompassed = np.zeros(len(gdf), dtype=bool)
    encompassed[within_pos] = True

    inlet_pos = [int(p) for p in crossing_pos[ds_in]]  # both-ends-in -> inlet
    outlet_pos = [int(p) for p in crossing_pos[us_in & ~ds_in]]
    passthrough_pos = [int(p) for p in crossing_pos[~us_in & ~ds_in]]

    n_inlet = n_outlet = n_split = 0
    new_rows: list[dict] = []
    # Clear the SOURCE maximum, not the loaded one: a filtered-out reach
    # still owns its id in the hydrofabric.
    next_id = max(int(gdf[REACH_ID_FIELD].max()), max_source_reach_id) + 1
    geom = gdf.geometry.name

    for p in sorted(inlet_pos):
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
        n_inlet += 1

    for p in sorted(outlet_pos):
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
        n_outlet += 1

    for p in sorted(passthrough_pos):
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

        # Downstream/outlet piece: a new reach that inherits the original's
        # downstream connectivity and its step-2 terminal state.
        outlet_row = gdf.iloc[p].to_dict()
        outlet_row[geom] = substring(line, d_last, line.length)
        outlet_row[REACH_ID_FIELD] = next_id
        outlet_row[LENGTH_KM_FIELD] = (line.length - d_last) / 1000.0
        outlet_row[LAKE_OUTLET_FIELD] = True
        outlet_row[IS_HEADWATER_FIELD] = True
        outlet_row[IS_TRIMMED_FIELD] = True
        new_rows.append(outlet_row)
        next_id += 1

        # Upstream/inlet piece keeps the original reach_id, so upstream
        # neighbors' pointers stay valid.
        gdf.loc[p, geom] = substring(line, 0, d_first)
        gdf.loc[p, LENGTH_KM_FIELD] = d_first / 1000.0
        gdf.loc[p, LAKE_INLET_FIELD] = True
        gdf.loc[p, IS_TERMINAL_FIELD] = True
        gdf.loc[p, TERMINAL_REASON_FIELD] = TERMINAL_REASON_LAKE
        gdf.loc[p, REACH_TO_ID_FIELD] = pd.NA
        gdf.loc[p, IS_TRIMMED_FIELD] = True
        n_split += 1

    touched = set(int(i) for i in gdf[REACH_ID_FIELD].to_numpy()[encompassed])
    touched |= {
        int(gdf[REACH_ID_FIELD].iloc[p])
        for p in (*inlet_pos, *outlet_pos, *passthrough_pos)
        if not encompassed[p]
    }
    touched |= {int(r[REACH_ID_FIELD]) for r in new_rows}

    counters.n_reaches_encompassed_removed_lake = int(encompassed.sum())
    counters.n_reaches_trimmed_inlet_lake = n_inlet
    counters.n_reaches_trimmed_outlet_lake = n_outlet
    counters.n_reaches_split_passthrough_lake = n_split

    gdf = gdf.loc[~encompassed]
    if new_rows:
        additions = gpd.GeoDataFrame(new_rows, geometry=geom, crs=gdf.crs)
        gdf = pd.concat([gdf, additions], ignore_index=True)
    return _normalize_dtypes(gdf.reset_index(drop=True)), touched


### MERGE (spec step 7) ###


def merge_short_reaches(
    gdf: gpd.GeoDataFrame,
    drainage_area_threshold_percent: float,
    max_length_threshold_km: float,
    counters: NetworkCounters,
) -> gpd.GeoDataFrame:
    """Chain-merge short reaches walking upstream from each chain start.

    Spec step 7: starting downstream, absorb the upstream neighbor while (a)
    its drainage-area difference from the CHAIN START is under the threshold,
    (b) cumulative chain length stays under the cap, and (c) the current
    reach has exactly one upstream neighbor (junctions never merge).

    Runs on post-waterbody topology (live reach_to_id). O(n) over numpy
    arrays; geometry is only touched once per merged chain at the end.
    Merged rows take the chain start's attributes (its drainage area is the
    chain's outlet DA), summed length, the top member's is_headwater /
    lake_outlet, and any member's is_trimmed. Tributary pointers into
    absorbed members are re-pointed at the surviving reach_id.
    """
    gdf = gdf.reset_index(drop=True)
    n = len(gdf)
    ids, ds_pos = _topology(gdf)
    da = gdf[AREA_SQKM_FIELD].to_numpy(dtype=float)
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
    roots = roots[np.argsort(ids[roots], kind="stable")]
    topo: list[int] = []
    seen = np.zeros(n, dtype=bool)
    seen[roots] = True
    stack = list(roots[::-1])
    while stack:
        v = int(stack.pop())
        topo.append(v)
        ps = parents(v)
        ps = ps[np.argsort(ids[ps], kind="stable")][::-1]
        for u in ps:
            if not seen[u]:
                seen[u] = True
                stack.append(int(u))
    if not seen.all():
        # A cycle in fp topology is data corruption, but don't lose reaches.
        leftovers = np.flatnonzero(~seen)
        logger.warning("%d reaches unreachable from any terminal (cycle?)", len(leftovers))
        topo.extend(int(v) for v in leftovers[np.argsort(ids[leftovers])])

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
            if indeg[cur] != 1:
                break
            u = int(parents(cur)[0])
            if assigned[u] or start_da <= 0:
                break
            if abs(da[u] - start_da) / start_da * 100.0 >= drainage_area_threshold_percent:
                break
            if chain_len + ln[u] > max_length_threshold_km:
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
    remap: dict[int, int] = {}
    for start, members in chains.items():
        merged_geom = shapely.line_merge(
            shapely.union_all(gdf.geometry.to_numpy()[members])
        )
        top = members[-1]
        gdf.loc[start, geom] = merged_geom
        gdf.loc[start, LENGTH_KM_FIELD] = float(ln[members].sum())
        gdf.loc[start, IS_HEADWATER_FIELD] = bool(gdf[IS_HEADWATER_FIELD].iloc[top])
        gdf.loc[start, LAKE_OUTLET_FIELD] = bool(gdf[LAKE_OUTLET_FIELD].iloc[top])
        gdf.loc[start, IS_TRIMMED_FIELD] = bool(
            gdf[IS_TRIMMED_FIELD].iloc[members].any()
        )
        for m in members[1:]:
            absorbed.append(m)
            remap[int(ids[m])] = int(ids[start])

    gdf = gdf.drop(index=absorbed)
    # Tributaries that pointed into an absorbed member follow it into the
    # surviving reach.
    rt = gdf[REACH_TO_ID_FIELD]
    mapped = rt.map(remap)
    gdf[REACH_TO_ID_FIELD] = mapped.combine_first(rt).astype("Int64")
    return gdf.reset_index(drop=True)


### FINALIZE ###


def finalize_network(
    gdf: gpd.GeoDataFrame, counters: NetworkCounters
) -> gpd.GeoDataFrame:
    """Canonical ordering + final-artifact counters (headwater/terminal/output)."""
    gdf = _normalize_dtypes(gdf.sort_values(REACH_ID_FIELD).reset_index(drop=True))
    counters.n_reaches_output = len(gdf)
    counters.n_headwater_reaches = int(gdf[IS_HEADWATER_FIELD].sum())
    counters.n_terminal_reaches = int(gdf[IS_TERMINAL_FIELD].sum())
    return gdf


def _normalize_dtypes(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Concats can widen dtypes; pin the contract columns back down."""
    for field in _FLAG_FIELDS:
        gdf[field] = gdf[field].astype(bool)
    gdf[REACH_ID_FIELD] = gdf[REACH_ID_FIELD].astype("int64")
    gdf[REACH_TO_ID_FIELD] = gdf[REACH_TO_ID_FIELD].astype("Int64")
    return gdf
