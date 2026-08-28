# cli.py

import argparse
import json
import logging
import sys
import traceback
from datetime import datetime, timezone
from typing import NoReturn

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


class _LoggingArgumentParser(argparse.ArgumentParser):
    """An ArgumentParser whose errors are log lines like everything else.

    argparse writes usage and the error straight to stderr, which would be the
    last unstructured output left in a job. Same exit status (2), so callers
    that check the code see no change.
    """

    def error(self, message: str) -> NoReturn:
        logging.getLogger("twod_fim_jobs").critical("Invalid invocation: %s", message)
        raise SystemExit(2)


def build_parser() -> argparse.ArgumentParser:
    # Accept a job name and a JSON payload with workflow inputs.
    parser = _LoggingArgumentParser(prog="twod-fim")
    parser.add_argument(
        "job",
        help="Workflow name to execute.",
    )
    parser.add_argument(
        "payload",
        help=(
            "JSON payload containing workflow inputs. "
            'Example: \'{"reach_id":123,"reach_network_path":"s3://bucket/reach_network.parquet"}\''
        ),
    )
    return parser


class _JsonLinesFormatter(logging.Formatter):
    """One line, one JSON object: {"level", "msg", "time"}.

    SEPEX unmarshals every log line into this shape (api/jobs/jobs.go,
    LogEntry). A line that is not valid JSON, or that carries no "msg", is kept
    verbatim as the message with a zero timestamp and no level — which is how a
    traceback ends up displayed as fifty entries dated 0001-01-01.

    Newlines are safe here because json.dumps escapes them, so a multi-line
    message (a traceback, a warning with its source line) stays one line on the
    wire and one entry in the viewer.
    """

    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        # format() is overridden wholesale, so the base class's exception
        # handling does not run; without this, logger.exception() would silently
        # drop the traceback it was called to record.
        if record.exc_info:
            msg = msg + "\n" + "".join(traceback.format_exception(*record.exc_info))
        if record.stack_info:
            msg = msg + "\n" + record.stack_info
        return json.dumps(
            {
                "level": record.levelname,
                "msg": msg,
                "time": datetime.fromtimestamp(
                    record.created, tz=timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        )


def _log_uncaught_exception(exc_type, exc, tb) -> None:
    """Report a crash as one structured line instead of a raw traceback.

    The interpreter still exits non-zero afterwards, so the job is reported
    failed exactly as before; only the shape of what it wrote changes.
    """
    logging.getLogger("twod_fim_jobs").critical(
        "Job failed: %s: %s", exc_type.__name__, exc, exc_info=(exc_type, exc, tb)
    )


def _configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(_JsonLinesFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    # Warnings go to stderr through their own channel, not through logging, so
    # without this a single SAWarning arrives as two unstructured lines (the
    # warning and the source line that raised it).
    logging.captureWarnings(True)
    sys.excepthook = _log_uncaught_exception


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
