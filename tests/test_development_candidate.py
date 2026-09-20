"""Reject misleading candidate evidence using real output files and bounded runs."""

from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from scripts import validate_development_candidate as validator


def make_unit(root):
    unit = root / "unit"
    unit.mkdir()
    (unit / "scenario.json").write_text('{"seed":17}')
    (unit / "card.toml").write_text(
        '[task]\nscenario_family="throughput-scale"\n'
        "[scoring.params]\nrequires_message_ledger=false\n"
    )
    pq.write_table(pa.table({"seq": list(range(5))}), unit / "message_trace.parquet")
    return unit


def make_output(root, *, messages=5, ledger=False, seconds=1.0):
    root.mkdir(parents=True)
    trace = root / "trace.parquet"
    pq.write_table(pa.table({"t_ns": [101, 102]}), trace)
    events = {
        "n_events": 2,
        "n_messages": messages,
        "wall_clock_sec": seconds,
        "events_per_sec": 2 / seconds,
        "trace_sha256": validator.sha(trace),
    }
    if ledger:
        message = root / "message_trace.parquet"
        pq.write_table(pa.table({"seq": list(range(5))}), message)
        events["message_trace_sha256"] = validator.sha(message)
    (root / "events.json").write_text(json.dumps(events))
    return root


def test_optional_count_is_bound_to_actual_public_ledger(tmp_path):
    unit = make_unit(tmp_path)
    output = make_output(tmp_path / "good")
    assert validator.measured_output(unit, output, 2)["counts"] == {
        "single": {"n_events": 2, "n_messages": 5}
    }
    bad = make_output(tmp_path / "bad", messages=6)
    with pytest.raises(ValueError, match="actual public reference ledger"):
        validator.measured_output(unit, bad, 2)


def test_emitted_count_and_rate_must_match_measured_artifact(tmp_path):
    unit = make_unit(tmp_path)
    output = make_output(tmp_path / "output", ledger=True)
    events_path = output / "events.json"
    events = json.loads(events_path.read_text())
    events["n_messages"] = 4
    events_path.write_text(json.dumps(events))
    with pytest.raises(ValueError, match="Footer count"):
        validator.measured_output(unit, output, 2)
    events["n_messages"] = 5
    events["events_per_sec"] = 2000
    events_path.write_text(json.dumps(events))
    with pytest.raises(ValueError, match="Reported rate"):
        validator.measured_output(unit, output, 2)


def test_inventory_rejects_links_and_missing_required_output(tmp_path):
    unit = make_unit(tmp_path)
    output = make_output(tmp_path / "output")
    trace = output / "trace.parquet"
    trace.unlink()
    with pytest.raises(ValueError, match="Missing output"):
        validator.inventory(unit, output)
    trace.symlink_to(unit / "message_trace.parquet")
    with pytest.raises(ValueError, match="Disallowed output"):
        validator.inventory(unit, output)


def test_repeat_identity_checks_own_arm_bytes_and_counts():
    original = {
        "parquet": {"trace.parquet": {"sha256": "a", "rows": 3}},
        "counts": {"single": {"n_events": 3, "n_messages": 7}},
    }
    validator.identity_matches(deepcopy(original), original)
    for key in ("parquet", "counts"):
        altered = deepcopy(original)
        altered[key] = {}
        with pytest.raises(ValueError, match="changed across repetitions"):
            validator.identity_matches(altered, original)


def test_completion_requires_exact_cartesian_roster():
    records = [
        {"unit": "one", "arm": arm, "group": group, "passed": True}
        for arm in ("baseline", "candidate")
        for group in (-1, 0, 1, 2)
    ]
    assert validator.complete(records, ["one"], 3)
    assert not validator.complete(records[:-1], ["one"], 3)
    assert not validator.complete(records + records[:1], ["one"], 3)
    records[-1]["passed"] = False
    assert not validator.complete(records, ["one"], 3)


@pytest.mark.parametrize("failure", [None, "participant", "cleanup"])
def test_controller_gates_once_alternates_arms_and_retains_failure(
    tmp_path, monkeypatch, failure
):
    unit = make_unit(tmp_path)
    monkeypatch.setattr(validator, "roster", lambda _: [unit])
    monkeypatch.setattr(validator.platform, "system", lambda: "Linux")
    monkeypatch.setattr(validator.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        validator.platform,
        "uname",
        lambda: ("Linux", "host", "test", "test", "x86_64", "x86_64"),
    )
    old_read = Path.read_text

    def read(path, *a, **kw):
        return "test cpu" if str(path) == "/proc/cpuinfo" else old_read(path, *a, **kw)

    monkeypatch.setattr(Path, "read_text", read)

    def check_output(command, **kwargs):
        if command[0] == "git":
            return "source-commit\n"
        return json.dumps(
            [{"Os": "linux", "Architecture": "amd64", "Id": command[-1]}]
        ).encode()

    monkeypatch.setattr(validator.subprocess, "check_output", check_output)
    calls = []

    def launch(image, unit, dest, verb, **kwargs):
        calls.append((image, dest.name))
        if failure == "participant" and dest.name == "pair-0-candidate":
            raise subprocess.CalledProcessError(1, "test-container")
        return make_output(
            dest / "output", seconds=1.0 if image == "baseline" else 0.5
        ), 2.0 if image == "baseline" else 1.0

    monkeypatch.setattr(validator.runtime, "container_run", launch)
    import verify_candidate_output

    gate_calls = []
    monkeypatch.setattr(
        verify_candidate_output,
        "bounded_verify",
        lambda *a, **kw: gate_calls.append(a) or {"admissible": True},
    )
    comparisons = []
    monkeypatch.setattr(
        validator, "exact", lambda *a, **kw: comparisons.append(a) or {"passed": True}
    )
    original_temp = tempfile.TemporaryDirectory
    if failure == "cleanup":

        @contextmanager
        def cleanup_failure(**kwargs):
            with original_temp(**kwargs) as directory:
                yield directory
            raise PermissionError("cleanup sentinel")

        monkeypatch.setattr(validator.tempfile, "TemporaryDirectory", cleanup_failure)
    args = SimpleNamespace(
        candidate="candidate",
        baseline="baseline",
        out=tmp_path / "evidence",
        unit=[],
        repeats=3,
    )
    if failure:
        with pytest.raises((PermissionError, subprocess.CalledProcessError)):
            validator.controller(args)
    else:
        validator.controller(args)
    summary = json.loads((args.out / "summary.json").read_text())
    assert len(gate_calls) == 1 and len(comparisons) == 2
    if failure:
        assert summary["passed"] is False
        assert summary["cleanup_complete"] is False
        assert summary["error"]["type"] in {"PermissionError", "CalledProcessError"}
    else:
        assert summary["passed"] is True and summary["cleanup_complete"] is True
        assert [arm for arm, label in calls] == [
            "baseline",
            "candidate",
            "baseline",
            "candidate",
            "candidate",
            "baseline",
            "baseline",
            "candidate",
        ]
        assert summary["summary"]["host"]["geometric_mean_paired_median_speedup"] == 2
        assert len(summary["records"]) == 8


def test_real_batch_schema_uses_sub_message_counts_without_total_messages(tmp_path):
    unit = make_unit(tmp_path)
    subs = [{"sub": "sub_00"}, {"sub": "sub_01"}]
    (unit / "batch.json").write_text(json.dumps({"subs": subs}))
    output = tmp_path / "batch-output"
    for sub in subs:
        prefix = sub["sub"]
        reference = unit / "checks/reference_data" / prefix
        reference.mkdir(parents=True)
        pq.write_table(
            pa.table({"seq": list(range(5))}), reference / "message_trace.parquet"
        )
        make_output(output / prefix, ledger=True)
    (output / "batch_events.json").write_text(
        json.dumps(
            {
                "n_scenarios": 2,
                "total_events": 4,
                "wall_clock_sec": 1.0,
                "events_per_sec": 4.0,
                "per_scenario": [
                    {
                        "sub": s["sub"],
                        "n_events": 2,
                        "trace_sha256": validator.sha(
                            output / s["sub"] / "trace.parquet"
                        ),
                    }
                    for s in subs
                ],
            }
        )
    )
    measured = validator.measured_output(unit, output, 2)
    assert measured["actual_events"] == 4
    assert measured["counts"] == {
        s["sub"]: {"n_events": 2, "n_messages": 5} for s in subs
    }
