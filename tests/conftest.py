from unittest.mock import patch

import pytest

from lisflood_mock import fake_run_lisflood


@pytest.fixture
def mock_run_lisflood():
    with patch(
        "twod_fim_jobs.hydraulic_solvers.run.run_lisflood", new=fake_run_lisflood
    ):
        yield
