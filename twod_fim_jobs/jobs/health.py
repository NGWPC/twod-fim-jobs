from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

from twod_fim_jobs.jobs.common import Job
from twod_fim_jobs.utils.storage import copy_file

logger = logging.getLogger(__name__)


class HealthInputs(BaseModel):
    """Inputs for the health workflow."""

    test_write_uri: Optional[str] = None
    """Optional local path to write a small sentinel file to verify write access."""


class HealthResult(BaseModel):
    """Result of the health workflow."""

    passed: bool = True


class HealthWorkflow(Job[HealthInputs]):
    """Verify the container environment is intact."""

    Inputs = HealthInputs

    def _run(self, inputs: HealthInputs, tmp_dir: Path) -> HealthResult:
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

        logger.info("Health check passed.")

        if inputs.test_write_uri is not None:
            tmp_path = tmp_dir / "health_check.txt"
            tmp_path.write_bytes(b"health check\n")
            copy_file(tmp_path, inputs.test_write_uri)

        return HealthResult()
