from .build_model import BuildModelJob
from .health import HealthWorkflow
from .run_nd_scenarios import RunNDScenariosJob
from .run_kwse_scenarios import RunKWSEScenariosJob

WORKFLOWS = {
    "health": HealthWorkflow,
    "build_model": BuildModelJob,
    "run_nd_scenarios": RunNDScenariosJob,
    "run_kwse_scenarios": RunKWSEScenariosJob,
}
