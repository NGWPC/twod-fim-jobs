# modify_network

## Overview

Modify NGWPC Hydrofabric (NHF) to prepare for hydraulic modeling. The job trim, tag, split, and merge the raw hydrofabric reach network removing/trimming reaches at lakes and coasts, tagging waterbody connectivity, and merging negligible-drainage-difference short reaches into a network that is ready for modeling.

## Inputs

### Required

| Name               | Type | Description                                                                                          |
| ------------------ | ---- | ---------------------------------------------------------------------------------------------------- |
| reach_network_path | str  | Raw hydrofabric reach network. It must be GPKG file.                                                 |
| lakes_dataset_path | str  | Lakes/waterbody dataset.                                                                             |
| coasts_vector_path | str  | Coastal/tidal-surface boundary as a vector dataset (e.g. GPKG). The only coastal input actually read for the intersection logic — see `coasts_raster_path` under Optional |
| base_output_path   | str  | Output location for the modified network and manifest                                                |

### Optional

| Name                            | Type  | Description                                                                                          |
| ------------------------------- | ----- | ---------------------------------------------------------------------------------------------------- |
| coasts_raster_path              | str   | Coastal/tidal-surface boundary as a raster (e.g. MHHW GeoTIFF). Not read directly — used only to derive a default `coasts_vector_path` (same path, `.gpkg` extension) when the vector isn't given. Raster-to-vector conversion itself is unimplemented in the prototype — see Errors |
| drainage_area_threshold_percent | float | Max drainage-area difference (%) between reaches eligible for merge. Default 5 (DR-024)              |
| stream_order_filter_threshold   | int   | Minimum Strahler stream order kept in the network at all.                                            |
| max_length_threshold_km         | float | Max combined length (km) of a merged reach chain. Default 3. (DR-024)                                |
| lake_area_threshold_sqkm        | float | Minimum lake area (km²) considered at all; smaller waterbodies are dropped before any reach classification. Default 5. |
| negative_lake_buffer_meters     | float | Inward buffer (m) applied to raw waterbody polygons to approximate the dead-pool extent — this *is* DR-034 ALT-A's "shrink an existing waterbody dataset," not a separate dataset. Default 50 |

## Processing Scope

Order of operations

1. Load the raw reach network from `reach_network_path`, restricted to `stream_order_field >= stream_order_filter_threshold` **at load time** — reaches below the threshold never enter processing at all.
2. Identify terminal reaches: null downstream reach (`fp_to_id`) → `is_terminal=True`, `terminal_reason="outlet"`.
3. Identify headwater reaches: reaches whose `fp_to_id` isn't any other reach's `fp_id`, evaluated **after** the stream-order filter — a reach can become an apparent headwater purely because its true upstream neighbor was filtered out, not because it's hydrologically a headwater.
4. **Coastal waterbodies** (processed before lakes): for reaches intersecting the coast layer

   - fully inside → dropped it and all reaches further downstream.
   - downstream end inside but upstream not→ trimmed to the upstream portion, `is_terminal=True`, `reach_to_id=None`, `terminal_reason='coast'` and reaches downstream of it are dropped.
5. **Lakes**:

   - fully inside → `waterbody_encompassed["lake"]=True`, dropped.
   - downstream end inside, upstream not → `waterbody_inlet["lake"]=True`, trimmed to the upstream portion, `is_terminal=True`, `reach_to_id=None`, `terminal_reason='lake'`.
   - upstream end inside, downstream not → `waterbody_outlet["lake"]=True`, trimmed to the downstream portion, `is_headwater=True`.
   - passes through (neither end inside, crosses the boundary twice) → split into two reaches: the original `reach_id` becomes the upstream/inlet piece, a newly minted `reach_id` becomes the downstream/outlet piece.

   Lakes are first filtered to `lake_area_threshold_sqkm` and shrunk by `negative_lake_buffer_meters` before this step runs.
6. **Flat reaches**: no-op by design (`handle_flat_reaches` is a deliberate pass-through) — matches DR-027 ALT-A ("Do Nothing"), not a gap.
7. **Merge**: starting downstream, merge with the upstream neighbor when drainage-area difference is under `drainage_area_threshold_percent` from the start point, combined length stays under `max_length_threshold_km`, and the reach has only one reach upstream (junctions are never merge candidates). Keep merging upstream reach by reach while all three hold; stop at the first candidate that fails any of them.
8. Write the modified network and the lakes layer actually used.
9. Write `network.json` last.

## Artifacts

| Artifact                                        | Description                                                                                          |
| ----------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `base_output_path/<identity_hash>/network.gpkg` | Modified reach network (flowpaths layer) — what `build_model`'s `db_uri` points at. Carries `is_headwater`, `is_terminal`, `terminal_reason` (coastal breaks persist only as `terminal_reason='coast'` — no separate coastal inlet/outlet/encompassed tags); `waterbody_inlet`/`outlet` (lakes) |
| `base_output_path/<identity_hash>/lakes.gpkg`   | Filtered + buffered lake polygons actually used for classification (QC/reference only — not inserted as network reaches, per DR-037 ALT-B) |
| `base_output_path/<identity_hash>/network.json` | Network definition and artifact inventory — see `network.schema.json`                                |

## Response

- `identity_hash` — str

## Out of Scope

- Building any single reach's Model (`build_model`'s job) — including the lake-outlet inflow BC offset DR-007 step 4 flags as informed by these tags but decided elsewhere.
- Producing or maintaining the lakes, coastal vector, or coastal raster source datasets themselves.
- Anything downstream-of-network scenario/run logic.

## Dependencies

- Python
- GeoPandas / Shapely / GDAL
- AWS CLI

## Errors

- Source network, lakes, or coastal dataset unavailable — raises `DatasetUnavailableError` (prototype currently raises `FileNotFoundError` directly for lakes/coastal; job-wrapping should normalize to the shared exception set)
- `coasts_vector_path` not given and not derivable from `coasts_raster_path` (neither resolves to a real file) — raises `DatasetUnavailableError`. If it resolves to the raster only, the prototype raises `NotImplementedError` on the raster-to-vector conversion path — not yet a real coastal input mode
- Output artifacts cannot be written — raises `WriteFailureError`

## Checks

- network already exists at output path — return immediately with warning `network_exists`
- reach flagged by both lake and coastal logic — warning `ambiguous_reach_classification`, keep processing (a real possibility given coastal and lake processing run sequentially over the same reach set)

## Metrics

Recorded in `network.json` under `properties` (see `network.schema.json`) — one counter per processing branch above, so a run is auditable against the code path it took:

- `n_reaches_input`, `n_reaches_output`
- `n_reaches_below_stream_order_removed`
- `n_reaches_encompassed_removed_lake`, `n_reaches_encompassed_removed_coastal`
- `n_reaches_trimmed_inlet_lake`, `n_reaches_trimmed_outlet_lake`
- `n_reaches_trimmed_inlet_coastal` — downstream end inside coastal coverage, upstream not (step 4's second case)
- `n_reaches_dropped_coastal_cascade` — reaches removed for being downstream of a coastal encompassed/trimmed reach, not for their own classification
- `n_reaches_split_passthrough_lake`
- `n_reaches_merged`
- `n_headwater_reaches`, `n_terminal_reaches`

## Performance

Runs over the entire network in one call, not per reach — expect the longest-running job in the system and infrequent (once per hydrofabric release or methodology/threshold change).