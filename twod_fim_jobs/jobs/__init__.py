from .build_model import BuildModelJob
from .health import HealthWorkflow

WORKFLOWS = {
    "build-model": BuildModelJob,
    "health": HealthWorkflow,
}
