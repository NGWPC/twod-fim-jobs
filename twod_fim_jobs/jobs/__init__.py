from .build_model import BuildModelJob
from .health import HealthWorkflow
from .modify_network import ModifyNetworkJob
from .run_nd_scenarios import RunNDScenariosJob

WORKFLOWS = {
    "health": HealthWorkflow,
    "modify_network": ModifyNetworkJob,
    "build_model": BuildModelJob,
    "run_nd_scenarios": RunNDScenariosJob,
}
