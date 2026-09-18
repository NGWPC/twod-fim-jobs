"""Scenario naming and formatting utilities."""

from twod_fim_jobs.consts import (
    RUN_NAME_KWSE_ROUNDING_PRECISION,
    RUN_NAME_Q_ROUNDING_PRECISION,
    RUN_NAME_SLOPE_ROUNDING_PRECISION,
)


def format_downstream_string(
    kwse_value: float | None, nd_value: float | None, include_prefix: bool = False
) -> str:
    """Format downstream boundary condition value for naming."""
    if kwse_value is not None:
        formatted_kwse = f"{kwse_value:.{RUN_NAME_KWSE_ROUNDING_PRECISION}f}"
        prefix = "KWSE" if include_prefix else "kwse="
        return f"{prefix}{formatted_kwse}"
    elif nd_value is not None:
        formatted_nd = (
            f"{nd_value:.{RUN_NAME_SLOPE_ROUNDING_PRECISION}e}".replace("-", "")
            .replace("+", "")
            .replace("e", "E")
        )
        prefix = "ND" if include_prefix else "nd="
        return f"{prefix}{formatted_nd}"
    else:
        raise ValueError("Either kwse_value or nd_value must be provided")


def format_upstream_string(q_value: float, include_prefix: bool = False) -> str:
    """Format upstream (discharge) boundary condition value for naming."""
    formatted_q = f"{q_value:.{RUN_NAME_Q_ROUNDING_PRECISION}f}"
    prefix = "Q" if include_prefix else "q="
    return f"{prefix}{formatted_q}"


def get_scenario_dir_name(
    kwse_value: float | None, nd_value: float | None, q_value: float
) -> str:
    """Get scenario directory name from boundary condition values."""
    ds_str = format_downstream_string(kwse_value, nd_value, include_prefix=False)
    us_str = format_upstream_string(q_value, include_prefix=False)
    return f"{ds_str}/{us_str}"


def get_scenario_code(
    kwse_value: float | None, nd_value: float | None, q_value: float
) -> str:
    """Get scenario code from boundary condition values."""
    ds_str = format_downstream_string(kwse_value, nd_value, include_prefix=True)
    us_str = format_upstream_string(q_value, include_prefix=True)
    return f"{ds_str}{us_str}"
