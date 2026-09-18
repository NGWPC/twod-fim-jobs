import logging
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, ClassVar, Generic, TypeVar

from pydantic import BaseModel
import json
from twod_fim_jobs.consts import (
    PRINT_SEPEX_STYLE_RESULTS,
)

T_Inputs = TypeVar("T_Inputs", bound=BaseModel)

logger = logging.getLogger(__name__)


class Job(ABC, Generic[T_Inputs]):
    """Provide a consistent structure for all jobs with shared input validation and logging."""

    Inputs: ClassVar[type[BaseModel]]

    def run(self, inputs: dict[str, Any]) -> Any:
        """Validate inputs, configure logging, provide a temp directory, and delegate to ``_run``."""
        validated: T_Inputs = self.Inputs.model_validate(inputs)  # type: ignore[assignment]

        logger.info("Starting job %s", type(self).__name__)

        with tempfile.TemporaryDirectory() as tmp_dir:
            result = self._run(validated, Path(tmp_dir))

        logger.info("Finished job %s", type(self).__name__)

        if PRINT_SEPEX_STYLE_RESULTS:
            print(json.dumps({"plugin_results": result.model_dump()}), flush=True)

        return result

    @abstractmethod
    def _run(self, inputs: T_Inputs, tmp_dir: Path) -> Any:
        """Execute the job logic; subclasses must implement this method."""
