# cli.py

import argparse
import json

from twod_fim_jobs.jobs import WORKFLOWS


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


def main():
    # Parse arguments
    parser = build_parser()
    args = parser.parse_args()
    job = args.job
    if job not in WORKFLOWS:
        valid_jobs = ", ".join(sorted(WORKFLOWS))
        parser.error(f"Unknown job '{job}'. Valid jobs: {valid_jobs}")
    input_values = _parse_payload(args.payload, parser)

    # Initialize workflow
    workflow_cls = WORKFLOWS[job]
    workflow = workflow_cls()
    input_model = workflow_cls.Inputs
    inputs = input_model.model_validate(input_values)

    # Run workflow
    workflow.run(inputs)


if __name__ == "__main__":
    main()
