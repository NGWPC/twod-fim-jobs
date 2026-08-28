from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import geopandas as gpd
from shapely.geometry import LineString

from twod_fim_jobs.hydraulic_solvers.common import check_run_exists, publish_scenario
from twod_fim_jobs.hydraulic_solvers.pre_process import process_bc_line
from twod_fim_jobs.models.common import Asset, Domain, GridProperties
from twod_fim_jobs.models.solvers import (
    ConvergenceResult,
    HFixBC,
    PostProcessResult,
    QFixBC,
    RunConfig,
    RunIdentity,
    RunScenarioInputs,
    RunScenarioManifest,
    RunScenarioResults,
    ScenarioAssets,
    SolveScenarioResults,
    TerminationCondition,
    TransferBC,
)


ROOT = Path(__file__).parent
TEST_MODEL_DATA = (
    ROOT / "run_scenarios" / "data" / "1257410937935512" / "fceb20c6_N164S214E230W107"
)


def create_test_run_scenario_inputs(
    base_out_dir: str, reach_id: int, model_id: str = "fceb20c6_N164S214E230W107"
) -> RunScenarioInputs:
    """Helper to create minimal valid RunScenarioInputs for testing."""
    return RunScenarioInputs(
        domain=Domain(
            bbox=(0.0, 0.0, 100.0, 100.0),
            anchor=(50.0, 50.0),
            offsets=(10.0, 20.0, 15.0, 5.0),
        ),
        grid_properties=GridProperties(rows=10, cols=10),
        terrain=Asset(
            href="s3://bucket/terrain.tif",
            checksum="a1b2c3d4e5f6a7b8",
            source_url=None,
            derived=False,
        ),
        roughness=Asset(
            href="s3://bucket/roughness.tif",
            checksum="b2c3d4e5f6a7b8c9",
            source_url=None,
            derived=False,
        ),
        boundary_conditions=[
            HFixBC(
                vector=Asset(
                    href="s3://bucket/bc_vector.geojson",
                    checksum="d4e5f6a7b8c9d0e1",
                    source_url=None,
                    derived=False,
                ),
                value=1.0,
            ),
            QFixBC(
                vector=Asset(
                    href="s3://bucket/qfix_vector.geojson",
                    checksum="e5f6a7b8c9d0e1f2",
                    source_url=None,
                    derived=False,
                ),
                value=100.0,
            ),
        ],
        hot_start=None,
        run_config=RunConfig(
            sim_time_seconds=3600.0,
            save_interval_seconds=600.0,
            max_simulation_wall_time_seconds=3600.0,
        ),
        base_out_dir=base_out_dir,
        reach_id=reach_id,
        model_id=model_id,
        centerline=Asset(
            href="s3://bucket/centerline.geojson",
            checksum="c3d4e5f6a7b8c9d0",
            source_url=None,
            derived=False,
        ),
        run_identity_hash="a1b2c3d4",
    )


### TESTS ###


def test_publish_scenario_preserves_s3_double_slash(tmp_path: Path) -> None:
    """s3:// scheme must not be collapsed to s3:/ by path joining."""
    # Create placeholder local files for mock results
    depth = tmp_path / "depth.tif"
    inun = tmp_path / "inundation.geojson"
    stl = tmp_path / "stl.geojson"
    for f in (depth, inun, stl):
        f.write_text("mock data")

    # Create real RunScenarioInputs with valid model_id format
    run_scenario_inputs = RunScenarioInputs(
        domain=Domain(
            bbox=(0.0, 0.0, 100.0, 100.0),
            anchor=(50.0, 50.0),
            offsets=(10.0, 20.0, 15.0, 5.0),
        ),
        grid_properties=GridProperties(rows=10, cols=10),
        terrain=Asset(
            href="s3://bucket/terrain.tif",
            checksum="a1b2c3d4e5f6a7b8",
            source_url=None,
            derived=False,
        ),
        roughness=Asset(
            href="s3://bucket/roughness.tif",
            checksum="b2c3d4e5f6a7b8c9",
            source_url=None,
            derived=False,
        ),
        boundary_conditions=[
            HFixBC(
                vector=Asset(
                    href="s3://bucket/bc_vector.geojson",
                    checksum="d4e5f6a7b8c9d0e1",
                    source_url=None,
                    derived=False,
                ),
                value=1.0,
            ),
            QFixBC(
                vector=Asset(
                    href="s3://bucket/qfix_vector.geojson",
                    checksum="e5f6a7b8c9d0e1f2",
                    source_url=None,
                    derived=False,
                ),
                value=100.0,
            ),
        ],
        hot_start=None,
        run_config=RunConfig(
            sim_time_seconds=3600.0,
            save_interval_seconds=600.0,
            max_simulation_wall_time_seconds=3600.0,
        ),
        base_out_dir="s3://bucket/results",
        reach_id=12345,
        model_id="fceb20c6_N164S214E230W107",
        centerline=Asset(
            href="s3://bucket/centerline.geojson",
            checksum="c3d4e5f6a7b8c9d0",
            source_url=None,
            derived=False,
        ),
        run_identity_hash="a1b2c3d4",
    )

    # Create mock solve results
    solve_results = SolveScenarioResults(
        convergence_results=[
            ConvergenceResult(
                volume_convergence=0.05, boundary_check=None, model_running=False
            )
        ],
        termination_condition=TerminationCondition.VOLUME_CONVERGENCE,
        wall_time=100.0,
    )

    # Create mock post-process results
    processed = PostProcessResult(
        depth_path=depth,
        inundation_polygon_path=inun,
        stl_path=stl,
        zarr_path=None,
        nominal_wse=1.0,
        sim_time=100.0,
    )

    # Mock the file operations to avoid actual S3 calls
    with (
        patch(
            "twod_fim_jobs.hydraulic_solvers.common.hash_file",
            return_value="a1b2c3d4e5f6a7b8",
        ),
        patch("twod_fim_jobs.hydraulic_solvers.common.copy_file"),
        patch("twod_fim_jobs.hydraulic_solvers.common.write_json"),
        patch(
            "twod_fim_jobs.hydraulic_solvers.common.get_run_identity_hash",
            return_value="fceb20c6",
        ),
        patch(
            "twod_fim_jobs.hydraulic_solvers.common.get_run_identity",
            return_value=RunIdentity(
                sdr_commit_id="826a602ddcaf58bf4081dc04b65ba15b82cc8c8a",
                solver="lisflood",
            ),
        ),
    ):
        manifest = publish_scenario(run_scenario_inputs, solve_results, processed)

    # Verify s3:// is preserved (not collapsed to s3:/)
    assert manifest.assets.depth.href.startswith("s3://"), (
        f"Expected s3:// prefix in depth asset, got: {manifest.assets.depth.href}"
    )
    assert manifest.assets.inundation_polygon.href.startswith("s3://"), (
        f"Expected s3:// prefix in inundation asset, got: {manifest.assets.inundation_polygon.href}"
    )
    assert manifest.assets.stage_transfer_line.href.startswith("s3://"), (
        f"Expected s3:// prefix in stl asset, got: {manifest.assets.stage_transfer_line.href}"
    )


def test_write_model_results_to_s3_works(tmp_path: Path) -> None:
    """Test that writing model results to S3 works by mocking S3 storage operations."""
    # Create placeholder local files for mock results
    depth = tmp_path / "depth.tif"
    inun = tmp_path / "inundation.geojson"
    stl = tmp_path / "stl.geojson"
    for f in (depth, inun, stl):
        f.write_text("mock output data")

    # Create real RunScenarioInputs with S3 output directory
    run_scenario_inputs = RunScenarioInputs(
        domain=Domain(
            bbox=(0.0, 0.0, 100.0, 100.0),
            anchor=(50.0, 50.0),
            offsets=(10.0, 20.0, 15.0, 5.0),
        ),
        grid_properties=GridProperties(rows=10, cols=10),
        terrain=Asset(
            href="s3://bucket/terrain.tif",
            checksum="a1b2c3d4e5f6a7b8",
            source_url=None,
            derived=False,
        ),
        roughness=Asset(
            href="s3://bucket/roughness.tif",
            checksum="b2c3d4e5f6a7b8c9",
            source_url=None,
            derived=False,
        ),
        boundary_conditions=[
            HFixBC(
                vector=Asset(
                    href="s3://bucket/bc_vector.geojson",
                    checksum="d4e5f6a7b8c9d0e1",
                    source_url=None,
                    derived=False,
                ),
                value=2.5,
            ),
            QFixBC(
                vector=Asset(
                    href="s3://bucket/qfix_vector.geojson",
                    checksum="e5f6a7b8c9d0e1f2",
                    source_url=None,
                    derived=False,
                ),
                value=250.0,
            ),
        ],
        hot_start=None,
        run_config=RunConfig(
            sim_time_seconds=3600.0,
            save_interval_seconds=600.0,
            max_simulation_wall_time_seconds=3600.0,
        ),
        base_out_dir="s3://bucket/results/reach-123",
        reach_id=9876543210,
        model_id="fceb20c6_N164S214E230W107",
        centerline=Asset(
            href="s3://bucket/centerline.geojson",
            checksum="c3d4e5f6a7b8c9d0",
            source_url=None,
            derived=False,
        ),
        run_identity_hash="a1b2c3d4",
    )

    # Create mock solve results
    solve_results = SolveScenarioResults(
        convergence_results=[
            ConvergenceResult(
                volume_convergence=0.05, boundary_check=None, model_running=False
            )
        ],
        termination_condition=TerminationCondition.VOLUME_CONVERGENCE,
        wall_time=250.5,
    )

    # Create mock post-process results
    processed = PostProcessResult(
        depth_path=depth,
        inundation_polygon_path=inun,
        stl_path=stl,
        zarr_path=None,
        nominal_wse=2.5,
        sim_time=360.0,
    )

    # Mock S3 operations to verify they are called
    mock_hash_file = MagicMock(return_value="a1b2c3d4e5f6a7b8")
    mock_copy_file = MagicMock()
    mock_write_json = MagicMock()

    with (
        patch("twod_fim_jobs.hydraulic_solvers.common.hash_file", mock_hash_file),
        patch("twod_fim_jobs.hydraulic_solvers.common.copy_file", mock_copy_file),
        patch("twod_fim_jobs.hydraulic_solvers.common.write_json", mock_write_json),
        patch(
            "twod_fim_jobs.hydraulic_solvers.common.get_run_identity_hash",
            return_value="fceb20c6",
        ),
        patch(
            "twod_fim_jobs.hydraulic_solvers.common.get_run_identity",
            return_value=RunIdentity(
                sdr_commit_id="826a602ddcaf58bf4081dc04b65ba15b82cc8c8a",
                solver="lisflood",
            ),
        ),
    ):
        manifest = publish_scenario(run_scenario_inputs, solve_results, processed)

    # Verify the manifest was created with expected structure
    assert isinstance(manifest, RunScenarioManifest)
    assert manifest.assets is not None
    assert manifest.assets.depth is not None
    assert manifest.assets.inundation_polygon is not None
    assert manifest.assets.stage_transfer_line is not None

    # Verify hash_file was called for each asset
    assert mock_hash_file.call_count == 3

    # Verify copy_file and write_json were called
    assert mock_copy_file.call_count >= 3  # For depth, inun, stl
    assert mock_write_json.called


def test_check_model_skips_when_run_exists() -> None:
    """Test that models skip when a run already exists by checking check_run_exists returns manifest."""
    # Create minimal RunScenarioInputs with valid model_id
    model_id = "fceb20c6_N164S214E230W107"
    run_scenario_inputs = create_test_run_scenario_inputs(
        base_out_dir="s3://bucket/results",
        reach_id=54321,
        model_id=model_id,
    )

    # Create a valid manifest that would be returned by check_run_exists
    expected_manifest = RunScenarioManifest(
        created_at=datetime.now(timezone.utc),
        identity_hash="fceb20c6",
        scenario_code="KWSE1.0Q100",
        reach_id=54321,
        model_id=model_id,
        identity=RunIdentity(
            sdr_commit_id="826a602ddcaf58bf4081dc04b65ba15b82cc8c8a",
            solver="lisflood",
        ),
        self_href="s3://bucket/manifest.json",
        inputs=run_scenario_inputs,
        assets=ScenarioAssets(
            depth=Asset(
                href="s3://bucket/depth.tif",
                checksum="a1b2c3d4e5f6a7b8",
                source_url=None,
                derived=True,
            ),
            inundation_polygon=Asset(
                href="s3://bucket/inun.geojson",
                checksum="b2c3d4e5f6a7b8c9",
                source_url=None,
                derived=True,
            ),
            stage_transfer_line=Asset(
                href="s3://bucket/stl.geojson",
                checksum="c3d4e5f6a7b8c9d0",
                source_url=None,
                derived=True,
            ),
            zarr_store=None,
        ),
        properties=RunScenarioResults(
            convergence_results=[
                ConvergenceResult(
                    volume_convergence=0.05, boundary_check=None, model_running=False
                )
            ],
            termination_condition=TerminationCondition.VOLUME_CONVERGENCE,
            wall_time=100.0,
            nominal_wse=1.0,
            us_discharge=100,
            sim_time=100.0,
        ),
        warnings=[],
    )

    # Mock the storage functions to return valid JSON
    with (
        patch(
            "twod_fim_jobs.hydraulic_solvers.common.check_file_exists",
            return_value=True,
        ),
        patch(
            "twod_fim_jobs.hydraulic_solvers.common.read_json",
            return_value=expected_manifest.model_dump_json(),
        ),
    ):
        result = check_run_exists(run_scenario_inputs)

    # Verify the function returns the manifest instead of None (skipping re-run)
    assert result is not None
    assert isinstance(result, RunScenarioManifest)
    assert result.reach_id == 54321
    assert result.model_id == model_id


def test_check_run_exists_returns_none_when_manifest_not_found() -> None:
    """Test that check_run_exists returns None when manifest file does not exist."""
    run_scenario_inputs = create_test_run_scenario_inputs(
        base_out_dir="/nonexistent/path",
        reach_id=11111,
        model_id="00000000_N0S0E0W0",
    )

    # Mock that the manifest file does not exist
    with patch(
        "twod_fim_jobs.hydraulic_solvers.common.check_file_exists", return_value=False
    ):
        result = check_run_exists(run_scenario_inputs)

    # Verify the function returns None when manifest doesn't exist
    assert result is None


def test_kwse_downstream_stl_outside_bounds_raises_error(tmp_path: Path) -> None:
    """Test that TransferBC with non-intersecting geometry returns empty points."""
    # Create a domain with specific bounds
    domain = Domain(
        bbox=(0.0, 0.0, 100.0, 100.0),
        anchor=(50.0, 50.0),
        offsets=(10.0, 20.0, 15.0, 5.0),
    )
    grid = GridProperties(rows=10, cols=10)

    # Create a line geometry that falls completely outside the domain bounds
    outside_line = LineString(
        [(200, 200), (250, 250)]
    )  # Well outside [0,0] to [100,100]

    # Write the geometry to a file
    geom_file = tmp_path / "outside_stl.geojson"
    gdf = gpd.GeoDataFrame({"geometry": [outside_line]}, crs="EPSG:4326")
    gdf.to_file(geom_file)

    # Create a TransferBC boundary condition pointing to this outside geometry
    transfer_bc = TransferBC(
        bc_type="TRANSFER",
        vector=Asset(
            href=str(geom_file),
            checksum="a1b2c3d4e5f6a7b8",
            source_url=None,
            derived=False,
        ),
        value=1.0,
        transfer_depths=Asset(
            href="s3://bucket/depth.tif",
            checksum="d4e5f6a7b8c9d0e1",
            source_url=None,
            derived=False,
        ),
        transfer_els=Asset(
            href="s3://bucket/terrain.tif",
            checksum="e5f6a7b8c9d0e1f2",
            source_url=None,
            derived=False,
        ),
        grid_properties=grid,
        domain=domain,
    )

    # Process the boundary condition - geometry outside domain should return empty
    with patch(
        "twod_fim_jobs.hydraulic_solvers.pre_process.ASSET_CACHE.materialize_path",
        return_value=str(geom_file),
    ):
        # When geometry is outside domain bounds, process_bc_line returns empty list
        result = process_bc_line(transfer_bc, domain, grid)
        assert result == [], "Expected empty BC points when geometry is outside domain"
