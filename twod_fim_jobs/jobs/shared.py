from abc import ABC, abstractmethod
from pydantic import BaseModel


class Job(ABC):
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

    Inputs: type[BaseModel]

    @abstractmethod
    def run(self, inputs: BaseModel):
        pass
