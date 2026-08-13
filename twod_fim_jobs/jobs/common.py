import logging
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, ClassVar, Generic, TypeVar

from pydantic import BaseModel
import json
from twod_fim_jobs.consts import (
    RUN_NAME_SLOPE_ROUNDING_PRECISION,
    RUN_NAME_Q_ROUNDING_PRECISION,
    RUN_NAME_KWSE_ROUNDING_PRECISION,
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


def make_scenario_code(bc_type: str, bc_value: float, q: float) -> str:
    """Build a scenario code string, e.g. KWSE200.2Q1000."""
    rounded = round(bc_value, RUN_NAME_KWSE_ROUNDING_PRECISION)
    val_str = f"{rounded:.{RUN_NAME_KWSE_ROUNDING_PRECISION}f}".rstrip("0").rstrip(".")
    q_str = f"{round(q)}"
    return f"{bc_type}{val_str}Q{q_str}"


def make_scenario_dir_name(
    q: float, kwse: float | None = None, nd: float | None = None
) -> str:
    """Build the output directory name for a scenario run."""
    if (kwse is None) == (nd is None):
        raise ValueError("Exactly one of 'kwse' or 'nd' must be provided")
    if nd is not None:
        ds_str = f"nd={f'{nd:.{RUN_NAME_SLOPE_ROUNDING_PRECISION}e}'.replace('-', '').replace('e', 'E')}"
    else:
        ds_str = f"kwse={f'{kwse:.{RUN_NAME_KWSE_ROUNDING_PRECISION}f}'}"
    us_str = f"q={q:.{RUN_NAME_Q_ROUNDING_PRECISION}f}"
    return f"{ds_str}/{us_str}"
