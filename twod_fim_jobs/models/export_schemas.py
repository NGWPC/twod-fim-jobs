"""Automatically exports json schemas for inputs, output manifest, and return payload for each job.

Writes results to schemas/ folder under job subfolders.
"""

import json
from pathlib import Path
from typing import TypeAlias

from pydantic import BaseModel

from twod_fim_jobs.models.build_model import (
    BuildModelInputs,
    BuildModelResult,
    ModelManifest,
)

SchemaSpec: TypeAlias = tuple[str, type[BaseModel]]

SCHEMAS: list[tuple[str, list[SchemaSpec]]] = [
    (
        "build_model",
        [
            ("inputs", BuildModelInputs),
            ("result", BuildModelResult),
            ("manifest", ModelManifest),
        ],
    ),
]


def export_schemas(schemas_dir: Path = Path("schemas")) -> None:
    for job_name, specs in SCHEMAS:
        job_dir = schemas_dir / job_name
        job_dir.mkdir(parents=True, exist_ok=True)
        for schema_name, model in specs:
            out_path = job_dir / f"{schema_name}.json"
            out_path.write_text(json.dumps(model.model_json_schema(), indent=2))
            print(f"Wrote {out_path}")


def main() -> None:
    export_schemas()


if __name__ == "__main__":
    main()
