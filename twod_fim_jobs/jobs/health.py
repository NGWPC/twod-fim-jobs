from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic import BaseModel

from twod_fim_jobs.jobs.shared import Job


class HealthInputs(BaseModel):
    """Inputs for the health workflow."""

    test_write_uri: Optional[str] = None
    """Optional local path to write a small sentinel file to verify write access."""


class HealthWorkflow(Job):
    """Verify the container environment is intact."""

    Inputs = HealthInputs

    def run(self, inputs: HealthInputs) -> None:
        # Eagerly import every job module so that broken environments (e.g.
        # missing GDAL shared libraries) surface here rather than silently at
        # job dispatch time.  New job modules are covered automatically.
        import importlib
        import pkgutil

        import twod_fim_jobs.jobs as _jobs_pkg

        for module_info in pkgutil.walk_packages(
            path=_jobs_pkg.__path__,
            prefix=_jobs_pkg.__name__ + ".",
        ):
            importlib.import_module(module_info.name)

        print("Health check passed.")

        if inputs.test_write_uri is not None:
            dest = Path(inputs.test_write_uri)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text("health-check-ok\n")
            print(f"Write check passed: {dest}")
