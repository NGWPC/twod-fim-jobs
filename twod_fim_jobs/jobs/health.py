from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

from twod_fim_jobs.jobs.shared import Job
from twod_fim_jobs.utils.storage import copy_file


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
            with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as tmp:
                tmp.write(b"health check\n")
                tmp_path = tmp.name
            try:
                copy_file(tmp_path, inputs.test_write_uri)
            finally:
                Path(tmp_path).unlink(missing_ok=True)
