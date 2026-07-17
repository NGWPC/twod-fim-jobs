import logging
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, ClassVar, Generic, TypeVar

from pydantic import BaseModel

T_Inputs = TypeVar("T_Inputs", bound=BaseModel)


class Job(ABC, Generic[T_Inputs]):
    """Base class for all jobs.

    Using a class rather than a plain function gives every job a consistent
    structure that enables shared behaviour to be added once here and inherited
    automatically. Concretely this means:

    - **Input validation** — ``Inputs`` is a Pydantic model, so bad arguments
      are caught before any work starts.
    - **Logging** — the base class can log job name, inputs, and duration
      without each job having to repeat that code.
    - **Metrics** — timing, success/failure, and output size can be captured
      in one place.
    - **Discovery** — the ``WORKFLOWS`` registry in ``jobs/__init__.py`` can
      find and load all jobs by scanning for subclasses, which is how the CLI
      builds its subcommands automatically.
    """

    Inputs: ClassVar[type[BaseModel]]

    def run(self, inputs: dict[str, Any]) -> Any:
        """Validate inputs, configure logging, provide a temp directory, and delegate to ``_job``."""
        validated: T_Inputs = self.Inputs.model_validate(inputs)  # type: ignore[assignment]

        logger = logging.getLogger(type(self).__name__)
        logger.info("Starting job %s", type(self).__name__)

        with tempfile.TemporaryDirectory() as tmp_dir:
            result = self._run(validated, Path(tmp_dir))

        logger.info("Finished job %s", type(self).__name__)
        return result

    @abstractmethod
    def _run(self, inputs: T_Inputs, tmp_dir: Path) -> Any:
        """Execute the job. Subclasses must implement this method."""
