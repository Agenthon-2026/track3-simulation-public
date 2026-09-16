"""Parser crashes must fail one invocation without losing its host evidence."""

import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import benchmark_candidates as bench  # noqa: E402
import verify_candidate_output as verifier  # noqa: E402


def test_execution_checkpoint_survives_parser_crash(tmp_path, monkeypatch):
    from throughput import run_unit as runner

    unit = tmp_path / "unit"
    unit.mkdir()
    (unit / "scenario.json").write_text("{}")
    logs = tmp_path / "logs"
    monkeypatch.setattr(
        runner,
        "timed_container_run",
        lambda command, **kwargs: (
            subprocess.CompletedProcess(command, 0, b"finished", b""),
            12.5,
            None,
            123456,
        ),
    )

    def crash(*args):
        raise MemoryError("parser allocation")

    monkeypatch.setattr(runner, "_host_n_events", crash)
    with pytest.raises(MemoryError, match="parser allocation"):
        runner.run_once("fixture", unit, batch=False, log_dir=logs)
    checkpoint = json.loads((logs / "execution.json").read_text())
    assert checkpoint["returncode"] == 0
    assert checkpoint["host_wall_clock_sec"] == 12.5
    assert checkpoint["host_peak_memory_bytes"] == 123456
    assert checkpoint["rankable"] is False
    assert (logs / "stdout.log").read_bytes() == b"finished"


@pytest.mark.parametrize("failure", ["crash", "timeout"])
def test_verifier_failure_preserves_measurement_and_stops(
    tmp_path, monkeypatch, failure
):
    from throughput import run_unit as runner

    plan = {
        "purpose": "baseline",
        "host": {},
        "repeats": 5,
        "roster": [{"unit": "fixture", "batch": False, "input_sha256": "input"}],
        "images": {"A": "image"},
        "budget_sec": 30,
        "timeout_sec": 10,
        "plan_sha256": "fixture",
        "history_dir": str(tmp_path / "history"),
    }
    path = tmp_path / "plan.json"
    bench.save(path, plan)
    monkeypatch.setattr(bench, "validate_plan", lambda *a, **kw: None)
    monkeypatch.setattr(bench, "host", lambda: {})
    launches = []

    def run(*args, **kwargs):
        launches.append(args)
        kwargs["keep_output"].mkdir()
        return runner.UnitRun(10, 10, 10, 1, None, 123456, 0)

    def failed_verification(unit, output, timeout):
        checkpoint = output.parent / "measurement.json"
        assert json.loads(checkpoint.read_text())["host_peak_memory_bytes"] == 123456
        if failure == "timeout":
            raise subprocess.TimeoutExpired("verifier", timeout)
        raise subprocess.CalledProcessError(-9, "verifier")

    monkeypatch.setattr(runner, "run_once", run)
    monkeypatch.setattr(bench, "gate_output", failed_verification)
    evidence = bench.run_plan(path, tmp_path / "runs", diagnostic=True)
    assert evidence["stop_reason"] == "unit-failed" and len(launches) == 1
    record = bench.read_index(evidence["runs"][0])
    assert record["status"] == "failed"
    assert record["measurement"]["n_events"] == 10
    assert bench.read_index(record["checkpoints"][0])["host_wall_clock_sec"] == 1


@pytest.mark.skipif(
    sys.platform != "linux", reason="RLIMIT_AS is enforced on Linux qualification hosts"
)
def test_memory_limit_refuses_allocation_in_child():
    code = "import sys;sys.path.insert(0,sys.argv[1]);from verify_candidate_output import limit_memory;limit_memory(128*1024**2);bytearray(256*1024**2)"
    result = subprocess.run(
        [sys.executable, "-c", code, str(ROOT / "scripts")],
        capture_output=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert b"MemoryError" in result.stderr


def test_verifier_deadline_is_enforced(monkeypatch, tmp_path):
    def child(command, **kwargs):
        assert kwargs["timeout"] == 0.01
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", child)
    with pytest.raises(subprocess.TimeoutExpired):
        verifier.bounded_verify(tmp_path, tmp_path, 0.01)
    with pytest.raises(TimeoutError):
        verifier.bounded_verify(tmp_path, tmp_path, 0)


@pytest.mark.parametrize(
    "stop,exit_code", [("unit-failed", 1), ("deadline", 1), ("completed-once", 0)]
)
def test_failed_cli_run_exits_nonzero(monkeypatch, tmp_path, stop, exit_code):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "benchmark",
            "run",
            "--plan",
            str(tmp_path / "plan"),
            "--out",
            str(tmp_path / "out"),
        ],
    )
    monkeypatch.setattr(bench, "run_plan", lambda *a: {"stop_reason": stop})
    assert bench.main() == exit_code
