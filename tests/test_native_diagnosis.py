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
