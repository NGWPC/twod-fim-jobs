from twod_fim_jobs.jobs.health import HealthResult, HealthWorkflow


def test_health_run_returns_result():
    result = HealthWorkflow().run({})
    assert isinstance(result, HealthResult)
    assert result.passed is True


def test_health_run_with_test_write_uri(tmp_path):
    dest = tmp_path / "sentinel.txt"
    HealthWorkflow().run({"test_write_uri": str(dest)})
    assert dest.exists()
