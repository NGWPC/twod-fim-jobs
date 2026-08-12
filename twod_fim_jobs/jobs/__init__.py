from .build_model import BuildModelJob
from .health import HealthWorkflow
from .modify_network import ModifyNetworkJob

WORKFLOWS = {
    "build_model": BuildModelJob,
    "health": HealthWorkflow,
    "modify_network": ModifyNetworkJob,
}
