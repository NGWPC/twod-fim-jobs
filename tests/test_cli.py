import pytest
from pydantic import BaseModel

from twod_fim_jobs import cli
from twod_fim_jobs.jobs.common import Job


class DummyInputs(BaseModel):
    value: int


class DummyWorkflow(Job):
    Inputs = DummyInputs
    last_inputs = None

    def _run(self, inputs: DummyInputs, _):
        DummyWorkflow.last_inputs = inputs
        return inputs


def test_main_accepts_single_json_payload(monkeypatch):
    monkeypatch.setattr(cli, "WORKFLOWS", {"dummy": DummyWorkflow})
    monkeypatch.setattr(
        "sys.argv",
        [
            "twod-fim",
            "dummy",
            '{"value":123}',
        ],
    )

    cli.main()

    assert DummyWorkflow.last_inputs is not None
    assert DummyWorkflow.last_inputs.value == 123


def test_main_exits_on_invalid_json(monkeypatch):
    monkeypatch.setattr(cli, "WORKFLOWS", {"dummy": DummyWorkflow})
    monkeypatch.setattr("sys.argv", ["twod-fim", "dummy", "{not-json}"])

    with pytest.raises(SystemExit):
        cli.main()


def test_main_exits_on_missing_job(monkeypatch):
    monkeypatch.setattr(cli, "WORKFLOWS", {"dummy": DummyWorkflow})
    monkeypatch.setattr("sys.argv", ["twod-fim", '{"value":1}'])

    with pytest.raises(SystemExit):
        cli.main()


def test_main_exits_on_unknown_job(monkeypatch):
    monkeypatch.setattr(cli, "WORKFLOWS", {"dummy": DummyWorkflow})
    monkeypatch.setattr(
        "sys.argv",
        ["twod-fim", "not-a-workflow", '{"value":1}'],
    )

    with pytest.raises(SystemExit):
        cli.main()


def test_main_exits_on_non_object_inputs(monkeypatch):
    monkeypatch.setattr(cli, "WORKFLOWS", {"dummy": DummyWorkflow})
    monkeypatch.setattr(
        "sys.argv",
        ["twod-fim", "dummy", "[1,2,3]"],
    )

    with pytest.raises(SystemExit):
        cli.main()
