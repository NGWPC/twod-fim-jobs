"""Tests for scenario naming utilities - readable function-based tests."""

import pytest
from twod_fim_jobs.utils.naming import get_scenario_dir_name, get_scenario_code


# Rounding precision values used (from consts):
# - RUN_NAME_KWSE_ROUNDING_PRECISION = 1 (KWSE: 1 decimal place)
# - RUN_NAME_Q_ROUNDING_PRECISION = 0 (Q/discharge: 0 decimal places)
# - RUN_NAME_SLOPE_ROUNDING_PRECISION = 1 (ND slope: scientific notation 1 decimal)


def test_scenario_code_kwse_basic():
    """scenario_code with KWSE (height-fixed) boundary condition"""
    code = get_scenario_code(kwse_value=200.2, nd_value=None, q_value=1000.0)
    assert code == "KWSE200.2Q1000"


def test_scenario_code_nd_basic():
    """scenario_code with ND (free slope) boundary condition"""
    code = get_scenario_code(kwse_value=None, nd_value=0.00012, q_value=1000.0)
    assert code == "ND1.2E04Q1000"


def test_scenario_code_kwse_decimal_precision():
    """scenario_code with KWSE respects 1 decimal place formatting"""
    code = get_scenario_code(kwse_value=50.0, nd_value=None, q_value=2000.0)
    assert code == "KWSE50.0Q2000"


def test_scenario_code_nd_very_small():
    """scenario_code with ND very small value (large negative exponent)"""
    code = get_scenario_code(kwse_value=None, nd_value=0.000015, q_value=1000.0)
    assert code == "ND1.5E05Q1000"


def test_dir_name_kwse_basic():
    """dir_name with KWSE boundary condition"""
    dirname = get_scenario_dir_name(kwse_value=200.2, nd_value=None, q_value=1000.0)
    assert dirname == "kwse=200.2/q=1000"


def test_dir_name_nd_basic():
    """dir_name with ND boundary condition"""
    dirname = get_scenario_dir_name(kwse_value=None, nd_value=0.00012, q_value=1000.0)
    assert dirname == "nd=1.2E04/q=1000"


def test_dir_name_kwse_various_q():
    """dir_name with KWSE and various Q values"""
    dirname = get_scenario_dir_name(kwse_value=50.0, nd_value=None, q_value=2000.0)
    assert dirname == "kwse=50.0/q=2000"


def test_dir_name_nd_various_q():
    """dir_name with ND and various Q values"""
    dirname = get_scenario_dir_name(kwse_value=None, nd_value=0.0013, q_value=500.0)
    assert dirname == "nd=1.3E03/q=500"


def test_edge_zero_kwse():
    """edge case: KWSE value is exactly zero"""
    code = get_scenario_code(kwse_value=0.0, nd_value=None, q_value=1000.0)
    assert code == "KWSE0.0Q1000"


def test_edge_zero_kwse_dirname():
    """edge case: KWSE zero in directory name"""
    dirname = get_scenario_dir_name(kwse_value=0.0, nd_value=None, q_value=1000.0)
    assert dirname == "kwse=0.0/q=1000"


def test_edge_large_kwse():
    """edge case: KWSE with very large value"""
    # 9999.99 with 1 decimal precision rounds to 10000.0
    code = get_scenario_code(kwse_value=9999.99, nd_value=None, q_value=1000.0)
    assert code == "KWSE10000.0Q1000"


def test_edge_large_q():
    """edge case: Discharge with very large value"""
    code = get_scenario_code(kwse_value=200.0, nd_value=None, q_value=100000.0)
    assert code == "KWSE200.0Q100000"


def test_edge_very_small_nd():
    """edge case: ND with tiny value (large negative exponent)"""
    code = get_scenario_code(kwse_value=None, nd_value=1.7e-10, q_value=1000.0)
    assert code == "ND1.7E10Q1000"


def test_edge_fractional_q():
    """edge case: Discharge with fractional value < 1"""
    code = get_scenario_code(kwse_value=200.0, nd_value=None, q_value=0.5)
    assert code == "KWSE200.0Q0"  # Q precision is 0 decimals, so 0.5 rounds to 0


def test_error_neither_kwse_nor_nd():
    """error case: Neither KWSE nor ND provided should raise ValueError"""
    with pytest.raises(
        ValueError, match="Either kwse_value or nd_value must be provided"
    ):
        get_scenario_code(kwse_value=None, nd_value=None, q_value=1000.0)


def test_error_neither_kwse_nor_nd_dirname():
    """error case: Neither KWSE nor ND in dir_name should raise ValueError"""
    with pytest.raises(
        ValueError, match="Either kwse_value or nd_value must be provided"
    ):
        get_scenario_dir_name(kwse_value=None, nd_value=None, q_value=1000.0)


def test_kwse_and_nd_together():
    """error case: Neither KWSE nor ND in dir_name should raise ValueError"""
    code = get_scenario_code(kwse_value=200.2, nd_value=1.6e-3, q_value=100)
    assert code == "KWSE200.2Q100"
