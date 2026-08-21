from twod_fim_jobs.consts import SCENARIO_SOLVER, SDR_COMMIT, SupportedSolver
import subprocess
import re

from twod_fim_jobs.models.solvers import RunIdentity, SolverInfo
from twod_fim_jobs.utils.hashing import hash_dict


def get_model_version(solver: SupportedSolver) -> str:
    if solver == SupportedSolver.LISFLOOD:
        return get_lisflood_version()
    elif solver == SupportedSolver.SFINCS:
        return get_sfincs_version()
    else:
        supported = [e.value for e in SupportedSolver]
        raise ValueError(
            f"Tried to get version of solver {solver}, but only {supported} are supported"
        )


def get_lisflood_version() -> str:
    """Get LISFLOOD-FP version by running the lisflood command."""
    result = subprocess.run(["lisflood"], capture_output=True, text=True)
    output = result.stdout + result.stderr

    # Match pattern: "LISFLOOD-FP version X.X.X"
    match = re.search(r"LISFLOOD-FP version ([\d.]+)", output)
    if match:
        return match.group(1)

    raise RuntimeError(f"Could not parse LISFLOOD version from output:\n{output}")


def get_sfincs_version() -> str:
    raise NotImplementedError("Have not added support for sfincs solver yet.")


def get_run_identity() -> RunIdentity:
    "Make canonical identity for solver and sdr commit id."
    version = get_model_version(SCENARIO_SOLVER)
    return RunIdentity(
        sdr_commit_id=SDR_COMMIT,
        solver=SolverInfo(name=SCENARIO_SOLVER.value, version=version),
    )


def get_run_identity_hash() -> str:
    "Make canonical identity hash for solver and sdr commit id."
    return hash_dict(get_run_identity().model_dump(), role_length=8)
