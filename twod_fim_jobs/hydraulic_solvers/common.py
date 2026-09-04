from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from twod_fim_jobs.hydraulic_solvers.identities import (
    get_run_identity,
    get_run_identity_hash,
)
from twod_fim_jobs.hydraulic_solvers.post_process import post_process_lisflood
from twod_fim_jobs.hydraulic_solvers.pre_process import write_model_files
from twod_fim_jobs.hydraulic_solvers.run import solve_scenario
from twod_fim_jobs.models.common import Asset
from twod_fim_jobs.models.solvers import (
    PostProcessResult,
    RunScenarioInputs,
    RunScenarioManifest,
    RunScenarioResults,
    ScenarioAssets,
    SolveScenarioResults,
)
from twod_fim_jobs.utils.hashing import hash_file
from twod_fim_jobs.utils.storage import (
    check_file_exists,
    copy_dir,
    copy_file,
    read_json,
    write_json,
)


def run_scenario(
    run_scenario_inputs: RunScenarioInputs, working_dir: Path
) -> RunScenarioManifest:
    completed = check_run_exists(run_scenario_inputs)
    if completed is not None:
        return completed

    config_path = write_model_files(run_scenario_inputs, working_dir)
    solve_scenario_results = solve_scenario(
        config_path, run_scenario_inputs, working_dir
    )
    processed = post_process_lisflood(run_scenario_inputs, working_dir)

    # Publish results to out location
    scenario_manifest = publish_scenario(
        run_scenario_inputs, solve_scenario_results, processed
    )

    return scenario_manifest


def check_run_exists(
    run_scenario_inputs: RunScenarioInputs,
) -> RunScenarioManifest | None:
    if not check_file_exists(run_scenario_inputs.manifest_href):
        return None
    try:
        manifest = RunScenarioManifest.model_validate_json(
            read_json(run_scenario_inputs.manifest_href)
        )
        if manifest.inputs == run_scenario_inputs:
            return manifest
        else:
            return None
    except ValidationError:
        return None


def publish_scenario(
    run_scenario_inputs: RunScenarioInputs,
    solve_scenario_results: SolveScenarioResults,
    processed: PostProcessResult,
) -> RunScenarioManifest:
    """Publish assets and manifest to final locations."""
    # Make assets
    dest_depth_str = (
        f"{run_scenario_inputs.scenario_out_dir}/{processed.depth_path.name}"
    )
    dest_inun_str = f"{run_scenario_inputs.scenario_out_dir}/{processed.inundation_polygon_path.name}"
    dest_stl_str = f"{run_scenario_inputs.scenario_out_dir}/{processed.stl_path.name}"
    depth_asset = Asset(
        href=dest_depth_str,
        checksum=hash_file(processed.depth_path, role_length=16),
        source_url=None,
        derived=True,
    )
    inun_asset = Asset(
        href=dest_inun_str,
        checksum=hash_file(processed.inundation_polygon_path, role_length=16),
        source_url=None,
        derived=True,
    )
    stl_asset = Asset(
        href=dest_stl_str,
        checksum=hash_file(processed.stl_path, role_length=16),
        source_url=None,
        derived=True,
    )
    dest_zarr = None
    zarr_asset = None
    if processed.zarr_path is not None:
        dest_zarr = f"{run_scenario_inputs.scenario_out_dir}/{processed.zarr_path.name}"
        zarr_asset = Asset(
            href=dest_zarr,
            checksum=hash_file(processed.zarr_path, role_length=16),
            source_url=None,
            derived=True,
        )

    assets = ScenarioAssets(
        depth=depth_asset,
        inundation_polygon=inun_asset,
        stage_transfer_line=stl_asset,
        zarr_store=zarr_asset,
    )

    # Make results
    run_scenario_results = RunScenarioResults(
        volume_convergence=solve_scenario_results.volume_convergence,
        termination_condition=solve_scenario_results.termination_condition,
        wall_time=solve_scenario_results.wall_time,
        nominal_wse=processed.nominal_wse,
        us_discharge=run_scenario_inputs.inflow,
        sim_time=processed.sim_time,
        max_depth=solve_scenario_results.max_depth,
        median_depth=solve_scenario_results.median_depth,
        extent_percent=solve_scenario_results.extent_percent,
    )

    # Make manifest
    manifest = RunScenarioManifest(
        created_at=datetime.now(timezone.utc),
        reach_id=run_scenario_inputs.reach_id,
        identity_hash=get_run_identity_hash(),
        scenario_code=run_scenario_inputs.scenario_code,
        model_id=run_scenario_inputs.model_id,
        identity=get_run_identity(),
        self_href=run_scenario_inputs.manifest_href,
        inputs=run_scenario_inputs,
        properties=run_scenario_results,
        assets=assets,
        warnings=[],
    )

    # Upload
    copy_file(processed.depth_path, dest_depth_str)
    copy_file(processed.inundation_polygon_path, dest_inun_str)
    copy_file(processed.stl_path, dest_stl_str)
    if dest_zarr is not None and processed.zarr_path is not None:
        copy_dir(processed.zarr_path, dest_zarr)
    write_json(manifest.self_href, manifest.model_dump_json())
    return manifest
