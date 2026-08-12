# modify_network

## Overview

Modify NGWPC Hydrofabric (NHF) to prepare for hydraulic modeling. The job trim, tag, split, and merge the raw hydrofabric reach network removing/trimming reaches at lakes and coasts, tagging waterbody connectivity, and merging negligible-drainage-difference short reaches into a network that is ready for modeling.

## Inputs

### Required

| Name                         | Type | Description                                                                                          |
| ---------------------------- | ---- | ---------------------------------------------------------------------------------------------------- |
| reach_network_path           | str  | Raw hydrofabric reach network. It must be GPKG file. Current column/layer names compliance is with NHF v1.2.3 `flowpaths` layer schema by default. |
| base_output_path             | str  | Output location for the modified network and manifest                                                |

### Optional

| Name                            | Type  | Description                                                                                          |
| ------------------------------- | ----- | ---------------------------------------------------------------------------------------------------- |
| lakes_layer_path                | str   | Lakes/waterbody dataset. Current column/layer names compliance is with NHF v1.2.3 `lakes_polygons` layer schema by default. It must be GPKG file. **Omit to skip lake processing entirely** — step 5 is not run, no `lakes.gpkg` is written, and every lake metric is null. |
| coastal_influence_layer_path    | str   | Coastal/tidal influence surface boundary as a vector dataset . It must be GPKG file. Default layer name is `coastal_influence`. **Omit to skip coastal processing entirely** — step 4 is not run and every coastal metric is null. |
| drainage_area_threshold_percent | float | Max drainage-area difference (%) between reaches eligible for merge. Default 5 (DR-024)              |
| stream_order_filter_threshold   | int   | Minimum Strahler stream order kept in the network at all. No default — **omit to skip stream-order filtering entirely**: every reach in `reach_network_path` enters processing and `n_reaches_below_stream_order_removed` is null. |
| min_length_threshold_km         | float | **Minimum** length (km) a reach should reach by merging — a floor, not a ceiling. Merging continues until the chain clears it, so no output reach is shorter unless topology or drainage area prevented it. A reach already at or above it never merges. Default 5. (DR-024, whose wording says "max" — see note below) |
| lake_area_threshold_sqkm        | float | Minimum lake area (km²) considered at all; smaller waterbodies are dropped before any reach classification. Default 5. |
| negative_lake_buffer_meters     | float | Inward buffer (m) applied to raw waterbody polygons to approximate the dead-pool extent — this *is* DR-034 ALT-A's "shrink an existing waterbody dataset," not a separate dataset. Default 50 |

## Processing Scope

Order of operations

1. Load the raw reach network from `reach_network_path`, restricted to `stream_order >= stream_order_filter_threshold` **at load time** — reaches below the threshold never enter processing at all. When `stream_order_filter_threshold` is not given, no restriction is applied and the whole network is loaded.
2. Identify terminal reaches: null downstream reach (`fp_to_id`) → `is_terminal=true`, `terminal_reason="outlet"`.
3. Identify headwater reaches: reaches that are no other reach's `fp_to_id` — nothing in the network flows into them. Evaluated **after** the stream-order filter, so a reach can become an apparent headwater purely because its true upstream neighbor was filtered out, not because it's hydrologically a headwater. With a threshold of 3 that is the common case, not the exception: every order-3 reach loses both its order-2 feeders to the filter, so apparent headwaters dominate the count.
4. **Coastal waterbodies** (processed before lakes) — **skipped entirely when `coastal_influence_layer_path` is not given**: for reaches intersecting the coast layer

   - fully inside → dropped it and all reaches further downstream.
   - downstream end inside but upstream not→ trimmed to the upstream portion, `is_terminal=true`, `reach_to_id=null`, `terminal_reason='coast'` and reaches downstream of it are dropped.
   - after the cascade, any surviving reach whose `reach_to_id` points at a deleted reach — a tributary that flowed into the cascade zone without itself intersecting the coast layer — → `is_terminal=true`, `reach_to_id=null`, `terminal_reason='coast'`, geometry untouched. Counted by `n_reaches_stranded_coastal`; not removed, so it does not enter the accounting identity.

   Trimmed reaches record the coastal polygon's `id` in `coast_to_id`. Stranded reaches leave it null — that null is what distinguishes "met the coast" from "lost its downstream to the cascade".
5. **Lakes** — **skipped entirely when `lakes_layer_path` is not given**:

   - fully inside → `lake_encompassed=true`, dropped.
   - **both ends inside the same `lake_id`** → `lake_encompassed=true`, dropped. The reach lies in the lake and is only excluded from "fully inside" because it crosses an island, or a gap between the lake's parts. Comparison is on `lake_id`, not polygon, since a multipart lake is exploded into several polygons that share one id. A reach starting in one lake and ending in a *different* one is handled by the next case instead.
   - downstream end inside, upstream not → `lake_inlet=true`, trimmed to the upstream portion, `is_terminal=true`, `reach_to_id=null`, `terminal_reason='lake'`.
   - upstream end inside, downstream not → `lake_outlet=true`, trimmed to the downstream portion, `is_headwater=true`.
   - **both ends inside different `lake_id`s** → a real channel running between two waterbodies: trimmed at *both* ends, keeping the dry middle. The inverse of the pass-through case below, which keeps the ends and drops the middle. The survivor is `lake_outlet=true` and `lake_inlet=true`, `is_headwater=true` and `is_terminal=true` with `terminal_reason='lake'`, `reach_to_id=null`. `lake_to_id` names the lake it flows *into*. If the two lakes touch and leave no dry middle, it is encompassed instead. Counted by `n_reaches_trimmed_between_lakes`; the row is kept, so it does not enter the accounting identity.
   - passes through (neither end inside, crosses the boundary twice) → split into two reaches: the original `reach_id` becomes the upstream/inlet piece, and the downstream/outlet piece is named after it by suffix — `8` splits into `8` and `8_1`, a second split into `8_2`, and so on. Deriving the id from the parent makes lineage readable and makes collision with a source id impossible, including with reaches the stream-order filter removed.

   Every reach that meets a lake records that lake's `lake_id` in `lake_to_id`; both split pieces record the lake between them.

   After the four cases above, any reach left with **no upstream and no downstream** in the surviving network is dropped as an orphan, counted by `n_reaches_orphaned_lake`. Lake removal can strip both of a reach's neighbours and leave it attached to nothing. `is_headwater` is the discriminator and needs no special case: it is false only for reaches that had an upstream neighbour at step 3, so a genuine one-reach watershed draining into a lake is kept, and so is a lake outlet whose downstream was encompassed (the outlet rule marks it headwater).

   Lakes are first filtered to `lake_area_threshold_sqkm` and shrunk by `negative_lake_buffer_meters` before this step runs.
6. **Flat reaches**: no-op by design (a deliberate pass-through) — matches DR-027 ALT-A ("Do Nothing"), not a gap.
7. **Merge**: starting downstream, merge with the upstream neighbor when the `total_da_sqkm` difference is under `drainage_area_threshold_percent` from the start point (cumulative drainage area, not `area_sqkm`, which is the local catchment — the rule asks whether two reaches carry the same flow), the chain so far is still **shorter than** `min_length_threshold_km`, and the reach has only one reach upstream (junctions are never merge candidates). **Upstream count is measured on the filtered, post-waterbody network, not the raw source**: a tributary removed by `stream_order_filter_threshold`, or one whose `reach_to_id` was nulled by a lake/coastal trim, does not make its outlet a junction. The drainage-area rule is what guards a real confluence — a filtered-out tributary's water is still counted in the downstream reach's `total_da_sqkm`, so a materially contributing inflow fails the threshold even though the tributary itself is absent. Keep merging upstream reach by reach while all three hold; stop at the first candidate that fails any of them. The length rule is a floor: absorbing stops as soon as the chain clears the threshold, which bounds a merged reach at the threshold plus the single reach that crossed it. Two cases can still leave a reach below the floor — the most upstream remnant of a chain, which has nothing left to absorb, and a reach whose only neighbor fails the drainage-area or junction test. The surviving row is the chain's most downstream reach, so it keeps its own `total_da_sqkm` — that is already the merged reach's accumulation — and takes the summed `length_km`.
8. Write the modified network, and the lakes layer actually used if lake processing ran.
9. Write `network.json` last.

Skipping a waterbody step is a no-op on the network, not a degraded pass: no reach is dropped, trimmed, split, or tagged on that account, and the corresponding identity hash is recorded as null. A network built without lakes is therefore a distinct identity from one built with them, not an interchangeable substitute.

## Artifacts

| Artifact                                        | Description                                                                                          |
| ----------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `base_output_path/<identity_hash>/network.gpkg` | Modified reach network, written as the **`reach_network`** layer — the table name `build_model` queries through its `db_uri` (the input NHF layer is `flowpaths`; the output deliberately differs). **Output columns are a closed set**; every source column outside it is dropped, including `fp_id`/`fp_to_id`, which `reach_id`/`reach_to_id` supersede. Identity: `reach_id`, `reach_to_id` (both TEXT — split pieces are suffixed, e.g. `8_1`). Waterbody references: `lake_to_id` (the lake's `lake_id`), `coast_to_id` (the coastal polygon's `id`), null where the reach never met one. Tags: `is_headwater`, `is_terminal`, `terminal_reason` (one of `outlet`, `coast`, `lake`, or null when `is_terminal` is false; coastal breaks persist only as `terminal_reason='coast'` — no separate coastal inlet/outlet/encompassed columns), `lake_inlet`, `lake_outlet`, `is_trimmed` (true where the geometry was cut; false for stranded reaches). Attributes carried through for `build_model`: `stream_order`, `total_da_sqkm`, `length_km`. `area_sqkm` (the local catchment) is not carried — nothing downstream reads it, and it would be wrong on a merged row unless summed. |
| `base_output_path/<identity_hash>/lakes.gpkg`   | Filtered + buffered lake polygons actually used for classification (QC/reference only — not inserted as network reaches, per DR-037 ALT-B). Written only when `lakes_layer_path` was given; absent otherwise |
| `base_output_path/<identity_hash>/network.json` | Network definition and artifact inventory — see `network.schema.json`                                |

## Response

- `identity_hash` — str

## Out of Scope

- Building any single reach's Model (`build_model`'s job) — including the lake-outlet inflow BC offset DR-007 step 4 flags as informed by these tags but decided elsewhere.
- Producing or maintaining the lakes or coastal vector source datasets themselves.
- Anything downstream-of-network scenario/run logic.

## Dependencies

- Python
- GeoPandas / Shapely / GDAL
- AWS CLI

## Errors

- Source network unavailable, or a lakes/coastal dataset was given but cannot be read — raises `DatasetUnavailableError` (prototype currently raises `FileNotFoundError` directly for lakes/coastal; job-wrapping should normalize to the shared exception set). Omitting a lakes or coastal path is not an error — it skips that step; only a path that was supplied and does not resolve is.
- Output artifacts cannot be written — raises `WriteFailureError`

## Checks

- network already exists at output path — return immediately with warning `network_exists`. No processing runs and nothing is written: the existing artifacts and their `network.json` are left byte-for-byte untouched, and the existing `identity_hash` is returned. The warning is returned to the caller only — it is never written into the pre-existing manifest, since mutating it would invalidate its own checksums. Re-running a completed job is therefore a no-op, not a rebuild; delete the output directory to force one.
- reach flagged by both lake and coastal logic — warning `ambiguous_reach_classification`, keep processing (a real possibility when both datasets are given, since coastal and lake processing then run sequentially over the same reach set; impossible when either step is skipped). The reach is still removed once and counted once, against coastal — see Metrics/Accounting.

## Metrics

Recorded in `network.json` under `properties` (see `network.schema.json`) — one counter per processing branch above, so a run is auditable against the code path it took. `0` and `null` are distinct: `0` means the branch ran and matched nothing, `null` means the branch did not run, so all lake counters are null when `lakes_layer_path` was omitted and all coastal counters are null when `coastal_influence_layer_path` was omitted:

- `n_reaches_input` — rows read from `reach_network_path`, before the stream-order filter; `n_reaches_output` — rows in the written `network.gpkg`, after every step including merge
- `n_reaches_below_stream_order_removed`
- `n_reaches_encompassed_removed_lake`, `n_reaches_encompassed_removed_coastal`
- `n_reaches_trimmed_inlet_lake`, `n_reaches_trimmed_outlet_lake`
- `n_reaches_trimmed_inlet_coastal` — downstream end inside coastal coverage, upstream not (step 4's second case)
- `n_reaches_dropped_coastal_cascade` — reaches removed for being downstream of a coastal encompassed/trimmed reach, not for their own classification
- `n_reaches_stranded_coastal` — tributaries left pointing at a cascade-deleted reach without themselves intersecting the coast layer; made terminal (`terminal_reason='coast'`) with geometry untouched. Not removed — absent from the accounting identity
- `n_reaches_split_passthrough_lake`
- `n_reaches_trimmed_between_lakes` — started in one lake, ended in another; trimmed at both ends, dry middle kept. Row kept, so absent from the identity
- `n_reaches_orphaned_lake` — left with no upstream and no downstream once lake removal took both neighbours, and not an original headwater. A removal, so it enters the identity below
- `n_reaches_merged`
- `n_headwater_reaches`, `n_terminal_reaches` — **states of the final artifact, not counts of any one step**: rows in the written `network.gpkg` with the flag set, measured after all trimming, splitting, and merging. Both flags are written by more than one step — `is_terminal` at steps 2, 4 and 5, `is_headwater` at steps 3 and 5 — so each exceeds the tally of the step that first sets it. The two overlap (a reach can be both), so they are not additive and neither appears in the accounting identity below

### Accounting

The removal counters are **disjoint**: every reach is counted in exactly one of them, the branch that actually removed it. A reach flagged by both coastal and lake logic — the `ambiguous_reach_classification` case — is attributed to coastal, because step 4 runs before step 5 and therefore removed it. That warning is a data-quality signal only; it never causes a reach to be counted twice.

Disjointness is what makes the counters reconcile, so a manifest must satisfy:

```
n_reaches_output = n_reaches_input
                 - n_reaches_below_stream_order_removed
                 - n_reaches_encompassed_removed_lake
                 - n_reaches_encompassed_removed_coastal
                 - n_reaches_dropped_coastal_cascade
                 - n_reaches_orphaned_lake
                 - n_reaches_merged
                 + n_reaches_split_passthrough_lake
```

The three trim counters are absent by design — trimming reshapes a reach's geometry and keeps its row, so it changes no count. Splits add rows: each pass-through split turns one reach into two, hence the `+`. Skipped steps drop out of the identity along with their null counters.

## Performance

Runs over the entire network in one call, not per reach — expect the longest-running job in the system and infrequent (once per hydrofabric release or methodology/threshold change).

## Open Questions

- A reach kept as a channel between two lakes records only the downstream lake in `lake_to_id`, since that is what the column name means. The upstream lake it emerges from is not captured anywhere. If a consumer needs it — the lake-outlet inflow BC offset in DR-007 is the likely case — a `lake_from_id` column would be the addition.
