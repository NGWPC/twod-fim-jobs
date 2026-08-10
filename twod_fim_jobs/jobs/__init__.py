from .build_model import BuildModelJob
from .health import HealthWorkflow
from .run_kwse_scenarios import RunKWSEScenariosJob
from .run_nd_scenarios import RunNDScenariosJob

WORKFLOWS = {
    "health": HealthWorkflow,
    "build_model": BuildModelJob,
    "run_nd_scenarios": RunNDScenariosJob,
}
