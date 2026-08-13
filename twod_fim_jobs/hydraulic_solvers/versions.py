from twod_fim_jobs.consts import SupportedSolver
import subprocess
import re


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
