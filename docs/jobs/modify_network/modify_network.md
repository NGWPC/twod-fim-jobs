# modify_network job

## Overview

Modify the NGWPC Hydrofabric (NHF) to prepare it for hydraulic modeling. The job trims, tags, splits and merges the raw reach network — removing or trimming reaches at lakes and coasts, tagging waterbody connectivity, and merging reaches whose drainage areas differ negligibly — into a network that is ready for modeling.

Runs over the entire network in one call, not per reach. Expect the longest-running job in the system, and an infrequent one: once per hydrofabric release, or when methodology or thresholds change.

## Inputs

<!-- AUTO:inputs_table -->
### Required

| Name | Type | Description |
| --- | --- | --- |
| `reach_network_path` | `string` | Raw hydrofabric reach network. Must be a GPKG file; column/layer names follow the NHF v1.2.3 flowpaths layer schema. |
| `base_output_path` | `string` | Output location for the modified network and manifest; artifacts are written under <base_output_path>/<identity_hash>/. |

### Optional

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `lakes_layer_path` | `string` | null | Lakes dataset (NHF v1.2.3 lakes_polygons layer schema). Must be a GPKG file. Omit to skip lake processing entirely. |
| `coastal_influence_layer_path` | `string` | null | Coastal/tidal-influence surface boundary vector dataset. Must be a GPKG file; default layer name is coastal_influence. Omit to skip coastal processing entirely. |
| `drainage_area_threshold_percent` | `number` | 5.0 | Max drainage-area difference (%) between reaches eligible for merge (DR-024). |
| `stream_order_filter_threshold` | `integer` | null | Minimum Strahler stream order kept in the network at all. Omit to skip stream-order filtering entirely — every reach enters processing. |
| `min_length_threshold_km` | `number` | 5.0 | Minimum length (km) a reach should reach by merging — a floor, not a ceiling: merging continues until the chain clears it (DR-024). |
| `lake_area_threshold_sqkm` | `number` | 5.0 | Minimum lake area (km2) considered at all; smaller waterbodies are dropped before any reach classification. |
| `negative_lake_buffer_meters` | `number` | 50.0 | Inward buffer (m) applied to raw lake polygons to approximate the dead-pool extent (DR-034 ALT-A). |
<!-- /AUTO:inputs_table -->

Omitting `lakes_layer_path`, `coastal_influence_layer_path`, or `stream_order_filter_threshold` skips that step entirely — a genuine no-op on the network, not a degraded pass. No reach is dropped, trimmed, split or tagged on that account, and the corresponding identity member is recorded as null. A network built without lakes is therefore a distinct identity from one built with them, not an interchangeable substitute.

## Processing Scope

1. **Load** from `reach_network_path`, restricted to `stream_order >= stream_order_filter_threshold` at read time so sub-threshold reaches never enter processing. Contiguous multipart geometries are fused; any that remain multipart are exploded into one LineString per part, numbered `3434_1`, `3434_2`, `3434_3`, chained to each other, with pointers into the parent repointed at the first part. Output geometry is always LineString.
2. **Terminal reaches** — no downstream reach *in this network* → `is_terminal=true`, `reach_to_id=null`, `terminal_reason='outlet'`. Either `fp_to_id` is null, or it names a reach absent from the input — what a clipped or regional extract produces at its boundary. The dangling pointer is nulled, so the artifact never references a reach it does not contain.
3. **Headwater reaches** — reaches that are no other reach's downstream. Evaluated *after* the stream-order filter, so a reach whose feeders were filtered out becomes an apparent headwater. With a threshold of 3 that is the common case, not the exception.
4. **Coastal** (before lakes; skipped when no coastal layer is given). Every reach overlapping the coast layer is removed from its first coastal contact downstream. One binary rule: a reach that **begins inside** the polygon is dropped whole; a reach that **begins outside** is trimmed at its first contact, keeping the portion above it and gaining `terminal_reason='coast'` plus `coast_to_id`. Either way every reach downstream is dropped. Afterwards, any reach left pointing at a deleted reach — a tributary into the cascade zone that never touched the coast layer itself — is made terminal in place with its geometry untouched.
5. **Lakes** (skipped when no lakes layer is given). Fully inside, or both ends inside the same `lake_id`, → dropped. Downstream end inside → trimmed to the upstream portion and made terminal. Upstream end inside → trimmed to the downstream portion and made a headwater. Both ends inside *different* lakes → trimmed at both ends, keeping the dry middle as a channel between them. Neither end inside but crossing twice → split into two suffixed pieces. Lakes are filtered to `lake_area_threshold_sqkm` and shrunk by `negative_lake_buffer_meters` first. Any reach left with no upstream and no downstream, and not an original headwater, is dropped as an orphan.
6. **Flat reaches** — no-op by design, per DR-027 ALT-A.
7. **Merge** — walking upstream from each chain start, absorb the neighbour while the chain is still shorter than `min_length_threshold_km`, the neighbour's drainage area is within `drainage_area_threshold_percent` of the chain start's, and the current reach has exactly one upstream neighbour. The length threshold is a **floor**, not a ceiling (DR-024): merging continues *until* the chain clears it, so no output reach is shorter unless topology or drainage area prevented it.
8. Write the network, and the lakes layer actually used if lake processing ran.
9. Write `network.json` last, so its presence means the whole artifact set landed.

Overlap with a waterbody must be real: a reach that only *touches* a polygon at a point — typically an endpoint snapped to a coastline — is not a crossing and is left alone.

## Artifacts

<!-- AUTO:artifacts_table -->
| Name | Description |
| --- | --- |
| `network` | The modified reach network, written as the reach_network layer — the table name build_model queries through its db_uri. |
| `lakes` | Filtered + buffered lake polygons actually used for classification (QC/reference only, DR-037 ALT-B). Present only when lake processing ran. |
<!-- /AUTO:artifacts_table -->

Written to `base_output_path/<identity_hash>/`. `network.gpkg` holds the `reach_network` layer — the table name `build_model` queries through its `db_uri`. Its columns are a closed set: `reach_id` and `reach_to_id` (TEXT, since a divided reach retires its id and suffixes every piece), `lake_to_id` and `coast_to_id`, the tags `is_headwater` / `is_terminal` / `terminal_reason` / `lake_inlet` / `lake_outlet` / `is_trimmed`, and `stream_order`, `total_da_sqkm` and `length_km` for `build_model`. `length_km` is recomputed from the final geometry for every reach, so any row can be verified against the artifact.

## Response

<!-- AUTO:result_table -->
| Name | Type | Description |
| --- | --- | --- |
| `identity_hash` | `string` | Hash of the network identity inputs (methodology, sources, thresholds). Addresses the output directory and forks on any change. |
| `network_dir` | `string` | Content-addressed path where the network artifacts were written. |
| `warnings` | `list[JobWarning]` | Non-fatal warnings raised during the job. |
<!-- /AUTO:result_table -->

## Checks

- A network already exists at the output path → return immediately with warning `network_exists`. Nothing is processed and nothing is written; the existing artifacts and their `network.json` are left byte-for-byte untouched and the existing `identity_hash` is returned. Re-running a completed job is a no-op, not a rebuild — delete the output directory to force one.
- A reach flagged by both lake and coastal logic → warning `ambiguous_reach_classification`, and processing continues. The reach is removed once and counted once, against coastal, because step 4 runs before step 5.

## Metrics

Recorded under `properties`. One counter per processing branch, so a run is auditable against the code path it took. `0` and `null` are distinct: `0` means the branch ran and matched nothing, `null` means it did not run.

The removal counters are disjoint — every reach is counted in exactly one — so they reconcile:

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

Trim and strand counters are absent by design: those reaches keep their rows. Splits add one row each. JSON Schema cannot express this constraint, so the producer enforces it — a manifest that fails it cannot be constructed.

`n_headwater_reaches` and `n_terminal_reaches` are states of the final artifact, not counts of any one step: rows in the written `network.gpkg` carrying the flag, measured after all trimming, splitting and merging.

## Out of Scope

- Building any single reach's Model — that is `build_model`'s job, including the lake-outlet inflow BC offset that DR-007 flags as informed by these tags but decided elsewhere.
- Producing or maintaining the lakes or coastal vector source datasets.
- Anything downstream-of-network scenario or run logic.

## Related decisions

- [[DR-007]] lakes, [[DR-023]] / [[DR-024]] short-reach merging thresholds, [[DR-027]] flat reaches, [[DR-034]] dead-pool extent, [[DR-037]] lake representation, [[DR-038]] coasts.
