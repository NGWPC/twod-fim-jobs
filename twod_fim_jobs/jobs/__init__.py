from .build_model import BuildModelWorkflow
from .health import HealthWorkflow

WORKFLOWS = {
    "build-model": BuildModelWorkflow,
    "health": HealthWorkflow,
}
