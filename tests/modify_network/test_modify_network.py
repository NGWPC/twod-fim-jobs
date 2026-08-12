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
    IS_HEADWATER_FIELD,
    IS_TERMINAL_FIELD,
    IS_TRIMMED_FIELD,
    LAKE_INLET_FIELD,
    LAKE_OUTLET_FIELD,
    REACH_ID_FIELD,
    REACH_TO_ID_FIELD,
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
        # fp_id, fp_to_id, area_sqkm, stream_order, coords
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
            "area_sqkm": [r[2] for r in rows],
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
        {"name": ["big", "tiny"]},
        geometry=[box(140, -10, 160, 10), box(400, 400, 401, 401)],
        crs=CRS,
    ).to_file(path, layer="lakes_polygons", driver="GPKG")
    return path


@pytest.fixture
def coastal_layer(tmp_path: Path) -> Path:
    path = tmp_path / "coastal.gpkg"
    gpd.GeoDataFrame(
        {"name": ["coast"]}, geometry=[box(300, -50, 315, 50)], crs=CRS
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
    gdf, counters, max_src = nw.load_reach_network(str(synthetic_network), 3)
    assert counters.n_reaches_input == 14
    assert counters.n_reaches_below_stream_order_removed == 2
    assert len(gdf) == 12
    assert set(gdf["stream_order"]) == {3, 4}


def test_no_threshold_means_no_filtering(synthetic_network):
    gdf, counters, max_src = nw.load_reach_network(str(synthetic_network), None)
    assert len(gdf) == 14
    # Null, not zero: the branch did not run.
    assert counters.n_reaches_below_stream_order_removed is None


def test_unprojected_crs_rejected(tmp_path):
    geom = [LineString([(0, 0), (1, 1)])]
    gpd.GeoDataFrame(
        {
            "fp_id": [1],
            "fp_to_id": [None],
            "area_sqkm": [1.0],
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
    gdf, _, max_src = nw.load_reach_network(str(synthetic_network), None)
    gdf = nw.tag_headwater_reaches(nw.tag_terminal_reaches(gdf))
    by_id = gdf.set_index(REACH_ID_FIELD)
    # 13 and 14 have nothing flowing into them.
    assert by_id.loc[13, IS_HEADWATER_FIELD]
    assert by_id.loc[14, IS_HEADWATER_FIELD]
    # 3 is a terminal outlet with two parents - never a headwater.
    assert by_id.loc[3, IS_TERMINAL_FIELD]
    assert not by_id.loc[3, IS_HEADWATER_FIELD]
    assert by_id.loc[3, TERMINAL_REASON_FIELD] == "outlet"


def test_filter_strands_reaches_into_apparent_headwaters(synthetic_network):
    """Reach 1 gains headwater status only because 13 and 14 were filtered."""
    unfiltered, _, _ = nw.load_reach_network(str(synthetic_network), None)
    assert (
        not nw.tag_headwater_reaches(unfiltered)
        .set_index(REACH_ID_FIELD)
        .loc[1, IS_HEADWATER_FIELD]
    )
    filtered, _, _ = nw.load_reach_network(str(synthetic_network), 3)
    assert (
        nw.tag_headwater_reaches(filtered)
        .set_index(REACH_ID_FIELD)
        .loc[1, IS_HEADWATER_FIELD]
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
    gdf, counters, max_src = nw.load_reach_network(str(synthetic_network), 3)
    gdf = nw.tag_headwater_reaches(nw.tag_terminal_reaches(gdf))
    coastal = gpd.read_file(coastal_layer).to_crs(gdf.crs)
    gdf, coastal_touched = nw.apply_coastal(gdf, coastal, counters)
    lakes = nw.prepare_lakes(gpd.read_file(lakes_layer).to_crs(gdf.crs), 0.0, 2.0)
    gdf, lake_touched = nw.apply_lakes(gdf, lakes, counters, max_src)
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
    assert sorted(gdf[REACH_ID_FIELD]) == [2, 3, 4, 5, 7, 8, 9, 12, 15]


def test_junction_blocks_merge(pipeline):
    """Reach 3 has two parents, so it is never a merge candidate."""
    gdf, *_ = pipeline
    by_id = gdf.set_index(REACH_ID_FIELD)
    assert 3 in by_id.index and 4 in by_id.index
    np.testing.assert_allclose(by_id.loc[3].geometry.length, 10.0)


def test_merge_sums_length_and_rewires_tributaries(pipeline):
    """Reach 1 is absorbed into 2: summed length, no dangling pointer."""
    gdf, *_ = pipeline
    by_id = gdf.set_index(REACH_ID_FIELD)
    assert 1 not in by_id.index
    np.testing.assert_allclose(by_id.loc[2, "length_km"], 0.02)
    np.testing.assert_allclose(by_id.loc[2].geometry.length, 20.0)
    assert 1 not in set(gdf[REACH_TO_ID_FIELD].dropna())
    # Headwater status of the chain's upstream end carries to the survivor.
    assert by_id.loc[2, IS_HEADWATER_FIELD]


def test_merged_chain_respects_length_cap(synthetic_network):
    """A cap below the combined length blocks an otherwise-eligible merge."""
    gdf, counters, max_src = nw.load_reach_network(str(synthetic_network), 3)
    gdf = nw.tag_headwater_reaches(nw.tag_terminal_reaches(gdf))
    gdf = nw.merge_short_reaches(gdf, 5.0, 0.015, counters)
    assert counters.n_reaches_merged == 0


def test_merge_respects_drainage_area_threshold(synthetic_network):
    gdf, counters, max_src = nw.load_reach_network(str(synthetic_network), 3)
    gdf = nw.tag_headwater_reaches(nw.tag_terminal_reaches(gdf))
    # 100 vs 101 is a ~1% difference; a 0.5% threshold blocks it.
    gdf = nw.merge_short_reaches(gdf, 0.5, 3.0, counters)
    assert counters.n_reaches_merged == 0


def test_lake_inlet_trimmed_and_terminated(pipeline):
    gdf, *_ = pipeline
    r5 = gdf.set_index(REACH_ID_FIELD).loc[5]
    assert r5[LAKE_INLET_FIELD] and r5[IS_TERMINAL_FIELD] and r5[IS_TRIMMED_FIELD]
    assert r5[TERMINAL_REASON_FIELD] == "lake"
    assert pd.isna(r5[REACH_TO_ID_FIELD])
    np.testing.assert_allclose(r5.geometry.length, 42.0)  # 100->142


def test_lake_outlet_trimmed_and_headwatered(pipeline):
    gdf, *_ = pipeline
    r7 = gdf.set_index(REACH_ID_FIELD).loc[7]
    assert r7[LAKE_OUTLET_FIELD] and r7[IS_HEADWATER_FIELD]
    np.testing.assert_allclose(r7.geometry.length, 32.0)  # 158->190


def test_lake_passthrough_split_keeps_original_id_upstream(pipeline):
    """Spec step 5: original reach_id is the inlet piece, new id the outlet."""
    gdf, *_ = pipeline
    by_id = gdf.set_index(REACH_ID_FIELD)
    inlet, outlet = by_id.loc[8], by_id.loc[15]
    assert inlet[LAKE_INLET_FIELD] and inlet[TERMINAL_REASON_FIELD] == "lake"
    assert outlet[LAKE_OUTLET_FIELD] and outlet[IS_HEADWATER_FIELD]
    np.testing.assert_allclose(inlet.geometry.length, 12.0)
    np.testing.assert_allclose(outlet.geometry.length, 12.0)


def test_coastal_trim_and_cascade(pipeline):
    gdf, *_ = pipeline
    by_id = gdf.set_index(REACH_ID_FIELD)
    assert 10 not in by_id.index and 11 not in by_id.index
    r9 = by_id.loc[9]
    assert r9[IS_TERMINAL_FIELD] and r9[TERMINAL_REASON_FIELD] == "coast"
    np.testing.assert_allclose(r9.geometry.length, 50.0)  # 250->300


def test_stranded_tributary_terminated_without_trimming(pipeline):
    """Reach 12 never touched the coast layer but lost its downstream."""
    gdf, *_ = pipeline
    r12 = gdf.set_index(REACH_ID_FIELD).loc[12]
    assert r12[IS_TERMINAL_FIELD] and r12[TERMINAL_REASON_FIELD] == "coast"
    assert not r12[IS_TRIMMED_FIELD], "stranded reaches keep their geometry"
    np.testing.assert_allclose(r12.geometry.length, np.hypot(4, 20))


def test_no_dangling_downstream_pointers(pipeline):
    gdf, *_ = pipeline
    live = set(gdf[REACH_ID_FIELD])
    assert set(gdf[REACH_TO_ID_FIELD].dropna().astype(int)) <= live


def test_pipeline_is_deterministic(synthetic_network, lakes_layer, coastal_layer):
    def once():
        gdf, counters, max_src = nw.load_reach_network(str(synthetic_network), 3)
        gdf = nw.tag_headwater_reaches(nw.tag_terminal_reaches(gdf))
        coastal = gpd.read_file(coastal_layer).to_crs(gdf.crs)
        gdf, _ = nw.apply_coastal(gdf, coastal, counters)
        lakes = nw.prepare_lakes(gpd.read_file(lakes_layer).to_crs(gdf.crs), 0.0, 2.0)
        gdf, _ = nw.apply_lakes(gdf, lakes, counters, max_src)
        gdf = nw.merge_short_reaches(gdf, 5.0, 3.0, counters)
        return nw.finalize_network(gdf, counters)

    a, b = once(), once()
    assert a.drop(columns=a.geometry.name).equals(b.drop(columns=b.geometry.name))
    assert (a.geometry.to_wkb() == b.geometry.to_wkb()).all()


def test_skipping_waterbodies_is_a_noop_on_the_network(synthetic_network):
    """No lakes and no coastal means no reach is dropped, trimmed, or split."""
    gdf, counters, max_src = nw.load_reach_network(str(synthetic_network), 3)
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
    assert manifest["identity"]["max_length_threshold_km"] == 3.0
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
            "max_length_threshold_km": 99.0,
            "base_output_path": payload["base_output_path"] + "-b",
        }
    )
    assert first.identity_hash != second.identity_hash
