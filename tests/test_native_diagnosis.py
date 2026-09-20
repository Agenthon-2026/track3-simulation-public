"""Keep diagnostic failures visible and reject misleading measurement evidence.

These tests use real Parquet footers and temporary files. The participant launch
is replaced only in the failed-run test, so the unit suite never invokes Docker.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from scripts.diagnostics import instrument_native, run_diagnosis


def test_generator_rejects_changed_sort_boundary():
    source = instrument_native.DEFAULT_SOURCE.read_text()
    changed = source.replace(
        '        order = np.argsort(seq[keep], kind="stable")\n',
        "        order = another_sort(seq[keep])\n",
    )
    with pytest.raises(ValueError, match="Expected one diagnostic anchor, found 0"):
        instrument_native.generate(changed, "stage")


def test_generator_rejects_overwriting_production_source(tmp_path, monkeypatch, capsys):
    source = tmp_path / "_native.pyx"
    original = instrument_native.DEFAULT_SOURCE.read_bytes()
    source.write_bytes(original)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "instrument_native.py",
            "--source",
            str(source),
            "--out",
            str(source),
            "--mode",
            "stage",
        ],
    )
    with pytest.raises(SystemExit) as error:
        instrument_native.main()
    assert error.value.code == 2
    assert "never overwrite production source" in capsys.readouterr().err
    assert source.read_bytes() == original


@pytest.mark.parametrize("manifest_target", ["source", "output"])
def test_generator_rejects_manifest_aliasing_code(
    tmp_path, monkeypatch, capsys, manifest_target
):
    source = tmp_path / "_native.pyx"
    output = tmp_path / "_native_diagnostic.pyx"
    source.write_text("production sentinel")
    output.write_text("previous diagnostic sentinel")
    manifest = source if manifest_target == "source" else output
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "instrument_native.py",
            "--source",
            str(source),
            "--out",
            str(output),
            "--manifest",
            str(manifest),
        ],
    )
    with pytest.raises(SystemExit) as error:
        instrument_native.main()
    assert error.value.code == 2
    assert "Manifest must not overwrite" in capsys.readouterr().err
    assert source.read_text() == "production sentinel"
    assert output.read_text() == "previous diagnostic sentinel"


def make_output(root: Path, *, batch: bool) -> tuple[Path, Path, str]:
    output = root / "output"
    sub = output / "sub_00" if batch else output
    sub.mkdir(parents=True)
    table = pa.table({"t_ns": pa.array([101, 102], type=pa.int64())})
    trace = sub / "trace.parquet"
    pq.write_table(table, trace)
    trace_hash = hashlib.sha256(trace.read_bytes()).hexdigest()
    events_path = sub / "events.json"
    events_path.write_text(json.dumps({"n_events": 2, "trace_sha256": trace_hash}))
    total_path = output / "batch_events.json" if batch else events_path
    if batch:
        total_path.write_text(json.dumps({"total_events": 2}))
    return output, total_path, trace_hash


@pytest.mark.parametrize("batch", [False, True])
def test_output_metadata_uses_actual_footer_and_rejects_count_mismatch(tmp_path, batch):
    output, events_path, trace_hash = make_output(tmp_path, batch=batch)
    measured = run_diagnosis.output_metadata(output, batch)
    trace_key = "sub_00/trace.parquet" if batch else "trace.parquet"
    assert measured["actual_events"] == 2
    assert measured["parquet"][trace_key] == {"rows": 2, "sha256": trace_hash}
    events = json.loads(events_path.read_text())
    events["total_events" if batch else "n_events"] = 3
    events_path.write_text(json.dumps(events))
    with pytest.raises(AssertionError):
        run_diagnosis.output_metadata(output, batch)


@pytest.mark.parametrize("batch", [False, True])
def test_output_metadata_rejects_hash_mismatch_even_with_valid_row_count(
    tmp_path, batch
):
    output, _, _ = make_output(tmp_path, batch=batch)
    events_path = output / "sub_00/events.json" if batch else output / "events.json"
    events = json.loads(events_path.read_text())
    events["trace_sha256"] = "0" * 64
    events_path.write_text(json.dumps(events))
    with pytest.raises(AssertionError):
        run_diagnosis.output_metadata(output, batch)


def test_failed_participant_logs_survive_temporary_output_cleanup(
    tmp_path, monkeypatch
):
    from throughput import timer

    stderr = b"native simulator failed: diagnostic failure sentinel\n"
    stdout = b"started unit before failing\n"

    def failed_run(command, **_kwargs):
        return subprocess.CompletedProcess(command, 17, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(timer, "bounded_container_run", failed_run)
    unit = tmp_path / "unit"
    unit.mkdir()
    (unit / "scenario.json").write_text("{}")
    log_root = tmp_path / "durable-logs"
    with tempfile.TemporaryDirectory(dir=tmp_path) as temporary:
        dest = Path(temporary) / "run-0"
        with pytest.raises(subprocess.CalledProcessError) as error:
            run_diagnosis.container_run(
                "unused-image", unit, dest, ["simulate"], log_root=log_root
            )
        assert error.value.returncode == 17
        assert error.value.stderr == stderr
    assert not dest.exists()
    assert (log_root / "unit/run-0/stderr.log").read_bytes() == stderr
    assert (log_root / "unit/run-0/stdout.log").read_bytes() == stdout


def test_batch_destinations_are_host_owned_before_participant_launch(
    tmp_path, monkeypatch
):
    import os
    import stat
    from throughput import timer

    unit = tmp_path / "batch-unit"
    scenarios = unit / "scenarios"
    scenarios.mkdir(parents=True)
    (unit / "batch.json").write_text("{}")
    names = ("sub_00", "scenario.with.dots")
    for name in names:
        (scenarios / f"{name}.json").write_text("{}")
    written = []

    def participant(command, **_kwargs):
        output = next(Path(arg[:-8]) for arg in command if arg.endswith(":/output"))
        for name in names:
            directory = output / name
            # These permissions let UID 65534 create output files, while host
            # ownership lets the controller remove them even if files are 0644.
            assert directory.stat().st_uid == os.geteuid()
            assert stat.S_IMODE(directory.stat().st_mode) == 0o777
            file = directory / "trace.parquet"
            file.write_bytes(b"participant output")
            file.chmod(0o644)
            written.append(file)
        return subprocess.CompletedProcess(command, 0, stdout=b"done", stderr=b"")

    monkeypatch.setattr(timer, "bounded_container_run", participant)
    with tempfile.TemporaryDirectory(dir=tmp_path) as temporary:
        run_diagnosis.container_run(
            "unused-image",
            unit,
            Path(temporary) / "run-0",
            ["simulate-batch"],
            log_root=tmp_path / "logs",
        )
        assert [path.read_bytes() for path in written] == [b"participant output"] * 2
    assert all(not path.exists() for path in written)


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_controller_records_measurements_before_cleanup_and_requires_cleanup_success(
    tmp_path, monkeypatch, cleanup_fails
):
    from types import SimpleNamespace

    args = SimpleNamespace(
        unit=["t3-gbatch-dense-3"],
        out=tmp_path / "evidence",
        image="unused-image",
        repeats=1,
        stage_repeats=1,
    )
    monkeypatch.setattr(run_diagnosis.platform, "system", lambda: "Linux")
    monkeypatch.setattr(run_diagnosis.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        run_diagnosis.platform,
        "uname",
        lambda: ("Linux", "diagnostic-host", "test", "test", "x86_64", "x86_64"),
    )
    original_read = Path.read_text

    def read_text(path, *positional, **kwargs):
        if str(path) in {"/proc/cpuinfo", "/proc/meminfo"}:
            return "diagnostic host metadata"
        return original_read(path, *positional, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)

    def command_output(command, **_kwargs):
        if command[0] == "git":
            return "test-commit\n"
        return json.dumps(
            [{"Architecture": "amd64", "Os": "linux", "Id": "test-image"}]
        ).encode()

    monkeypatch.setattr(run_diagnosis.subprocess, "check_output", command_output)
    monkeypatch.setattr(run_diagnosis, "container_run", lambda *a, **k: (tmp_path, 1.0))
    monkeypatch.setattr(
        run_diagnosis,
        "output_metadata",
        lambda *_: {
            "parquet": {"trace.parquet": {"rows": 2}},
            "actual_events": 2,
            "events": {"wall_clock_sec": 0.5},
        },
    )
    original_temporary = tempfile.TemporaryDirectory

    class Scratch:
        def __init__(self, **kwargs):
            self.actual = original_temporary(dir=tmp_path, **kwargs)

        def __enter__(self):
            return self.actual.__enter__()

        def __exit__(self, *error):
            if error[0] is not None:
                return self.actual.__exit__(*error)
            before_cleanup = json.loads((args.out / "results.json").read_text())
            assert before_cleanup["measurements_complete"] is True
            assert before_cleanup["cleanup_complete"] is False
            assert before_cleanup["passed"] is False
            self.actual.__exit__(*error)
            if cleanup_fails:
                raise PermissionError("cleanup denied sentinel")

    monkeypatch.setattr(run_diagnosis.tempfile, "TemporaryDirectory", Scratch)
    if cleanup_fails:
        with pytest.raises(PermissionError, match="cleanup denied sentinel"):
            run_diagnosis.controller(args)
    else:
        run_diagnosis.controller(args)
    result = json.loads((args.out / "results.json").read_text())
    assert result["measurements_complete"] is True
    assert result["cleanup_complete"] is (not cleanup_fails)
    assert result["passed"] is (not cleanup_fails)
    assert result["units"]["t3-gbatch-dense-3"]["cold_cli"][1]["actual_events"] == 2
