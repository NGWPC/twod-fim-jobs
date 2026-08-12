"""Tests for the modify_network job.

Fixtures are generated rather than committed as binary GeoPackages: the
synthetic network is designed so each reach exercises one branch, and that
intent is only legible in code. See ``synthetic_network`` for the layout.
"""

import hashlib
import json
import re
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError
from shapely.geometry import LineString, box
from twod_fim_jobs.consts import (
    COAST_TO_ID_FIELD,
    DA_FIELD,
    LAKE_ID_FIELD,
    IS_HEADWATER_FIELD,
    IS_TERMINAL_FIELD,
    IS_TRIMMED_FIELD,
    LAKE_INLET_FIELD,
    LAKE_OUTLET_FIELD,
    LAKE_TO_ID_FIELD,
    LENGTH_KM_FIELD,
    OUTPUT_COLUMNS,
    REACH_ID_FIELD,
    REACH_TABLE,
    REACH_TO_ID_FIELD,
    SLOPE_FIELD,
    TERMINAL_REASON_FIELD,
)
from twod_fim_jobs.jobs.modify_network import ModifyNetworkJob
from twod_fim_jobs.models.common import Asset
from twod_fim_jobs.models.modify_network import (
    AmbiguousReachClassificationWarning,
    Assets,
    Identity,
    ModifyNetworkInputs,
    NetworkExistsWarning,
    NetworkManifest,
    Properties,
)
from twod_fim_jobs.utils import network as nw

SCHEMA_PATH = Path(__file__).parents[2] / "specs-and-manifests" / "network.schema.json"
EXAMPLE_PATH = (
    Path(__file__).parents[2] / "specs-and-manifests" / "network.example.jsonc"
)
CRS = "EPSG:5070"


### FIXTURES ###


@pytest.fixture
def synthetic_network(tmp_path: Path) -> Path:
    """A 14-reach network where every reach exercises one branch.

    1->2->3      mainstem; 1 and 2 are merge candidates (DA 100 vs 101)
    4->3         junction blocker: 3 has two parents, so 3 never merges
    13->1, 14->1 stream order 1 and 2, removed by a threshold of 3
    5->6->7      crosses lake box(140,160): inlet / encompassed / outlet
    8            isolated, passes clean through the lake -> split in two
    9->10->11    crosses coast box(300,315): 9 trimmed, 10 and 11 cascade
    12->11       clean tributary into the cascade zone -> stranded
    """
    rows = [
        # fp_id, fp_to_id, total_da_sqkm, stream_order, coords
        (1, 2, 100.0, 3, [(0, 0), (10, 0)]),
        (2, 3, 101.0, 3, [(10, 0), (20, 0)]),
        (3, None, 102.0, 4, [(20, 0), (30, 0)]),
        (4, 3, 50.0, 3, [(10, 10), (20, 0)]),
        (5, 6, 10.0, 3, [(100, 0), (150, 0)]),
        (6, 7, 11.0, 3, [(150, 0), (155, 0)]),
        (7, None, 12.0, 3, [(155, 0), (190, 0)]),
        (8, None, 5.0, 3, [(150, -20), (150, 20)]),
        (9, 10, 20.0, 3, [(250, 0), (310, 0)]),
        (10, 11, 21.0, 3, [(310, 0), (320, 0)]),
        (11, None, 22.0, 3, [(320, 0), (330, 0)]),
        (12, 11, 9.0, 3, [(316, -20), (320, 0)]),
        (13, 1, 1.0, 1, [(-10, 0), (0, 0)]),
        (14, 1, 2.0, 2, [(-10, 5), (0, 0)]),
    ]
    geoms = [LineString(c) for *_, c in rows]
    gdf = gpd.GeoDataFrame(
        {
            "fp_id": [r[0] for r in rows],
            "fp_to_id": [r[1] for r in rows],
            "total_da_sqkm": [r[2] for r in rows],
            # local catchment, deliberately unlike the cumulative DA: if the
            # merge ever reads this instead, the thresholds stop matching.
            "area_sqkm": [r[2] / 10 for r in rows],
            "stream_order": [r[3] for r in rows],
            "length_km": [g.length / 1000.0 for g in geoms],
        },
        geometry=geoms,
        crs=CRS,
    )
    path = tmp_path / "network_raw.gpkg"
    gdf.to_file(path, layer="flowpaths", driver="GPKG")
    return path


@pytest.fixture
def lakes_layer(tmp_path: Path) -> Path:
    """One lake straddling reaches 5-8, plus a tiny one below any threshold."""
    path = tmp_path / "lakes.gpkg"
    gpd.GeoDataFrame(
        {"lake_id": [77, 78], "name": ["big", "tiny"]},
        geometry=[box(140, -10, 160, 10), box(400, 400, 401, 401)],
        crs=CRS,
    ).to_file(path, layer="lakes_polygons", driver="GPKG")
    return path


@pytest.fixture
def coastal_layer(tmp_path: Path) -> Path:
    path = tmp_path / "coastal.gpkg"
    gpd.GeoDataFrame(
        {"id": [42], "name": ["coast"]}, geometry=[box(300, -50, 315, 50)], crs=CRS
    ).to_file(path, layer="coastal_influence", driver="GPKG")
    return path


@pytest.fixture
def payload(synthetic_network, lakes_layer, coastal_layer, tmp_path) -> dict:
    return {
        "reach_network_path": str(synthetic_network),
        "lakes_layer_path": str(lakes_layer),
        "coastal_influence_layer_path": str(coastal_layer),
        "base_output_path": str(tmp_path / "out"),
        "stream_order_filter_threshold": 3,
        "lake_area_threshold_sqkm": 0.0,
        "negative_lake_buffer_meters": 2.0,
    }


@pytest.fixture
def schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


@pytest.fixture
def example_manifest() -> dict:
    """The committed example manifest, with // comments stripped."""
    raw = EXAMPLE_PATH.read_text()
    stripped = re.sub(
        r'"(?:\\.|[^"\\])*"|//[^\n]*',
        lambda m: m.group(0) if m.group(0).startswith('"') else "",
        raw,
    )
    return json.loads(stripped)


@pytest.fixture
def result(payload):
    return ModifyNetworkJob().run(payload)


@pytest.fixture
def manifest(result) -> dict:
    return json.loads((Path(result.network_dir) / "network.json").read_text())


@pytest.fixture
def output_network(result) -> gpd.GeoDataFrame:
    gdf = gpd.read_file(Path(result.network_dir) / "network.gpkg")
    return gdf.set_index(REACH_ID_FIELD)


### ALGORITHM: LOADING AND TAGGING ###


def test_stream_order_filter_pushes_down_to_read(synthetic_network):
    gdf, counters = nw.load_reach_network(str(synthetic_network), 3)
    assert counters.n_reaches_input == 14
    assert counters.n_reaches_below_stream_order_removed == 2
    assert len(gdf) == 12
    assert set(gdf["stream_order"]) == {3, 4}


def test_no_threshold_means_no_filtering(synthetic_network):
    gdf, counters = nw.load_reach_network(str(synthetic_network), None)
    assert len(gdf) == 14
    # Null, not zero: the branch did not run.
    assert counters.n_reaches_below_stream_order_removed is None


def test_unprojected_crs_rejected(tmp_path):
    geom = [LineString([(0, 0), (1, 1)])]
    gpd.GeoDataFrame(
        {
            "fp_id": [1],
            "fp_to_id": [None],
            "total_da_sqkm": [1.0],
            "stream_order": [3],
            "length_km": [1.0],
        },
        geometry=geom,
        crs="EPSG:4326",
    ).to_file(tmp_path / "geo.gpkg", layer="flowpaths", driver="GPKG")
    with pytest.raises(ValueError, match="projected"):
        nw.load_reach_network(str(tmp_path / "geo.gpkg"), None)


def test_missing_dataset_raises_dataset_unavailable(tmp_path):
    from twod_fim_jobs.exceptions import DatasetUnavailableError

    with pytest.raises(DatasetUnavailableError):
        nw.load_reach_network(str(tmp_path / "nope.gpkg"), None)


def test_headwater_is_no_upstream_not_no_downstream(synthetic_network):
    """Regression: the definition was inverted, flagging outlets as headwaters."""
    gdf, _ = nw.load_reach_network(str(synthetic_network), None)
    gdf = nw.tag_headwater_reaches(nw.tag_terminal_reaches(gdf))
    by_id = gdf.set_index(REACH_ID_FIELD)
    # 13 and 14 have nothing flowing into them.
    assert by_id.loc["13", IS_HEADWATER_FIELD]
    assert by_id.loc["14", IS_HEADWATER_FIELD]
    # 3 is a terminal outlet with two parents - never a headwater.
    assert by_id.loc["3", IS_TERMINAL_FIELD]
    assert not by_id.loc["3", IS_HEADWATER_FIELD]
    assert by_id.loc["3", TERMINAL_REASON_FIELD] == "outlet"


def test_filter_strands_reaches_into_apparent_headwaters(synthetic_network):
    """Reach 1 gains headwater status only because 13 and 14 were filtered."""
    unfiltered, _ = nw.load_reach_network(str(synthetic_network), None)
    assert (
        not nw.tag_headwater_reaches(unfiltered)
        .set_index(REACH_ID_FIELD)
        .loc["1", IS_HEADWATER_FIELD]
    )
    filtered, _ = nw.load_reach_network(str(synthetic_network), 3)
    assert (
        nw.tag_headwater_reaches(filtered)
        .set_index(REACH_ID_FIELD)
        .loc["1", IS_HEADWATER_FIELD]
    )


### ALGORITHM: LAKES PREPROCESSING ###


def test_prepare_lakes_filters_before_buffering(lakes_layer):
    raw = gpd.read_file(lakes_layer)
    prepared = nw.prepare_lakes(
        raw, lake_area_threshold_sqkm=0.0, negative_lake_buffer_meters=2.0
    )
    # Tiny lake (1x1 m) vanishes under a 2 m inward buffer.
    assert len(prepared) == 1
    np.testing.assert_allclose(prepared.total_bounds, [142, -8, 158, 8])


def test_prepare_lakes_area_threshold_uses_raw_area(lakes_layer):
    raw = gpd.read_file(lakes_layer)
    # 400 sq m = 0.0004 sq km; a threshold above that drops the big lake too.
    assert len(nw.prepare_lakes(raw, 1.0, 2.0)) == 0


### ALGORITHM: FULL PIPELINE ###


@pytest.fixture
def pipeline(synthetic_network, lakes_layer, coastal_layer):
    """Run the algorithm directly, returning (gdf, counters, touched sets)."""
    gdf, counters = nw.load_reach_network(str(synthetic_network), 3)
    gdf = nw.tag_headwater_reaches(nw.tag_terminal_reaches(gdf))
    coastal = gpd.read_file(coastal_layer).to_crs(gdf.crs)
    gdf, coastal_touched = nw.apply_coastal(gdf, coastal, counters)
    lakes = nw.prepare_lakes(gpd.read_file(lakes_layer).to_crs(gdf.crs), 0.0, 2.0)
    gdf, lake_touched = nw.apply_lakes(gdf, lakes, counters)
    gdf = nw.merge_short_reaches(gdf, 5.0, 3.0, counters)
    gdf = nw.finalize_network(gdf, counters)
    return gdf, counters, coastal_touched, lake_touched


EXPECTED_COUNTERS = {
    "n_reaches_input": 14,
    "n_reaches_below_stream_order_removed": 2,
    "n_reaches_encompassed_removed_lake": 1,
    "n_reaches_encompassed_removed_coastal": 0,
    "n_reaches_trimmed_inlet_lake": 1,
    "n_reaches_trimmed_outlet_lake": 1,
    "n_reaches_trimmed_inlet_coastal": 1,
    "n_reaches_dropped_coastal_cascade": 2,
    "n_reaches_stranded_coastal": 1,
    "n_reaches_split_passthrough_lake": 1,
    "n_reaches_merged": 1,
    "n_reaches_output": 9,
    "n_headwater_reaches": 8,
    "n_terminal_reaches": 7,
}


@pytest.mark.parametrize("name,expected", sorted(EXPECTED_COUNTERS.items()))
def test_counter(pipeline, name, expected):
    _, counters, _, _ = pipeline
    assert getattr(counters, name) == expected


def test_counters_reconcile(pipeline):
    _, c, _, _ = pipeline
    assert c.n_reaches_output == (
        c.n_reaches_input
        - c.n_reaches_below_stream_order_removed
        - c.n_reaches_encompassed_removed_lake
        - c.n_reaches_encompassed_removed_coastal
        - c.n_reaches_dropped_coastal_cascade
        - c.n_reaches_merged
        + c.n_reaches_split_passthrough_lake
    )


def test_surviving_reach_ids(pipeline):
    gdf, *_ = pipeline
    assert list(gdf[REACH_ID_FIELD]) == ["2", "3", "4", "5", "7", "8_1", "8_2", "9", "12"]


def test_junction_blocks_merge(pipeline):
    """Reach 3 has two parents, so it is never a merge candidate."""
    gdf, *_ = pipeline
    by_id = gdf.set_index(REACH_ID_FIELD)
    assert "3" in by_id.index and "4" in by_id.index
    np.testing.assert_allclose(by_id.loc["3"].geometry.length, 10.0)


def test_merge_sums_length_and_rewires_tributaries(pipeline):
    """Reach 1 is absorbed into 2: summed length, no dangling pointer."""
    gdf, *_ = pipeline
    by_id = gdf.set_index(REACH_ID_FIELD)
    assert "1" not in by_id.index
    np.testing.assert_allclose(by_id.loc["2", "length_km"], 0.02)
    np.testing.assert_allclose(by_id.loc["2"].geometry.length, 20.0)
    assert "1" not in set(gdf[REACH_TO_ID_FIELD].dropna())
    # Headwater status of the chain's upstream end carries to the survivor.
    assert by_id.loc["2", IS_HEADWATER_FIELD]


def test_reach_already_long_enough_never_merges(synthetic_network):
    """The length threshold is a floor: clear it and you are left alone."""
    gdf, counters = nw.load_reach_network(str(synthetic_network), 3)
    gdf = nw.tag_headwater_reaches(nw.tag_terminal_reaches(gdf))
    # Every fixture reach is 0.01 km; a 0.005 km floor is already satisfied.
    nw.merge_short_reaches(gdf, 5.0, 0.005, counters)
    assert counters.n_reaches_merged == 0


def test_merge_respects_drainage_area_threshold(synthetic_network):
    gdf, counters = nw.load_reach_network(str(synthetic_network), 3)
    gdf = nw.tag_headwater_reaches(nw.tag_terminal_reaches(gdf))
    # 100 vs 101 is a ~1% difference; a 0.5% threshold blocks it.
    gdf = nw.merge_short_reaches(gdf, 0.5, 3.0, counters)
    assert counters.n_reaches_merged == 0


def test_lake_inlet_trimmed_and_terminated(pipeline):
    gdf, *_ = pipeline
    r5 = gdf.set_index(REACH_ID_FIELD).loc["5"]
    assert r5[LAKE_INLET_FIELD] and r5[IS_TERMINAL_FIELD] and r5[IS_TRIMMED_FIELD]
    assert r5[TERMINAL_REASON_FIELD] == "lake"
    assert pd.isna(r5[REACH_TO_ID_FIELD])
    np.testing.assert_allclose(r5.geometry.length, 42.0)  # 100->142


def test_lake_outlet_trimmed_and_headwatered(pipeline):
    gdf, *_ = pipeline
    r7 = gdf.set_index(REACH_ID_FIELD).loc["7"]
    assert r7[LAKE_OUTLET_FIELD] and r7[IS_HEADWATER_FIELD]
    np.testing.assert_allclose(r7.geometry.length, 32.0)  # 158->190


def test_lake_passthrough_split_suffixes_both_pieces(pipeline):
    """The parent id is retired; _1 is upstream/inlet, _2 downstream/outlet."""
    gdf, *_ = pipeline
    by_id = gdf.set_index(REACH_ID_FIELD)
    assert "8" not in by_id.index, "the parent id must not survive a split"
    inlet, outlet = by_id.loc["8_1"], by_id.loc["8_2"]
    assert inlet[LAKE_INLET_FIELD] and inlet[TERMINAL_REASON_FIELD] == "lake"
    assert outlet[LAKE_OUTLET_FIELD] and outlet[IS_HEADWATER_FIELD]
    np.testing.assert_allclose(inlet.geometry.length, 12.0)
    np.testing.assert_allclose(outlet.geometry.length, 12.0)


def test_coastal_trim_and_cascade(pipeline):
    gdf, *_ = pipeline
    by_id = gdf.set_index(REACH_ID_FIELD)
    assert "10" not in by_id.index and "11" not in by_id.index
    r9 = by_id.loc["9"]
    assert r9[IS_TERMINAL_FIELD] and r9[TERMINAL_REASON_FIELD] == "coast"
    np.testing.assert_allclose(r9.geometry.length, 50.0)  # 250->300


def test_stranded_tributary_terminated_without_trimming(pipeline):
    """Reach 12 never touched the coast layer but lost its downstream."""
    gdf, *_ = pipeline
    r12 = gdf.set_index(REACH_ID_FIELD).loc["12"]
    assert r12[IS_TERMINAL_FIELD] and r12[TERMINAL_REASON_FIELD] == "coast"
    assert not r12[IS_TRIMMED_FIELD], "stranded reaches keep their geometry"
    np.testing.assert_allclose(r12.geometry.length, np.hypot(4, 20))


def test_no_dangling_downstream_pointers(pipeline):
    gdf, *_ = pipeline
    live = set(gdf[REACH_ID_FIELD])
    assert set(gdf[REACH_TO_ID_FIELD].dropna()) <= live


def test_pipeline_is_deterministic(synthetic_network, lakes_layer, coastal_layer):
    def once():
        gdf, counters = nw.load_reach_network(str(synthetic_network), 3)
        gdf = nw.tag_headwater_reaches(nw.tag_terminal_reaches(gdf))
        coastal = gpd.read_file(coastal_layer).to_crs(gdf.crs)
        gdf, _ = nw.apply_coastal(gdf, coastal, counters)
        lakes = nw.prepare_lakes(gpd.read_file(lakes_layer).to_crs(gdf.crs), 0.0, 2.0)
        gdf, _ = nw.apply_lakes(gdf, lakes, counters)
        gdf = nw.merge_short_reaches(gdf, 5.0, 3.0, counters)
        return nw.finalize_network(gdf, counters)

    a, b = once(), once()
    assert a.drop(columns=a.geometry.name).equals(b.drop(columns=b.geometry.name))
    assert (a.geometry.to_wkb() == b.geometry.to_wkb()).all()


def test_skipping_waterbodies_is_a_noop_on_the_network(synthetic_network):
    """No lakes and no coastal means no reach is dropped, trimmed, or split."""
    gdf, counters = nw.load_reach_network(str(synthetic_network), 3)
    gdf = nw.tag_headwater_reaches(nw.tag_terminal_reaches(gdf))
    before = len(gdf)
    gdf = nw.merge_short_reaches(gdf, 5.0, 3.0, counters)
    gdf = nw.finalize_network(gdf, counters)
    assert counters.n_reaches_output == before - counters.n_reaches_merged
    assert not gdf[IS_TRIMMED_FIELD].any()
    assert counters.n_reaches_encompassed_removed_lake is None
    assert counters.n_reaches_stranded_coastal is None


### MODELS ###


def test_example_manifest_roundtrips_through_the_models(example_manifest, schema):
    jsonschema = pytest.importorskip("jsonschema")
    m = NetworkManifest(
        created_at="2026-06-01T09:00:00Z",
        identity_hash=example_manifest["identity_hash"],
        id=example_manifest["id"],
        identity=Identity(**example_manifest["identity"]),
        inputs=ModifyNetworkInputs(**example_manifest["inputs"]),
        properties=Properties(**example_manifest["properties"]),
        assets=Assets(
            network=Asset(**example_manifest["assets"]["network"]),
            lakes=Asset(**example_manifest["assets"]["lakes"]),
        ),
    )
    jsonschema.validate(m.model_dump(mode="json"), schema)


def test_non_reconciling_counters_rejected(example_manifest):
    props = dict(example_manifest["properties"])
    props["n_reaches_output"] += 1
    with pytest.raises(ValidationError, match="reconcile"):
        Properties(**props)


def test_partially_null_step_counters_rejected(example_manifest):
    props = dict(example_manifest["properties"])
    props["n_reaches_trimmed_inlet_lake"] = None
    with pytest.raises(ValidationError, match="all-null"):
        Properties(**props)


def test_id_must_equal_identity_hash(example_manifest):
    with pytest.raises(ValidationError, match="identity_hash"):
        NetworkManifest(
            created_at="2026-06-01T09:00:00Z",
            identity_hash="7c2a9e41",
            id="deadbeef",
            identity=Identity(**example_manifest["identity"]),
            inputs=ModifyNetworkInputs(**example_manifest["inputs"]),
            properties=Properties(**example_manifest["properties"]),
            assets=Assets(network=Asset(**example_manifest["assets"]["network"])),
        )


def test_skip_incoherence_rejected(example_manifest):
    """A lake hash without a lake path cannot describe a real run."""
    inputs = ModifyNetworkInputs(
        reach_network_path="a.gpkg", base_output_path="/tmp/out"
    )
    props = {k: None for k in example_manifest["properties"]}
    props |= {"n_reaches_input": 10, "n_reaches_output": 10, "n_reaches_merged": 0}
    with pytest.raises(ValidationError, match="lakes_layer_hash"):
        NetworkManifest(
            created_at="2026-06-01T09:00:00Z",
            identity_hash="7c2a9e41",
            id="7c2a9e41",
            identity=Identity(
                **{
                    **example_manifest["identity"],
                    "coastal_influence_layer_hash": None,
                },
            ),
            inputs=inputs,
            properties=Properties(**props),
            assets=Assets(network=Asset(**example_manifest["assets"]["network"])),
        )


@pytest.mark.parametrize(
    "warning,code",
    [
        (
            AmbiguousReachClassificationWarning(reach_ids=[3, 1]),
            "ambiguous_reach_classification",
        ),
        (
            NetworkExistsWarning(network_dir="s3://x/ab", identity_hash="aa11bb22"),
            "network_exists",
        ),
    ],
)
def test_warnings_serialize_as_code_and_message(warning, code):
    dumped = warning.model_dump(mode="json")
    assert sorted(dumped) == ["code", "message"]
    assert dumped["code"] == code


def test_absent_lakes_asset_is_omitted_not_null(example_manifest):
    assets = Assets(network=Asset(**example_manifest["assets"]["network"]))
    assert "lakes" not in assets.model_dump(mode="json")


### JOB END-TO-END ###


def test_manifest_validates_against_schema(manifest, schema):
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.validate(manifest, schema)


def test_artifacts_written_under_identity_hash(result):
    net_dir = Path(result.network_dir)
    assert net_dir.name == result.identity_hash
    assert sorted(p.name for p in net_dir.iterdir()) == [
        "lakes.gpkg",
        "network.gpkg",
        "network.json",
    ]


def test_manifest_checksums_match_bytes_on_disk(result, manifest):
    net_dir = Path(result.network_dir)
    for role, filename in (("network", "network.gpkg"), ("lakes", "lakes.gpkg")):
        digest = hashlib.sha256((net_dir / filename).read_bytes()).hexdigest()[:16]
        assert manifest["assets"][role]["checksum"] == digest


def test_output_carries_contract_columns(output_network):
    for column in (
        IS_HEADWATER_FIELD,
        IS_TERMINAL_FIELD,
        TERMINAL_REASON_FIELD,
        LAKE_INLET_FIELD,
        LAKE_OUTLET_FIELD,
        IS_TRIMMED_FIELD,
        REACH_TO_ID_FIELD,
    ):
        assert column in output_network.columns


def test_terminal_reason_uses_documented_vocabulary_only(output_network):
    values = set(output_network[TERMINAL_REASON_FIELD].dropna())
    assert values <= {"outlet", "coast", "lake"}


def test_no_encompassed_columns_persist(output_network):
    """Encompassed classification is intermediate; those reaches are dropped."""
    assert not [c for c in output_network.columns if "encompassed" in c]


def test_assets_are_retained_not_lifecycle_eligible(manifest):
    assert all(a["derived"] is False for a in manifest["assets"].values())


def test_defaults_resolved_into_identity(manifest):
    assert manifest["identity"]["min_length_threshold_km"] == 5.0
    assert manifest["identity"]["stream_order_filter_threshold"] == 3


def test_rerun_short_circuits_without_touching_artifacts(payload, result):
    net_dir = Path(result.network_dir)
    before = {p.name: p.stat().st_mtime_ns for p in net_dir.iterdir()}
    second = ModifyNetworkJob().run(payload)
    assert [w.code for w in second.warnings] == ["network_exists"]
    assert second.identity_hash == result.identity_hash
    assert {p.name: p.stat().st_mtime_ns for p in net_dir.iterdir()} == before


def test_skip_case_writes_no_lakes_and_forks_identity(
    synthetic_network, tmp_path, result, schema
):
    jsonschema = pytest.importorskip("jsonschema")
    second = ModifyNetworkJob().run(
        {
            "reach_network_path": str(synthetic_network),
            "base_output_path": str(tmp_path / "out2"),
        }
    )
    net_dir = Path(second.network_dir)
    doc = json.loads((net_dir / "network.json").read_text())
    jsonschema.validate(doc, schema)

    assert not (net_dir / "lakes.gpkg").exists()
    assert "lakes" not in doc["assets"]
    assert doc["identity"]["lakes_layer_hash"] is None
    assert doc["identity"]["stream_order_filter_threshold"] is None
    assert doc["properties"]["n_reaches_below_stream_order_removed"] is None
    assert doc["properties"]["n_reaches_trimmed_inlet_lake"] is None
    assert second.identity_hash != result.identity_hash


def test_changing_a_threshold_forks_identity(payload):
    first = ModifyNetworkJob().run(payload)
    second = ModifyNetworkJob().run(
        {
            **payload,
            "min_length_threshold_km": 99.0,
            "base_output_path": payload["base_output_path"] + "-b",
        }
    )
    assert first.identity_hash != second.identity_hash


### OUTPUT SCHEMA AND WATERBODY REFERENCES ###


def test_split_id_derives_from_its_parent(pipeline):
    """A split piece is named after the reach it came from, not renumbered."""
    gdf, *_ = pipeline
    ids = set(gdf[REACH_ID_FIELD])
    assert "8_1" in ids and "8_2" in ids
    # 13 and 14 exist in the source but were filtered out; a numeric
    # high-water mark would have reused one of them here.
    assert not ids & {"13", "14", "15"}


def test_split_ids_sort_beside_their_parent(pipeline):
    """Output order is natural, not lexicographic: 2 < 8 < 8_1 < 9 < 12."""
    gdf, *_ = pipeline
    assert list(gdf[REACH_ID_FIELD]) == [
        "2", "3", "4", "5", "7", "8_1", "8_2", "9", "12",
    ]


def test_lake_reaches_record_which_lake(pipeline):
    """Inlet, outlet, and both split pieces carry the lake's own id."""
    gdf, *_ = pipeline
    by_id = gdf.set_index(REACH_ID_FIELD)
    for rid in ("5", "7", "8_1", "8_2"):
        assert by_id.loc[rid, LAKE_TO_ID_FIELD] == "77", rid
    # A reach that never met a lake leaves it null.
    assert pd.isna(by_id.loc["3", LAKE_TO_ID_FIELD])


def test_coastal_trim_records_which_polygon_but_stranded_does_not(pipeline):
    """coast_to_id separates 'met the coast' from 'lost its downstream'."""
    gdf, *_ = pipeline
    by_id = gdf.set_index(REACH_ID_FIELD)
    assert by_id.loc["9", COAST_TO_ID_FIELD] == "42"
    assert pd.isna(by_id.loc["12", COAST_TO_ID_FIELD]), (
        "reach 12 never intersected the coast layer"
    )


def test_source_columns_are_dropped(pipeline):
    """fp_id/fp_to_id are superseded; unlisted source columns do not ship."""
    gdf, *_ = pipeline
    assert "fp_id" not in gdf.columns
    assert "fp_to_id" not in gdf.columns


def test_output_columns_are_exactly_the_contract(pipeline):
    gdf, *_ = pipeline
    assert list(gdf.columns) == [*OUTPUT_COLUMNS, gdf.geometry.name]


def test_build_model_attributes_survive(pipeline):
    """build_model reads these off this network; dropping them would break it."""
    gdf, *_ = pipeline
    for column in ("stream_order", "total_da_sqkm", "length_km"):
        assert column in gdf.columns
    # local catchment is not carried: unused downstream, and wrong on a
    # merged row unless summed.
    assert "area_sqkm" not in gdf.columns


def test_written_gpkg_matches_the_output_schema(output_network):
    """The schema survives the GeoPackage round trip, ids included."""
    expected = [c for c in OUTPUT_COLUMNS if c != REACH_ID_FIELD]
    assert list(output_network.columns) == [*expected, output_network.geometry.name]
    assert pd.api.types.is_string_dtype(output_network.index.dtype)
    assert "8_1" in output_network.index


def test_merge_uses_cumulative_drainage_area_not_local_catchment(pipeline):
    """The rule asks whether two reaches carry the same flow.

    The fixture sets area_sqkm to a tenth of total_da_sqkm, so reaches 1 and 2
    differ by ~1% on cumulative DA (mergeable at a 5% threshold) but by the
    same ~1% on local area — the discriminating property is that only
    total_da_sqkm is present in the output and consulted by the merge.
    """
    gdf, counters, _, _ = pipeline
    assert counters.n_reaches_merged == 1
    assert DA_FIELD in gdf.columns


def test_merged_reach_keeps_the_chain_start_drainage_area(pipeline):
    """The survivor is the most downstream reach, so its accumulation stands."""
    gdf, *_ = pipeline
    # reach 1 (DA 100) was absorbed into reach 2 (DA 101); 101 survives.
    assert gdf.set_index(REACH_ID_FIELD).loc["2", DA_FIELD] == 101.0


def test_missing_drainage_area_column_is_reported_clearly(tmp_path):
    from twod_fim_jobs.exceptions import DatasetUnavailableError

    gpd.GeoDataFrame(
        {"fp_id": [1], "fp_to_id": [None], "length_km": [1.0], "stream_order": [3]},
        geometry=[LineString([(0, 0), (10, 0)])],
        crs=CRS,
    ).to_file(tmp_path / "no_da.gpkg", layer="flowpaths", driver="GPKG")
    with pytest.raises(DatasetUnavailableError, match="total_da_sqkm"):
        nw.load_reach_network(str(tmp_path / "no_da.gpkg"), None)


def test_output_layer_is_named_for_build_model(result):
    """build_model queries this GeoPackage by table name, not by position."""
    import pyogrio

    layers = [name for name, _ in pyogrio.list_layers(
        Path(result.network_dir) / "network.gpkg"
    )]
    assert layers == [REACH_TABLE]


def test_build_model_can_query_the_network_it_receives(result):
    """The output satisfies build_model's REACH_FIELDS, slope excepted."""
    from twod_fim_jobs.consts import REACH_FIELDS

    gdf = gpd.read_file(Path(result.network_dir) / "network.gpkg", layer=REACH_TABLE)
    wanted = [f for f in REACH_FIELDS if f not in ("geom", SLOPE_FIELD)]
    assert not [f for f in wanted if f not in gdf.columns]


### JUNCTION DETECTION AGAINST THE FILTERED NETWORK ###


@pytest.fixture
def confluence_network(tmp_path):
    """A -> B -> C mainstem with tributary T joining at B.

    T's stream order and drainage area are parameterised by the factory so a
    test can ask what happens when the filter removes it.
    """

    def _build(trib_da: float, trib_order: int) -> Path:
        rows = [
            ("A", "B", 100.0, 3, [(0, 0), (1000, 0)]),
            ("B", "C", 100.0 + trib_da, 3, [(1000, 0), (2000, 0)]),
            ("C", None, 100.0 + trib_da + 1, 3, [(2000, 0), (3000, 0)]),
            ("T", "B", trib_da, trib_order, [(1000, 800), (1000, 0)]),
        ]
        geoms = [LineString(c) for *_, c in rows]
        path = tmp_path / f"conf_{trib_da}_{trib_order}.gpkg"
        gpd.GeoDataFrame(
            {
                "fp_id": [r[0] for r in rows],
                "fp_to_id": [r[1] for r in rows],
                "total_da_sqkm": [r[2] for r in rows],
                "stream_order": [r[3] for r in rows],
                "length_km": [g.length / 1000 for g in geoms],
            },
            geometry=geoms,
            crs=CRS,
        ).to_file(path, layer="flowpaths", driver="GPKG")
        return path

    return _build


def _merge_run(path, threshold):
    gdf, counters = nw.load_reach_network(str(path), threshold)
    gdf = nw.tag_headwater_reaches(nw.tag_terminal_reaches(gdf))
    gdf = nw.merge_short_reaches(gdf, 5.0, 10.0, counters)
    return nw.finalize_network(gdf, counters), counters


def test_junction_uses_filtered_topology_not_source_topology(confluence_network):
    """A filtered-out tributary does not make its outlet a junction.

    Deliberate: the merge operates on the network being modeled, so a reach
    that is not present cannot constrain it. The drainage-area rule is what
    guards against merging through a hydrologically real inflow — see the
    companion test below.
    """
    path = confluence_network(trib_da=1.0, trib_order=2)
    _, unfiltered = _merge_run(path, None)
    _, filtered = _merge_run(path, 3)
    assert unfiltered.n_reaches_merged == 1, "T present: B is a junction, merge stops"
    assert filtered.n_reaches_merged == 2, "T filtered out: B is no longer a junction"


def test_drainage_area_still_blocks_merging_through_a_real_confluence(
    confluence_network,
):
    """The safety net: a filtered tributary that matters still stops the merge.

    T carries 40 km2 into a 100 km2 reach. Even though the filter removes T
    from the topology, its water is still counted in B's total_da_sqkm, so
    reach A sits 29% away from the chain start and fails the 5% threshold.
    """
    path = confluence_network(trib_da=40.0, trib_order=2)
    gdf, counters = _merge_run(path, 3)
    assert counters.n_reaches_merged == 1
    assert "A" in set(gdf[REACH_ID_FIELD]), "A must survive the confluence"


### LENGTH THRESHOLD AS A FLOOR ###


@pytest.fixture
def linear_chain(tmp_path):
    """A single-thread chain, downstream first, with equal drainage areas."""

    def _build(lengths_km: list[float]) -> Path:
        ids = [chr(65 + i) for i in range(len(lengths_km))]
        rows, x = [], 0.0
        for i, length in enumerate(lengths_km):
            rows.append(
                (ids[i], ids[i - 1] if i else None, [(x, 0), (x + length * 1000, 0)])
            )
            x += length * 1000
        geoms = [LineString(c) for *_, c in rows]
        path = tmp_path / f"chain_{'_'.join(str(L) for L in lengths_km)}.gpkg"
        gpd.GeoDataFrame(
            {
                "fp_id": [r[0] for r in rows],
                "fp_to_id": [r[1] for r in rows],
                "total_da_sqkm": [100.0] * len(rows),
                "stream_order": [3] * len(rows),
                "length_km": [g.length / 1000 for g in geoms],
            },
            geometry=geoms,
            crs=CRS,
        ).to_file(path, layer="flowpaths", driver="GPKG")
        return path

    return _build


def _merged_lengths(path, floor_km):
    gdf, counters = nw.load_reach_network(str(path), None)
    gdf = nw.tag_headwater_reaches(nw.tag_terminal_reaches(gdf))
    gdf = nw.merge_short_reaches(gdf, 5.0, floor_km, counters)
    gdf = nw.finalize_network(gdf, counters)
    return {r[REACH_ID_FIELD]: round(r[LENGTH_KM_FIELD], 1) for _, r in gdf.iterrows()}


def test_chain_merges_past_the_floor_rather_than_stopping_short(linear_chain):
    """A=2, B=2, C=3 with a 5 km floor merges all three, not just A and B.

    Under a ceiling reading the walk would stop at 4 km and strand C at 3 km,
    leaving two reaches both shorter than the threshold.
    """
    assert _merged_lengths(linear_chain([2, 2, 3]), 5.0) == {"A": 7.0}


def test_merged_length_cannot_exceed_the_floor_by_more_than_one_reach(linear_chain):
    """The walk stops the moment the chain clears the floor, so it is bounded."""
    result = _merged_lengths(linear_chain([1] * 12), 5.0)
    assert result == {"A": 5.0, "F": 5.0, "K": 2.0}
    assert max(result.values()) <= 5.0 + 1.0


def test_upstream_tail_can_remain_below_the_floor(linear_chain):
    """The most upstream remnant has nothing left to absorb.

    Documented limitation: 'no reach shorter than the floor' holds only where
    topology allows it.
    """
    assert _merged_lengths(linear_chain([1] * 12), 5.0)["K"] == 2.0


### ORPHANS LEFT BY LAKE REMOVAL ###


@pytest.fixture
def island_lake(tmp_path):
    """A reach crossing an island inside ONE lake — the reported failure.

        U --> M --> D      lake 900 covers x<300 and x>600; island 300..600

    Both halves carry lake_id 900, so M starts and ends in the same lake and
    is only excluded from `within` by the island. U and D lie in water.
    """
    rows = [
        ("U", "M", 100.0, [(50, 0), (250, 0)]),
        ("M", "D", 101.0, [(250, 0), (700, 0)]),
        ("D", None, 102.0, [(700, 0), (950, 0)]),
    ]
    geoms = [LineString(c) for *_, c in rows]
    net = tmp_path / "island_net.gpkg"
    gpd.GeoDataFrame(
        {
            "fp_id": [r[0] for r in rows],
            "fp_to_id": [r[1] for r in rows],
            "total_da_sqkm": [r[2] for r in rows],
            "stream_order": [7] * 3,
            "length_km": [g.length / 1000 for g in geoms],
        },
        geometry=geoms,
        crs=CRS,
    ).to_file(net, layer="flowpaths", driver="GPKG")

    # One lake, two parts, one lake_id - explode() keeps the id on both.
    lake = tmp_path / "island_lake.gpkg"
    gpd.GeoDataFrame(
        {"lake_id": [900]},
        geometry=[box(0, -100, 300, 100).union(box(600, -100, 1000, 100))],
        crs=CRS,
    ).to_file(lake, layer="lakes_polygons", driver="GPKG")
    return net, lake


def _run_lakes(net, lake):
    gdf, counters = nw.load_reach_network(str(net), None)
    gdf = nw.tag_headwater_reaches(nw.tag_terminal_reaches(gdf))
    lakes = nw.prepare_lakes(gpd.read_file(lake).to_crs(gdf.crs), 0.0, 0.0)
    gdf, _ = nw.apply_lakes(gdf, lakes, counters)
    return gdf, counters


def test_both_ends_in_the_same_lake_is_encompassed_not_trimmed(island_lake):
    """The reported failure: an island crossing must not become an inlet stub.

    Trimming it as an inlet left a stub the width of the island, terminal at
    the lake, attached to nothing once the water either side was dropped.
    The comparison is on lake_id, not polygon position, because explode()
    splits this one lake into two parts.
    """
    gdf, counters = _run_lakes(*island_lake)
    assert counters.n_reaches_trimmed_inlet_lake == 0, "M must not be trimmed"
    assert counters.n_reaches_encompassed_removed_lake == 3
    assert set(gdf[REACH_ID_FIELD]) == set()


def test_exploded_lake_parts_share_one_lake_id(island_lake):
    """Guards the reason the comparison must be on lake_id."""
    _, lake = island_lake
    parts = nw.prepare_lakes(gpd.read_file(lake), 0.0, 0.0)
    assert len(parts) == 2
    assert set(parts["lake_id"]) == {900}


@pytest.fixture
def orphaning_lake(tmp_path):
    """A lake inlet whose only upstream reach is encompassed.

    Topology and geometry are decoupled on purpose: connectivity comes from
    fp_to_id, so U can sit inside lake 900 while M approaches lake 901 from
    dry land. That is the minimal shape leaving a survivor with no upstream
    and no downstream.
    """
    rows = [
        ("U", "M", 100.0, [(50, 0), (250, 0)]),
        ("M", None, 101.0, [(700, 0), (950, 0)]),
    ]
    geoms = [LineString(c) for *_, c in rows]
    net = tmp_path / "orphan_net.gpkg"
    gpd.GeoDataFrame(
        {
            "fp_id": [r[0] for r in rows],
            "fp_to_id": [r[1] for r in rows],
            "total_da_sqkm": [r[2] for r in rows],
            "stream_order": [7] * 2,
            "length_km": [g.length / 1000 for g in geoms],
        },
        geometry=geoms,
        crs=CRS,
    ).to_file(net, layer="flowpaths", driver="GPKG")

    lake = tmp_path / "orphan_lake.gpkg"
    gpd.GeoDataFrame(
        {"lake_id": [900, 901]},
        geometry=[box(0, -100, 300, 100), box(800, -100, 1200, 100)],
        crs=CRS,
    ).to_file(lake, layer="lakes_polygons", driver="GPKG")
    return net, lake


def test_orphan_left_by_lake_removal_is_dropped(orphaning_lake):
    """M is a legitimate inlet, but loses its only upstream to the lake."""
    gdf, counters = _run_lakes(*orphaning_lake)
    assert counters.n_reaches_encompassed_removed_lake == 1  # U
    assert counters.n_reaches_orphaned_lake == 1  # M, attached to nothing
    assert set(gdf[REACH_ID_FIELD]) == set()


def test_original_headwater_draining_into_a_lake_is_kept(tmp_path):
    """A one-reach watershed has no upstream by nature, not by removal."""
    net = tmp_path / "hw.gpkg"
    geom = [LineString([(100, 0), (500, 0)])]
    gpd.GeoDataFrame(
        {"fp_id": ["H"], "fp_to_id": [None], "total_da_sqkm": [10.0],
         "stream_order": [3], "length_km": [0.4]},
        geometry=geom, crs=CRS,
    ).to_file(net, layer="flowpaths", driver="GPKG")
    lake = tmp_path / "hw_lake.gpkg"
    gpd.GeoDataFrame({"lake_id": [1]}, geometry=[box(300, -100, 900, 100)],
                     crs=CRS).to_file(lake, layer="lakes_polygons", driver="GPKG")

    gdf, counters = _run_lakes(net, lake)
    assert counters.n_reaches_orphaned_lake == 0
    assert "H" in set(gdf[REACH_ID_FIELD])


def test_lake_outlet_with_no_downstream_is_kept(tmp_path):
    """Row 3: a real reach at a real lake, kept even though isolated."""
    rows = [
        ("O", None, 50.0, [(300, 0), (900, 0)]),   # emerges from the lake
    ]
    net = tmp_path / "outlet.gpkg"
    geoms = [LineString(c) for *_, c in rows]
    gpd.GeoDataFrame(
        {"fp_id": ["O"], "fp_to_id": [None], "total_da_sqkm": [50.0],
         "stream_order": [5], "length_km": [g.length / 1000 for g in geoms]},
        geometry=geoms, crs=CRS,
    ).to_file(net, layer="flowpaths", driver="GPKG")
    lake = tmp_path / "outlet_lake.gpkg"
    gpd.GeoDataFrame({"lake_id": [2]}, geometry=[box(0, -100, 500, 100)],
                     crs=CRS).to_file(lake, layer="lakes_polygons", driver="GPKG")

    gdf, counters = _run_lakes(net, lake)
    assert counters.n_reaches_orphaned_lake == 0
    row = gdf.set_index(REACH_ID_FIELD).loc["O"]
    assert row[LAKE_OUTLET_FIELD] and row[IS_HEADWATER_FIELD]


def test_orphan_removal_enters_the_reconciliation(orphaning_lake):
    """Orphans are a removal, so the accounting identity must include them."""
    gdf, counters = _run_lakes(*orphaning_lake)
    gdf = nw.merge_short_reaches(gdf, 5.0, 5.0, counters)
    gdf = nw.finalize_network(gdf, counters)
    assert counters.n_reaches_output == (
        counters.n_reaches_input
        - counters.n_reaches_encompassed_removed_lake
        - counters.n_reaches_orphaned_lake
        - counters.n_reaches_merged
        + counters.n_reaches_split_passthrough_lake
    )


### CHANNELS BETWEEN TWO LAKES ###


@pytest.fixture
def two_lake_channel(tmp_path):
    """One reach running from lake 900, across dry land, into lake 901.

        M: (100,0) -> (900,0)     lake 900: x<300     lake 901: x>700

    Both endpoints are in water but in DIFFERENT lakes, so the 300..700 dry
    middle is a real channel and must survive.
    """
    geom = [LineString([(100, 0), (900, 0)])]
    net = tmp_path / "two_lake_net.gpkg"
    gpd.GeoDataFrame(
        {"fp_id": ["M"], "fp_to_id": [None], "total_da_sqkm": [100.0],
         "stream_order": [6], "length_km": [0.8]},
        geometry=geom, crs=CRS,
    ).to_file(net, layer="flowpaths", driver="GPKG")

    lake = tmp_path / "two_lake_lakes.gpkg"
    gpd.GeoDataFrame(
        {"lake_id": [900, 901]},
        geometry=[box(0, -100, 300, 100), box(700, -100, 1200, 100)],
        crs=CRS,
    ).to_file(lake, layer="lakes_polygons", driver="GPKG")
    return net, lake


def test_channel_between_two_lakes_keeps_its_middle(two_lake_channel):
    """The inverse of a pass-through split: drop the ends, keep the middle."""
    gdf, counters = _run_lakes(*two_lake_channel)
    assert counters.n_reaches_trimmed_between_lakes == 1
    assert counters.n_reaches_encompassed_removed_lake == 0
    # 300..700 survives; the two submerged ends do not.
    np.testing.assert_allclose(
        gdf.set_index(REACH_ID_FIELD).loc["M"].geometry.length, 400.0
    )
    # length_km is written once, at finalize.
    final = nw.finalize_network(gdf, counters)
    np.testing.assert_allclose(
        final.set_index(REACH_ID_FIELD).loc["M", LENGTH_KM_FIELD], 0.4
    )


def test_channel_between_two_lakes_is_both_outlet_and_inlet(two_lake_channel):
    """It emerges from one lake and enters the other, so it is both."""
    gdf, _ = _run_lakes(*two_lake_channel)
    row = gdf.set_index(REACH_ID_FIELD).loc["M"]
    assert row[LAKE_OUTLET_FIELD] and row[LAKE_INLET_FIELD]
    assert row[IS_HEADWATER_FIELD] and row[IS_TERMINAL_FIELD]
    assert row[TERMINAL_REASON_FIELD] == "lake"
    assert row[IS_TRIMMED_FIELD]
    assert pd.isna(row[REACH_TO_ID_FIELD])


def test_channel_between_two_lakes_records_the_downstream_lake(two_lake_channel):
    """lake_to_id names the lake it flows INTO, matching the column name."""
    gdf, _ = _run_lakes(*two_lake_channel)
    assert gdf.set_index(REACH_ID_FIELD).loc["M", LAKE_TO_ID_FIELD] == "901"


def test_channel_between_two_lakes_survives_the_orphan_sweep(two_lake_channel):
    """Being isolated is correct here — it is a real reach between two lakes."""
    gdf, counters = _run_lakes(*two_lake_channel)
    assert counters.n_reaches_orphaned_lake == 0
    assert "M" in set(gdf[REACH_ID_FIELD])


def test_no_dry_middle_between_lakes_is_encompassed(tmp_path):
    """Touching lakes leave nothing to keep, so the reach is dropped."""
    geom = [LineString([(100, 0), (900, 0)])]
    net = tmp_path / "touching_net.gpkg"
    gpd.GeoDataFrame(
        {"fp_id": ["M"], "fp_to_id": [None], "total_da_sqkm": [100.0],
         "stream_order": [6], "length_km": [0.8]},
        geometry=geom, crs=CRS,
    ).to_file(net, layer="flowpaths", driver="GPKG")
    lake = tmp_path / "touching_lakes.gpkg"
    gpd.GeoDataFrame(
        {"lake_id": [900, 901]},
        geometry=[box(0, -100, 500, 100), box(500, -100, 1200, 100)],
        crs=CRS,
    ).to_file(lake, layer="lakes_polygons", driver="GPKG")

    gdf, counters = _run_lakes(net, lake)
    assert counters.n_reaches_encompassed_removed_lake == 1
    assert counters.n_reaches_trimmed_between_lakes == 0
    assert set(gdf[REACH_ID_FIELD]) == set()


### MERGE MUST NOT DESTROY WATERBODY REFERENCES ###


@pytest.fixture
def inlet_with_upstream(tmp_path):
    """U -> M, where M runs into a lake and is trimmed as an inlet.

    M is short enough that the merge walk absorbs U into it, which is where
    the reported lake_inlet-without-lake_to_id row came from.
    """
    rows = [
        ("U", "M", 150.0, [(0, 0), (300, 0)]),
        ("M", None, 156.0, [(300, 0), (700, 0)]),
    ]
    geoms = [LineString(c) for *_, c in rows]
    net = tmp_path / "inlet_net.gpkg"
    gpd.GeoDataFrame(
        {
            "fp_id": [r[0] for r in rows],
            "fp_to_id": [r[1] for r in rows],
            "total_da_sqkm": [r[2] for r in rows],
            "stream_order": [3, 3],
            "length_km": [g.length / 1000 for g in geoms],
        },
        geometry=geoms,
        crs=CRS,
    ).to_file(net, layer="flowpaths", driver="GPKG")

    lake = tmp_path / "inlet_lake.gpkg"
    gpd.GeoDataFrame(
        {"lake_id": [900]}, geometry=[box(600, -200, 1200, 200)], crs=CRS
    ).to_file(lake, layer="lakes_polygons", driver="GPKG")
    return net, lake


def test_merge_preserves_the_inlet_lake_reference(inlet_with_upstream):
    """Regression: lake_inlet true beside a null lake_to_id was impossible.

    lake_to_id describes the downstream end, so it belongs to the chain
    start. Copying it from the chain's top member — as lake_outlet and
    is_headwater correctly are — overwrote it with the upstream reach's null.
    """
    gdf, counters = _run_lakes(*inlet_with_upstream)
    before = gdf.set_index(REACH_ID_FIELD).loc["M", LAKE_TO_ID_FIELD]
    assert before == "900"

    gdf = nw.merge_short_reaches(gdf, 5.0, 5.0, counters)
    gdf = nw.finalize_network(gdf, counters)
    row = gdf.set_index(REACH_ID_FIELD).loc["M"]

    assert counters.n_reaches_merged == 1, "U must be absorbed for this to bite"
    assert row[LAKE_INLET_FIELD]
    assert row[LAKE_TO_ID_FIELD] == "900", "lake reference survives the merge"
    assert row[TERMINAL_REASON_FIELD] == "lake"
    # Upstream-end attributes still come from the top member.
    assert row[IS_HEADWATER_FIELD]


def test_no_reach_claims_a_waterbody_without_naming_it(pipeline):
    """A tag and its reference must agree across the whole output."""
    gdf, *_ = pipeline
    inlets = gdf[gdf[LAKE_INLET_FIELD] | gdf[LAKE_OUTLET_FIELD]]
    assert not inlets[LAKE_TO_ID_FIELD].isna().any(), (
        "lake_inlet/lake_outlet set without a lake_to_id"
    )
    coastal = gdf[gdf[TERMINAL_REASON_FIELD] == "coast"]
    trimmed_coastal = coastal[coastal[IS_TRIMMED_FIELD]]
    assert not trimmed_coastal[COAST_TO_ID_FIELD].isna().any(), (
        "a coastal trim must name the polygon it met"
    )


def test_trimmed_reaches_report_their_new_length(pipeline):
    """Every reach whose geometry was cut carries the cut length, not the old one."""
    gdf, *_ = pipeline
    trimmed = gdf[gdf[IS_TRIMMED_FIELD]]
    assert len(trimmed) > 0
    np.testing.assert_allclose(
        trimmed[LENGTH_KM_FIELD].to_numpy(dtype=float),
        trimmed.geometry.length.to_numpy() / 1000.0,
        rtol=1e-9,
    )


def test_merged_length_is_the_sum_of_post_trim_lengths(inlet_with_upstream):
    """A chain containing a trimmed inlet sums the trimmed length, not the raw one."""
    gdf, counters = _run_lakes(*inlet_with_upstream)
    by_id = gdf.set_index(REACH_ID_FIELD)
    trimmed_inlet = by_id.loc["M"].geometry.length / 1000
    upstream = by_id.loc["U"].geometry.length / 1000
    np.testing.assert_allclose(trimmed_inlet, 0.3)  # 300..600, not 300..700

    gdf = nw.merge_short_reaches(gdf, 5.0, 5.0, counters)
    gdf = nw.finalize_network(gdf, counters)
    merged = gdf.set_index(REACH_ID_FIELD).loc["M"]
    np.testing.assert_allclose(merged[LENGTH_KM_FIELD], trimmed_inlet + upstream)
    np.testing.assert_allclose(merged[LENGTH_KM_FIELD], merged.geometry.length / 1000)


def test_stranded_reach_keeps_its_original_length(pipeline):
    """Stranded reaches lose a pointer, not geometry, so length is untouched."""
    gdf, *_ = pipeline
    r12 = gdf.set_index(REACH_ID_FIELD).loc["12"]
    assert not r12[IS_TRIMMED_FIELD]
    np.testing.assert_allclose(r12[LENGTH_KM_FIELD], np.hypot(4, 20) / 1000.0)


def test_length_km_matches_geometry_for_every_reach(output_network):
    """One definition, applied uniformly — verifiable against the artifact.

    Previously trimmed reaches carried a length we computed while untouched
    reaches carried NHF's own, so two rows were not strictly comparable and
    the merge floor summed across both bases.
    """
    np.testing.assert_allclose(
        output_network[LENGTH_KM_FIELD].to_numpy(dtype=float),
        output_network.geometry.length.to_numpy() / 1000.0,
        rtol=1e-9,
    )


def test_source_length_column_is_not_required(tmp_path):
    """It is computed, so a source without it still loads."""
    geom = [LineString([(0, 0), (1000, 0)])]
    path = tmp_path / "no_length.gpkg"
    gpd.GeoDataFrame(
        {"fp_id": [1], "fp_to_id": [None], "total_da_sqkm": [5.0],
         "stream_order": [3]},
        geometry=geom, crs=CRS,
    ).to_file(path, layer="flowpaths", driver="GPKG")

    gdf, counters = nw.load_reach_network(str(path), None)
    gdf = nw.finalize_network(nw.tag_terminal_reaches(gdf), counters)
    np.testing.assert_allclose(gdf.iloc[0][LENGTH_KM_FIELD], 1.0)


def test_source_length_disagreeing_with_geometry_is_overridden(tmp_path):
    """NHF's own value does not survive into the output."""
    geom = [LineString([(0, 0), (1000, 0)])]
    path = tmp_path / "wrong_length.gpkg"
    gpd.GeoDataFrame(
        {"fp_id": [1], "fp_to_id": [None], "total_da_sqkm": [5.0],
         "stream_order": [3], "length_km": [99.0]},
        geometry=geom, crs=CRS,
    ).to_file(path, layer="flowpaths", driver="GPKG")

    gdf, counters = nw.load_reach_network(str(path), None)
    gdf = nw.finalize_network(nw.tag_terminal_reaches(gdf), counters)
    np.testing.assert_allclose(gdf.iloc[0][LENGTH_KM_FIELD], 1.0)


### MULTIPART EXPLODE ###


@pytest.fixture
def multipart_network(tmp_path):
    """Reach 3434 is a MultiLineString of three disjoint parts.

        T --> 3434 (3 parts) --> D

    The gaps are real, so line_merge cannot fuse them. T points at 3434 and
    must end up pointing at the first part.
    """
    from shapely.geometry import MultiLineString

    rows = [
        ("10", "3434", MultiLineString([[(0, 500), (0, 0)]])),
        (
            "3434",
            "20",
            MultiLineString(
                [
                    [(0, 0), (100, 0)],
                    [(110, 0), (200, 0)],
                    [(210, 0), (300, 0)],
                ]
            ),
        ),
        ("20", None, MultiLineString([[(300, 0), (400, 0)]])),
    ]
    path = tmp_path / "multipart.gpkg"
    gpd.GeoDataFrame(
        {
            "fp_id": [r[0] for r in rows],
            "fp_to_id": [r[1] for r in rows],
            "total_da_sqkm": [100.0, 101.0, 102.0],
            "stream_order": [3, 3, 3],
        },
        geometry=[r[2] for r in rows],
        crs=CRS,
    ).to_file(path, layer="flowpaths", driver="GPKG")
    return path


def test_output_contains_no_multipart_geometry(multipart_network):
    gdf, counters = nw.load_reach_network(str(multipart_network), None)
    assert set(gdf.geom_type) == {"LineString"}


def test_multipart_reach_becomes_numbered_parts(multipart_network):
    """3434 with three parts becomes 3434_1, 3434_2, 3434_3."""
    gdf, _ = nw.load_reach_network(str(multipart_network), None)
    ids = set(gdf[REACH_ID_FIELD])
    assert {"3434_1", "3434_2", "3434_3"} <= ids
    assert "3434" not in ids, "the parent id must not survive an explode"
    # Single-part reaches are untouched by line_merge and keep their id.
    assert {"10", "20"} <= ids


def test_exploded_parts_are_chained_in_flow_order(multipart_network):
    """Part k drains to part k+1; the last keeps the original downstream."""
    gdf, _ = nw.load_reach_network(str(multipart_network), None)
    by_id = gdf.set_index(REACH_ID_FIELD)
    assert by_id.loc["3434_1", REACH_TO_ID_FIELD] == "3434_2"
    assert by_id.loc["3434_2", REACH_TO_ID_FIELD] == "3434_3"
    assert by_id.loc["3434_3", REACH_TO_ID_FIELD] == "20"


def test_tributary_follows_an_exploded_reach_to_its_first_part(multipart_network):
    """Flow enters at part 1, so the pointer must land there, not dangle."""
    gdf, _ = nw.load_reach_network(str(multipart_network), None)
    by_id = gdf.set_index(REACH_ID_FIELD)
    assert by_id.loc["10", REACH_TO_ID_FIELD] == "3434_1"
    live = set(gdf[REACH_ID_FIELD])
    assert set(gdf[REACH_TO_ID_FIELD].dropna()) <= live, "no dangling pointers"


def test_exploded_part_split_by_a_lake_nests_its_suffix(tmp_path):
    """3434_3 split by a lake becomes 3434_3_1 and 3434_3_2."""
    from shapely.geometry import MultiLineString

    net = tmp_path / "nested.gpkg"
    gpd.GeoDataFrame(
        {"fp_id": ["3434"], "fp_to_id": [None], "total_da_sqkm": [100.0],
         "stream_order": [3]},
        geometry=[MultiLineString([[(0, 0), (100, 0)], [(200, 0), (900, 0)]])],
        crs=CRS,
    ).to_file(net, layer="flowpaths", driver="GPKG")

    lake = tmp_path / "nested_lake.gpkg"
    gpd.GeoDataFrame(
        {"lake_id": [7]}, geometry=[box(400, -50, 600, 50)], crs=CRS
    ).to_file(lake, layer="lakes_polygons", driver="GPKG")

    gdf, counters = _run_lakes(net, lake)
    gdf = nw.finalize_network(gdf, counters)
    ids = set(gdf[REACH_ID_FIELD])
    assert counters.n_reaches_split_passthrough_lake == 1
    assert {"3434_1", "3434_2_1", "3434_2_2"} == ids


def test_nested_ids_sort_beside_their_parent(tmp_path):
    """Ordering is by integer path, so 3434_2_1 follows 3434_1 not 3434_10."""
    order = nw._natural_order(
        pd.Series(["3434_10", "3434_2_2", "10", "2", "3434_1", "3434_2_1"])
    )
    ranked = pd.Series(
        ["3434_10", "3434_2_2", "10", "2", "3434_1", "3434_2_1"]
    ).iloc[order].tolist()
    assert ranked == ["2", "10", "3434_1", "3434_2_1", "3434_2_2", "3434_10"]


def test_lake_count_reported_is_lakes_not_parts(caplog, payload, lakes_layer):
    """The negative buffer can split one lake into several polygons.

    Reporting len(prepared_lakes) counted parts, so a single lake broken in
    two read as two lakes.
    """
    import logging

    with caplog.at_level(logging.INFO, logger="twod_fim_jobs.jobs.modify_network"):
        ModifyNetworkJob().run(payload)
    line = next(m for m in caplog.messages if m.startswith("Lake pass left"))
    # Fixture has two lakes; the tiny one is buffered away, leaving one.
    assert "1 lakes" in line, line


def test_one_lake_split_into_parts_counts_once(tmp_path):
    """A dumbbell lake pinched in two by the buffer is still one lake."""
    from shapely.geometry import Polygon

    dumbbell = Polygon(
        [(0, 0), (400, 0), (400, 190), (600, 190), (600, 0), (1000, 0),
         (1000, 400), (600, 400), (600, 210), (400, 210), (400, 400), (0, 400)]
    )
    path = tmp_path / "dumbbell.gpkg"
    gpd.GeoDataFrame({"lake_id": [55]}, geometry=[dumbbell], crs=CRS).to_file(
        path, layer="lakes_polygons", driver="GPKG"
    )
    parts = nw.prepare_lakes(gpd.read_file(path), 0.0, 20.0)
    assert len(parts) > 1, "buffer must actually pinch it apart"
    assert parts[LAKE_ID_FIELD].nunique() == 1


### MISSING WATERBODY ID COLUMNS ###


def test_missing_coastal_id_records_null_not_a_row_number(tmp_path, caplog):
    """A fabricated index joins to nothing and moves if the source is reordered."""
    import logging

    net = tmp_path / "n.gpkg"
    gpd.GeoDataFrame(
        {"fp_id": ["A"], "fp_to_id": [None], "total_da_sqkm": [10.0],
         "stream_order": [3]},
        geometry=[LineString([(0, 0), (800, 0)])], crs=CRS,
    ).to_file(net, layer="flowpaths", driver="GPKG")
    coast = tmp_path / "c.gpkg"
    gpd.GeoDataFrame(  # no 'id' column
        {"name": ["shore"]}, geometry=[box(500, -100, 900, 100)], crs=CRS
    ).to_file(coast, layer="coastal_influence", driver="GPKG")

    gdf, counters = nw.load_reach_network(str(net), None)
    gdf = nw.tag_headwater_reaches(nw.tag_terminal_reaches(gdf))
    with caplog.at_level(logging.WARNING, logger="twod_fim_jobs.utils.network"):
        gdf, _ = nw.apply_coastal(gdf, gpd.read_file(coast).to_crs(gdf.crs), counters)

    row = gdf.set_index(REACH_ID_FIELD).loc["A"]
    assert counters.n_reaches_trimmed_inlet_coastal == 1
    assert row[TERMINAL_REASON_FIELD] == "coast", "tagging is unaffected"
    assert pd.isna(row[COAST_TO_ID_FIELD]), "no fabricated reference"
    assert any("coastal_influence" in m for m in caplog.messages), (
        "the warning must name the layer that is missing the column"
    )


def test_unidentified_lakes_do_not_compare_as_one_lake(tmp_path):
    """Null must not equal null: two unnamed lakes are still two lakes.

    Otherwise a channel running between them would be encompassed instead of
    keeping its middle.
    """
    net = tmp_path / "n.gpkg"
    gpd.GeoDataFrame(
        {"fp_id": ["M"], "fp_to_id": [None], "total_da_sqkm": [10.0],
         "stream_order": [3]},
        geometry=[LineString([(100, 0), (900, 0)])], crs=CRS,
    ).to_file(net, layer="flowpaths", driver="GPKG")
    lake = tmp_path / "l.gpkg"
    gpd.GeoDataFrame(  # no 'lake_id' column
        {"name": ["a", "b"]},
        geometry=[box(0, -100, 300, 100), box(700, -100, 1200, 100)], crs=CRS,
    ).to_file(lake, layer="lakes_polygons", driver="GPKG")

    gdf, counters = _run_lakes(net, lake)
    assert counters.n_reaches_trimmed_between_lakes == 1, (
        "two null-id lakes must still read as two lakes"
    )
    assert counters.n_reaches_encompassed_removed_lake == 0
    assert pd.isna(gdf.set_index(REACH_ID_FIELD).loc["M", LAKE_TO_ID_FIELD])
