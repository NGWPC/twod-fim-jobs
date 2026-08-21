import logging
from pathlib import Path
from twod_fim_jobs.hydraulic_solvers.common import run_scenario
from twod_fim_jobs.jobs.common import Job
from twod_fim_jobs.models.build_model import ModelManifest
from twod_fim_jobs.models.solvers import FreeBC, QFixBC, RunConfig, TransferBC
from twod_fim_jobs.models.run_kwse_scenarios import (
    RunKWSEScenariosInputs,
    RunKWSEScenariosResult,
)
from twod_fim_jobs.models.solvers import (
    RunScenarioInputs,
    RunScenarioManifest,
)
from twod_fim_jobs.utils.storage import read_json

logger = logging.getLogger(__name__)


class RunKWSEScenariosJob(Job[RunKWSEScenariosInputs]):
    """Initialize a 2D FIM model for a single reach."""

    Inputs = RunKWSEScenariosInputs

    def _run(
        self, inputs: RunKWSEScenariosInputs, tmp_dir: Path
    ) -> RunKWSEScenariosResult:
        model_manifest = ModelManifest.model_validate_json(
            read_json(inputs.model_manifest_path)
        )

        run_config = RunConfig(
            sim_time_seconds=inputs.max_simulation_length_seconds,
            save_interval_seconds=inputs.save_interval_seconds,
            volume_convergence_tolerance=inputs.volume_convergence_tolerance,
            allow_water_on_edges=inputs.allow_water_on_edges,
            max_simulation_wall_time_seconds=inputs.max_simulation_wall_time_seconds,
        )
        manifests = []
        for scenario in inputs.scenarios:
            ds_scenario_asset = RunScenarioManifest.model_validate_json(
                read_json(scenario.downstream_Scenario)
            )
            inflow_bc = QFixBC(
                bc_type="QFIX",
                vector=model_manifest.assets.inflow_line,
                value=scenario.upstream_discharge,
            )
            outflow_bc = FreeBC(
                bc_type="FREE",
                vector=ds_scenario_asset.assets.inundation_polygon,
                value=0.5,
            )
            transfer_bc = TransferBC(
                bc_type="TRANSFER",
                vector=ds_scenario_asset.assets.stage_transfer_line,
                value=scenario.bc_value,
                transfer_depths=ds_scenario_asset.assets.depth,
            )
            bcs = [inflow_bc, outflow_bc, transfer_bc]

            # Make scenario inputs
            run_scenario_inputs = RunScenarioInputs(
                domain=model_manifest.domain,
                grid_properties=model_manifest.properties.grid,
                terrain=model_manifest.assets.terrain,
                roughness=model_manifest.assets.roughness,
                boundary_conditions=bcs,
                hot_start=None,
                run_config=run_config,
                base_out_dir=inputs.model_results_base_path,
                reach_id=model_manifest.reach_id,
                model_id=model_manifest.model_id,
                tmp_dir=tmp_dir,
                centerline=model_manifest.assets.centerline,
            )

            # Make hotstart
            if scenario.hotstart is not None:
                hot_ds_bc_proxy = transfer_bc.model_copy(
                    update={"value": scenario.hotstart.bc_value}
                )
                hot_us_bc_proxy = inflow_bc.model_copy(
                    update={"value": scenario.hotstart.upstream_discharge}
                )
                hot_bc_proxy = [hot_us_bc_proxy, outflow_bc, hot_ds_bc_proxy]
                hot_scenario_proxy = run_scenario_inputs.model_copy(
                    update={"boundary_conditions": hot_bc_proxy}
                )
                hot_scenario_manifest = RunScenarioManifest.model_validate_json(
                    read_json(hot_scenario_proxy.manifest_href)
                )
                run_scenario_inputs.hot_start = hot_scenario_manifest.assets.depth

            scenario_manifest = run_scenario(run_scenario_inputs)
            manifests.append(scenario_manifest.self_href)

        return RunKWSEScenariosResult(manifests=manifests, warnings=[])
