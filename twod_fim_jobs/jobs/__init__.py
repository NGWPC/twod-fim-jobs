from .build_model import BuildModelJob
from .health import HealthWorkflow

WORKFLOWS = {
    "build_model": BuildModelJob,
    "health": HealthWorkflow,
}
