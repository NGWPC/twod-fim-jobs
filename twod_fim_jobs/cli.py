# cli.py

import argparse
import json
import logging
from datetime import datetime, timezone

from twod_fim_jobs.jobs import WORKFLOWS

_QUIET_LOGGERS = ["rasterio", "pyogrio"]


def _parse_payload(raw_payload: str, parser: argparse.ArgumentParser) -> dict:
    """Parse and validate the JSON payload passed to the CLI."""
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError as exc:
        parser.error(f"Invalid JSON payload: {exc}")

    if not isinstance(payload, dict):
        parser.error("JSON payload must be an object.")

    return payload


def build_parser() -> argparse.ArgumentParser:
    # Accept a job name and a JSON payload with workflow inputs.
    parser = argparse.ArgumentParser(prog="twod-fim")
    parser.add_argument(
        "job",
        help="Workflow name to execute.",
    )
    parser.add_argument(
        "payload",
        help=(
            "JSON payload containing workflow inputs. "
            'Example: \'{"reach_id":123,"db_uri":"sqlite:////tmp/db.gpkg"}\''
        ),
    )
    return parser


class _JsonLinesFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {
                "level": record.levelname,
                "msg": record.getMessage(),
                "time": datetime.fromtimestamp(
                    record.created, tz=timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        )


def _configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(_JsonLinesFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def main():
    _configure_logging()
    # Parse arguments
    parser = build_parser()
    args = parser.parse_args()
    job = args.job
    if job not in WORKFLOWS:
        valid_jobs = ", ".join(sorted(WORKFLOWS))
        parser.error(f"Unknown job '{job}'. Valid jobs: {valid_jobs}")
    input_dict = _parse_payload(args.payload, parser)

    # Initialize workflow
    workflow_cls = WORKFLOWS[job]
    workflow = workflow_cls()

    # Run workflow
    workflow.run(input_dict)


if __name__ == "__main__":
    main()
