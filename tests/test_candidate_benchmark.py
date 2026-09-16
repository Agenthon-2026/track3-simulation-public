"""Candidate selection must use complete paired host evidence, never the best run."""

import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import benchmark_candidates as bench  # noqa: E402


def plan():
    return {
        "images": {"A": "a", "B": "b"},
        "roster": [
            {"unit": "short", "family": "first"},
            {"unit": "long", "family": "second"},
        ],
        "analysis_seed": bench.ANALYSIS_SEED,
        "bootstrap_samples": bench.BOOTSTRAPS,
    }


def records(a=(100, 100, 100, 100, 100), b=(110, 110, 110, 110, 110)):
    return [
        {
            "arm": arm,
            "unit": unit,
            "group": i,
            "measurement": {
                "n_events": 1000,
                "reported_n_events": 1000,
                "host_wall_clock_sec": 1000 / rate,
                "events_per_sec": 1e99 if arm == "A" else 0,
            },
        }
        for arm, rates in (("A", a), ("B", b))
        for unit in ("short", "long")
        for i, rate in enumerate(rates)
    ]


def test_group_bootstrap_uses_host_clock_not_reported_rate():
    result = bench.paired_statistics(plan(), records())
    assert result["scores"] == pytest.approx({"A": 100, "B": 110})
    assert result["delta_score"] == pytest.approx(10)
    assert result["ci95"] == pytest.approx([10, 10])
    assert all(f["ci95"] == pytest.approx([10, 10]) for f in result["family"].values())


def test_one_best_run_and_cross_zero_do_not_establish_gain():
    result = bench.paired_statistics(plan(), records(b=(90, 95, 100, 105, 10000)))
    assert result["delta_score"] == 0
    assert result["ci95"][0] < 0 < result["ci95"][1]


def test_family_regression_cannot_hide_in_total():
    rows = records(b=(150,) * 5)
    for row in rows:
        if row["unit"] == "long" and row["arm"] == "B":
            row["measurement"]["host_wall_clock_sec"] = 1000 / 90
    result = bench.paired_statistics(plan(), rows)
    assert result["ci95"][0] > 0
    assert result["family"]["second"]["ci95"][1] < 0


def test_missing_unit_refuses_mean_over_subset():
    with pytest.raises(KeyError):
        bench.paired_statistics(plan(), records()[:-1])


def test_duplicate_record_refused():
    rows = records()
    with pytest.raises(ValueError, match="duplicate"):
        bench.paired_statistics(plan(), rows + [rows[0]])


@pytest.mark.parametrize(
    "field,value",
    [
        ("host_wall_clock_sec", float("nan")),
        ("host_wall_clock_sec", 0),
        ("reported_n_events", 1000000),
    ],
)
def test_invalid_host_measurement_refused(field, value):
    rows = records()
    rows[0]["measurement"][field] = value
    with pytest.raises(ValueError, match="host measurement"):
        bench.paired_statistics(plan(), rows)


def test_hash_index_detects_changed_evidence(tmp_path):
    path = tmp_path / "record.json"
    bench.save(path, {"rankable": False, "value": 1})
    entry = bench.index(path)
    assert bench.read_index(entry)["value"] == 1
    path.write_text('{"value":2}')
    with pytest.raises(ValueError, match="bytes changed"):
        bench.read_index(entry)


@pytest.mark.parametrize("unsafe", [False, True])
def test_failed_container_preserves_measurement_and_safe_evidence(
    tmp_path, monkeypatch, unsafe
):
    import subprocess
    from throughput import run_unit as runner

    unit = tmp_path / "unit"
    unit.mkdir()
    (unit / "scenario.json").write_text("{}")
    outside = tmp_path / "outside"
    outside.write_bytes(b"must not be copied")

    def failed_run(cmd, **kwargs):
        output = Path(next(s[:-8] for s in cmd if s.endswith(":/output")))
        (output / "trace.parquet").write_bytes(b"partial parquet bytes")
        if unsafe:
            (output / "events.json").symlink_to(outside)
        return (
            subprocess.CompletedProcess(cmd, 137, b"progress", b"killed"),
            12.5,
            None,
            123456,
        )

    monkeypatch.setattr(runner, "timed_container_run", failed_run)
    retained = tmp_path / "retained"
    logs = tmp_path / "logs"
    with pytest.raises(runner.UnitExecutionError, match="code 137") as caught:
        runner.run_once(
            "fixture", unit, batch=False, keep_output=retained, log_dir=logs
        )
    measured = caught.value.measurement
    assert (
        measured.returncode,
        measured.host_wall_clock_sec,
        measured.host_peak_memory_bytes,
    ) == (137, 12.5, 123456)
    assert (logs / "stdout.log").read_bytes() == b"progress"
    assert (logs / "stderr.log").read_bytes() == b"killed"
    if unsafe:
        assert not retained.exists()
        assert "refused" in (logs / "retention-error.log").read_text()
        assert outside.read_bytes() == b"must not be copied"
    else:
        assert (retained / "trace.parquet").read_bytes() == b"partial parquet bytes"


@pytest.mark.parametrize("purpose", ["baseline", "screen", "confirmation"])
def test_failed_plan_indexes_partial_output(tmp_path, monkeypatch, capsys, purpose):
    from throughput import run_unit as runner

    frozen = {
        "purpose": purpose,
        "created_at": "2026-09-15T00:00:00Z",
        "host": {},
        "repeats": 1,
        "roster": [{"unit": "fixture", "batch": False, "input_sha256": "input"}],
        "screen_units": ["fixture"],
        "images": {"A": "baseline", "B": "image"}
        if purpose == "confirmation"
        else {"A": "image"},
        "budget_sec": 60,
        "timeout_sec": 30,
        "plan_sha256": "fixture",
        "history_dir": str(tmp_path / "history"),
    }
    plan_path = tmp_path / "plan.json"
    bench.save(plan_path, frozen)
    monkeypatch.setattr(bench, "validate_plan", lambda *a, **kw: None)
    monkeypatch.setattr(bench, "host", lambda: {})

    def failed_run(*args, **kwargs):
        output = kwargs["keep_output"]
        output.mkdir()
        (output / "trace.parquet").write_bytes(b"retained partial output")
        (kwargs["log_dir"] / "stderr.log").write_bytes(b"killed")
        raise runner.UnitExecutionError(
            "exit 137", runner.UnitRun(0, 0, 0, 12.5, None, 123456, 137)
        )

    monkeypatch.setattr(runner, "run_once", failed_run)
    if purpose == "confirmation":
        bench.export_confirmation(plan_path, tmp_path / "early-reservation")
    evidence = bench.run_plan(plan_path, tmp_path / "run", diagnostic=True)
    if purpose == "confirmation":
        name = bench.digest(frozen["images"]["B"]) + ".json"
        assert (tmp_path / "early-reservation" / name).read_bytes() == (
            tmp_path / "history/confirmations" / name
        ).read_bytes()
    assert evidence["stop_reason"] == "unit-failed"
    assert len(evidence["runs"]) == 1  # No retry or next arm after a failed warmup.
    assert json.loads((tmp_path / "run/evidence.json").read_text()) == evidence
    raw = bench.read_index(evidence["runs"][0])
    assert raw["status"] == "failed" and raw["rankable"] is False
    assert raw["measurement"]["returncode"] == 137
    assert raw["resources"]["peak_memory_bytes"] == 123456
    assert raw["resources"]["output_bytes"] == len(b"retained partial output")
    assert raw["resources"]["disk_status"] == "missing"
    progress = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert [event["event"] for event in progress] == ["unit-start", "unit-finished"]
    assert progress[-1]["measurement"]["returncode"] == 137
    assert raw["artifacts"] == [bench.index(Path(raw["output_dir"]) / "trace.parquet")]
    assert raw["logs"][0]["sha256"] == bench.file_digest(Path(raw["logs"][0]["path"]))


@pytest.mark.parametrize("after_one_run", [False, True])
def test_host_timeout_retains_completed_records(tmp_path, monkeypatch, after_one_run):
    from throughput import run_unit as runner
    import subprocess

    frozen = {
        "purpose": "baseline",
        "host": {},
        "repeats": 1,
        "roster": [{"unit": "fixture", "batch": False, "input_sha256": "input"}],
        "images": {"A": "image"},
        "budget_sec": 60,
        "timeout_sec": 30,
        "plan_sha256": "fixture",
        "history_dir": str(tmp_path / "history"),
    }
    plan_path = tmp_path / "plan.json"
    bench.save(plan_path, frozen)
    monkeypatch.setattr(bench, "validate_plan", lambda *a, **kw: None)
    calls = []

    def failing_host():
        if after_one_run and not calls:
            return {}
        raise subprocess.TimeoutExpired(["docker", "info"], 30)

    def successful_run(*args, **kwargs):
        calls.append(args)
        kwargs["keep_output"].mkdir()
        (kwargs["log_dir"] / "stdout.log").write_bytes(b"completed")
        return runner.UnitRun(10, 10, 10, 1, None, 123456, 0)

    monkeypatch.setattr(bench, "host", failing_host)
    monkeypatch.setattr(runner, "run_once", successful_run)
    monkeypatch.setattr(bench, "gate_output", lambda *a: {"admissible": True})
    evidence = bench.run_plan(plan_path, tmp_path / "run", diagnostic=True)
    assert evidence["stop_reason"] == "host-unavailable"
    assert evidence["stop_error"]["kind"] == "TimeoutExpired"
    assert len(calls) == len(evidence["runs"]) == int(after_one_run)
    assert json.loads((tmp_path / "run/evidence.json").read_text()) == evidence
    if after_one_run:
        raw = bench.read_index(evidence["runs"][0])
        assert raw["status"] == "passed"
        assert raw["logs"] == [
            bench.index(tmp_path / "run/warmup/fixture/A/stdout.log")
        ]


def decision(g2="pass", g3="pass", purpose="confirmation"):
    return {
        "G2": g2,
        "G3": g3,
        "purpose": purpose,
        "images": {"A": "a", "B": "b"} if purpose != "baseline" else {"A": "a"},
        "reasons": [],
        "evidence": {"path": "fixture", "sha256": "fixture"},
        "rankable": False,
        "execution_platform": "linux/amd64",
        "caps": bench.CAPS,
    }


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(bench.preparation, "validate_delivery", lambda *a: None)
    archive = tmp_path / "archive"
    archive.write_bytes(b"fixture archive")
    g1s = []
    for image in ("a", "b"):
        p = tmp_path / (image + ".json")
        bench.save(
            p,
            {
                "image": image,
                "scope": "G1",
                "status": "passed",
                "rankable": False,
                "archive": str(archive),
                "archive_sha256": bench.file_digest(archive),
                "delivery": {},
            },
        )
        g1s.append(p)
    monkeypatch.setattr(
        bench, "assess", lambda _: decision(purpose="baseline", g3="missing")
    )
    directory = tmp_path / "history"
    baseline = bench.decide(directory, tmp_path / "evidence", g1s, None)
    assert baseline["stable"] == "a" and baseline["action"] == "promote"
    return directory, g1s


@pytest.mark.parametrize(
    "g2,g3",
    [("missing", "pass"), ("fail", "pass"), ("pass", "inconclusive"), ("pass", "fail")],
)
def test_incomplete_failed_or_uncertain_candidate_keeps_baseline(
    state, monkeypatch, tmp_path, g2, g3
):
    directory, g1s = state
    monkeypatch.setattr(bench, "assess", lambda _: decision(g2, g3))
    event = bench.decide(directory, tmp_path / "evidence", g1s, "a")
    assert event["stable"] == "a" and event["action"] != "promote"


def test_only_all_gates_promote_and_baseline_change_refuses(
    state, monkeypatch, tmp_path
):
    directory, g1s = state
    monkeypatch.setattr(bench, "assess", lambda _: decision())
    with pytest.raises(ValueError, match="pointer changed"):
        bench.decide(directory, tmp_path / "evidence", g1s, "wrong")
    promoted = bench.decide(directory, tmp_path / "evidence", g1s, "a")
    assert promoted["stable"] == "b" and promoted["previous_stable"] == "a"
    assert len(bench.history(directory)) == 2
    with pytest.raises(ValueError, match="already used"):
        bench.decide(directory, tmp_path / "evidence", g1s, "b")


def test_missing_g1_prevents_promotion(state, monkeypatch, tmp_path):
    directory, _ = state
    monkeypatch.setattr(bench, "assess", lambda _: decision())
    event = bench.decide(directory, tmp_path / "evidence", [], "a")
    assert event["stable"] == "a" and event["decision"]["G1"] == "missing"


def test_history_tampering_refused(state):
    directory, _ = state
    path = directory / "000000.json"
    event = json.loads(path.read_text())
    event["stable"] = "unverified"
    path.write_text(json.dumps(event))
    with pytest.raises(ValueError, match="history modified"):
        bench.history(directory)


@pytest.fixture
def full_evidence(tmp_path, monkeypatch):
    import pyarrow as pa
    import pyarrow.parquet as pq
    import validate_candidate_parameters as parameters

    units = [
        {
            "unit": f"unit-{i:02d}",
            "family": f"family-{i % 7}",
            "batch": False,
            "input_sha256": f"input-{i}",
        }
        for i in range(72)
    ]
    identities = {
        "code_sha256": "code",
        "toolkit_version": "test",
        "toolkit_source": "test",
        "scorer_sha256": "scorer",
    }
    node = {"native": True, "node": "dedicated-test"}
    monkeypatch.setattr(bench, "identity", lambda: identities)
    monkeypatch.setattr(bench, "roster", lambda: units)
    monkeypatch.setattr(bench, "host", lambda: node)
    monkeypatch.setattr(
        bench.preparation,
        "repeat_artifacts",
        lambda _: {"trace.parquet", "message_trace.parquet"},
    )
    monkeypatch.setattr(parameters, "validate", lambda *args: None)
    heldout = tmp_path / "heldout.json"
    bench.save(heldout, {"identity": identities})
    heldout_map = tmp_path / "holdouts.json"
    bench.save(heldout_map, {"a": bench.index(heldout), "b": bench.index(heldout)})
    frozen = {
        "rankable": False,
        "profile": "developer",
        "purpose": "confirmation",
        "images": {"A": "a", "B": "b"},
        "roster": units,
        "identity": identities,
        "host": node,
        "caps": bench.CAPS,
        "repeats": bench.REPEATS,
        "warmups": 1,
        "analysis_seed": bench.ANALYSIS_SEED,
        "bootstrap_samples": bench.BOOTSTRAPS,
        "holdout": bench.index(heldout_map),
    }
    frozen["plan_sha256"] = bench.digest(frozen)
    plan_path = tmp_path / "plan.json"
    bench.save(plan_path, frozen)
    out = tmp_path / "output"
    out.mkdir()
    pq.write_table(pa.table({"t_ns": list(range(1000))}), out / "trace.parquet")
    pq.write_table(
        pa.table({"send_time_ns": list(range(1000))}), out / "message_trace.parquet"
    )
    bench.save(out / "events.json", {"n_events": 1000})
    log = tmp_path / "stdout.log"
    log.write_text("bounded container log\n")
    artifacts = [bench.index(p) for p in sorted(out.iterdir())]
    rows = []
    for u in units:
        for arm in ("A", "B"):
            for group in range(-1, bench.REPEATS):
                raw = {
                    "unit": u["unit"],
                    "arm": arm,
                    "group": group,
                    "image": frozen["images"][arm],
                    "host": node,
                    "plan_sha256": frozen["plan_sha256"],
                    "input_sha256": u["input_sha256"],
                    "rankable": False,
                    "status": "passed",
                    "verification": {
                        "admissible": True,
                        "gates": {f"g{i}": {"passed": True} for i in range(4)},
                    },
                    "measurement": {
                        "n_events": 1000,
                        "reported_n_events": 1000,
                        "host_wall_clock_sec": 10 if arm == "A" else 9,
                        "events_per_sec": 1e99,
                    },
                    "resources": {
                        "peak_memory_bytes": 10000000,
                        "peak_disk_bytes": 200000,
                        "disk_capacity_bytes": 10 * 1024**3,
                        "disk_status": "bounded",
                    },
                    "output_dir": str(out),
                    "parquet_sha256": bench.preparation.output_hashes(out),
                    "artifacts": artifacts,
                    "logs": [bench.index(log)],
                }
                path = tmp_path / "raw" / f"{u['unit']}-{arm}-{group}.json"
                bench.save(path, raw)
                rows.append(bench.index(path))
    evidence_path = tmp_path / "evidence.json"
    bench.save(
        evidence_path,
        {"plan": bench.index(plan_path), "runs": rows, "diagnostic": False},
    )
    return evidence_path, frozen


def test_full_roster_assessment_derives_positive_gain(full_evidence):
    result = bench.assess(full_evidence[0])
    assert result["G2"] == result["G3"] == "pass"
    assert len(result["statistics"]["units"]) == 72
    assert result["exemplar_semantics"] == "unknown" and result["rankable"] is False


def test_screen_selects_short_long_all_batches_and_exemplar(tmp_path, monkeypatch):
    units = []
    for family in ("first", "second"):
        for i, horizon in enumerate((20, 10, 30)):
            name = f"{family}-{i}"
            units.append({"unit": name, "family": family, "batch": False})
            bench.save(
                tmp_path / "units" / name / "scenario.json", {"horizon_ns": horizon}
            )
    units.extend(
        [
            {"unit": "batch", "family": "first", "batch": True},
            {"unit": bench.EXEMPLAR, "family": "second", "batch": False},
        ]
    )
    bench.save(
        tmp_path / "units" / bench.EXEMPLAR / "scenario.json", {"horizon_ns": 20}
    )
    monkeypatch.setattr(bench, "ROOT", tmp_path)
    assert set(bench.select_screen_units(units)) == {
        "first-1",
        "first-2",
        "second-1",
        "second-2",
        "batch",
        bench.EXEMPLAR,
    }


def test_screen_evidence_never_yields_qualification_or_partial_score(full_evidence):
    evidence_path, frozen = full_evidence
    frozen.update(
        purpose="screen",
        repeats=1,
        screen_units=[u["unit"] for u in frozen["roster"][:7]],
        holdout=None,
    )
    frozen["plan_sha256"] = bench.digest(
        {k: v for k, v in frozen.items() if k != "plan_sha256"}
    )
    evidence = json.loads(evidence_path.read_text())
    plan_path = Path(evidence["plan"]["path"])
    plan_path.write_text(json.dumps(frozen))
    evidence["plan"] = bench.index(plan_path)
    entries = []
    for entry in evidence["runs"]:
        row = bench.read_index(entry)
        if row["unit"] in frozen["screen_units"] and row["group"] < 1:
            row["plan_sha256"] = frozen["plan_sha256"]
            path = Path(entry["path"])
            path.write_text(json.dumps(row))
            entries.append(bench.index(path))
    evidence["runs"] = entries
    evidence_path.write_text(json.dumps(evidence))
    result = bench.assess(evidence_path)
    assert result["screen_status"] == "pass"
    assert result["executed_units"] == 7
    assert result["statistics"] is None
    assert result["G2"] == result["G3"] == "missing"
    assert result["rankable"] is False
    evidence["runs"] = entries[:-1]
    evidence_path.write_text(json.dumps(evidence))
    incomplete = bench.assess(evidence_path)
    assert incomplete["screen_status"] != "pass"
    assert incomplete["G2"] == incomplete["G3"] == "missing"
    assert incomplete["statistics"] is None
    frozen["screen_units"] = frozen["screen_units"][:-1]
    frozen["plan_sha256"] = bench.digest(
        {k: v for k, v in frozen.items() if k != "plan_sha256"}
    )
    with pytest.raises(ValueError, match="every family"):
        bench.validate_plan(frozen)


@pytest.mark.parametrize(
    "fault",
    [
        "missing-unit",
        "semantic-failure",
        "host-drift",
        "missing-memory",
        "missing-disk",
        "self-report",
        "missing-artifacts",
    ],
)
def test_full_roster_faults_block_promotion(full_evidence, fault):
    evidence_path, _ = full_evidence
    e = json.loads(evidence_path.read_text())
    if fault == "missing-unit":
        e["runs"] = e["runs"][:-1]
    else:
        entry = e["runs"][0]
        path = Path(entry["path"])
        raw = json.loads(path.read_text())
        if fault == "semantic-failure":
            raw["verification"]["admissible"] = False
        elif fault == "host-drift":
            raw["host"] = {"node": "another-machine"}
        elif fault == "missing-memory":
            raw["resources"]["peak_memory_bytes"] = None
        elif fault == "missing-disk":
            raw["resources"]["peak_disk_bytes"] = None
        elif fault == "missing-artifacts":
            raw["artifacts"] = []
        elif fault == "self-report":
            raw["measurement"]["n_events"] = 10**12
            raw["measurement"]["reported_n_events"] = 10**12
        path.write_text(json.dumps(raw))
        e["runs"][0] = bench.index(path)
    evidence_path.write_text(json.dumps(e))
    result = bench.assess(evidence_path)
    assert result["G2"] != "pass" and result["G3"] != "pass"


def test_stale_code_rejects_complete_evidence(full_evidence, monkeypatch):
    monkeypatch.setattr(bench, "identity", lambda: {"code_sha256": "changed"})
    with pytest.raises(ValueError, match="stale code"):
        bench.assess(full_evidence[0])


@pytest.mark.parametrize("required", [False, True])
def test_full_roster_allows_only_policy_optional_missing_ledgers(
    full_evidence, monkeypatch, required
):
    evidence_path, _ = full_evidence
    evidence = json.loads(evidence_path.read_text())
    monkeypatch.setattr(
        bench.preparation,
        "repeat_artifacts",
        lambda _: {"trace.parquet", "message_trace.parquet"}
        if required
        else {"trace.parquet"},
    )
    for entry in evidence["runs"]:
        raw = bench.read_index(entry)
        ledger = Path(raw["output_dir"]) / "message_trace.parquet"
        ledger.unlink(missing_ok=True)
        raw["parquet_sha256"].pop("message_trace.parquet")
        raw["artifacts"] = [
            a
            for a in raw["artifacts"]
            if Path(a["path"]).name != "message_trace.parquet"
        ]
        path = Path(entry["path"])
        path.write_text(json.dumps(raw))
        entry["sha256"] = bench.file_digest(path)
    evidence_path.write_text(json.dumps(evidence))
    result = bench.assess(evidence_path)
    assert (result["G2"] == "pass") is (not required)


def test_rollback_requires_qualified_prior_candidate(state, monkeypatch, tmp_path):
    directory, g1s = state
    monkeypatch.setattr(bench, "assess", lambda _: decision())
    bench.decide(directory, tmp_path / "evidence", g1s, "a")
    real_read = bench.read_index
    monkeypatch.setattr(
        bench, "read_index", lambda e: {} if e["path"] == "fixture" else real_read(e)
    )
    result = bench.rollback(directory, "a", "observed regression", "b")
    assert result["stable"] == "a" and result["previous_stable"] == "b"
    assert [e["action"] for e in bench.history(directory)] == [
        "promote",
        "promote",
        "rollback",
    ]
    with pytest.raises(ValueError, match="current"):
        bench.rollback(directory, "a", "again", "a")


def test_problem_count_survives_rounds_but_resets_after_fix(tmp_path):
    assert bench.record_problem(tmp_path, "unit", "semantic") == 1
    assert bench.record_problem(tmp_path, "unrelated", None) == 0
    assert bench.record_problem(tmp_path, "unit", "semantic") == 2
    assert bench.record_problem(tmp_path, "unit", "semantic") == 3
    assert bench.record_problem(tmp_path, "unit", None) == 0
    assert bench.record_problem(tmp_path, "unit", "semantic") == 1


@pytest.mark.parametrize("current_g2", ["pass", "fail"])
def test_rollback_uses_baseline_requalified_in_latest_pair(
    state, monkeypatch, tmp_path, current_g2
):
    directory, g1s = state
    paired = decision()
    paired["evidence"] = {"path": "fresh-pair", "sha256": "fixture"}
    monkeypatch.setattr(bench, "assess", lambda _: paired.copy())
    bench.decide(directory, tmp_path / "evidence", g1s, "a")
    real_read = bench.read_index
    monkeypatch.setattr(
        bench, "read_index", lambda e: {} if e["path"] == "fresh-pair" else real_read(e)
    )

    def current_assessment(path):
        if str(path) == "fixture":
            raise ValueError("stale code/toolkit/scorer evidence")
        assert str(path) == "fresh-pair"
        return decision(g2=current_g2)

    monkeypatch.setattr(bench, "assess", current_assessment)
    if current_g2 == "fail":
        with pytest.raises(ValueError, match="no longer passes"):
            bench.rollback(directory, "a", "regression", "b")
        assert bench.history(directory)[-1]["stable"] == "b"
    else:
        result = bench.rollback(directory, "a", "regression", "b")
        assert result["stable"] == "a"
        assert result["qualification"]["path"] == "fresh-pair"


@pytest.mark.parametrize("invalid", [False, True])
def test_restore_native_history_preserves_unresolved_count(
    tmp_path, monkeypatch, invalid
):
    import io
    import zipfile
    import restore_candidate_history as restore

    source = tmp_path / "source"
    bench.record_problem(source, "exemplar", "UnitExecutionError")
    bench.record_problem(source, "other", None)
    bench.record_problem(source, "exemplar", "UnitExecutionError")
    payload = (source / "problems.jsonl").read_bytes()
    if invalid:
        payload = payload.replace(b'"count": 2', b'"count": 1')
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("history/problems.jsonl", payload)
        z.writestr("../outside", b"must not extract")

    def api(endpoint):
        if endpoint.endswith("/zip"):
            return archive.getvalue()
        if endpoint.split("?")[0].endswith("/artifacts"):
            return json.dumps(
                {
                    "artifacts": [
                        {"id": 20, "name": "native-qualification-2", "expired": False}
                    ]
                }
            ).encode()
        return json.dumps({"workflow_runs": [{"id": 3}, {"id": 2}]}).encode()

    monkeypatch.setattr(restore, "api", api)
    target = tmp_path / "restored"
    if invalid:
        with pytest.raises(ValueError, match="count disagrees"):
            restore.restore("owner/repo", "branch", 3, target)
        assert not target.exists()
    else:
        result = restore.restore("owner/repo", "branch", 3, target)
        assert result["source_run"] == 2
        assert (target / "problems.jsonl").read_bytes() == payload
        assert bench.record_problem(target, "exemplar", "UnitExecutionError") == 3
    assert not (tmp_path / "outside").exists()


@pytest.mark.parametrize("expired", [False, True])
def test_interrupted_confirmation_is_restored_before_retry(
    tmp_path, monkeypatch, expired
):
    import io
    import zipfile
    import restore_candidate_history as restore

    image = "registry/candidate@sha256:" + "a" * 64
    plan = {
        "purpose": "confirmation",
        "images": {"A": "registry/base@sha256:" + "b" * 64, "B": image},
        "created_at": "2026-09-15T00:00:00Z",
        "plan_sha256": "c" * 64,
        "history_dir": str(tmp_path / "restored"),
    }
    plan_path = tmp_path / "plan.json"
    bench.save(plan_path, plan)
    monkeypatch.setattr(bench, "validate_plan", lambda *a, **kw: None)
    early = tmp_path / "early"
    reservation = bench.export_confirmation(plan_path, early)
    name = bench.digest(image) + ".json"
    payload = (early / name).read_bytes()
    assert json.loads(payload) == reservation
    history_source = tmp_path / "history-source"
    bench.record_problem(history_source, "exemplar", "UnitExecutionError")

    def archive(files):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as z:
            for path, content in files.items():
                z.writestr(path, content)
        return data.getvalue()

    archives = {
        20: archive({name: payload}),  # Run 2 stopped before final/history upload.
        10: archive(
            {"problems.jsonl": (history_source / "problems.jsonl").read_bytes()}
        ),
    }

    def api(endpoint):
        route = endpoint.split("?")[0]
        if route.endswith("/zip"):
            return archives[int(route.split("/")[-2])]
        if route.endswith("/artifacts"):
            run = int(route.split("/")[-2])
            artifacts = (
                [{"id": 20, "name": "native-reservations-2", "expired": expired}]
                if run == 2
                else [{"id": 10, "name": "native-history-1", "expired": False}]
            )
            return json.dumps({"artifacts": artifacts}).encode()
        return json.dumps({"workflow_runs": [{"id": 3}, {"id": 2}, {"id": 1}]}).encode()

    monkeypatch.setattr(restore, "api", api)
    target = tmp_path / "restored"
    if expired:
        with pytest.raises(ValueError, match="reservations expired"):
            restore.restore("owner/repo", "branch", 3, target)
        assert not target.exists()
        return
    result = restore.restore("owner/repo", "branch", 3, target)
    assert (target / "confirmations" / name).read_bytes() == payload
    assert result["source_run"] == 1
    assert result["reservation_sources"][0]["source_run"] == 2
    assert bench.record_problem(target, "exemplar", "UnitExecutionError") == 2
    with pytest.raises(ValueError, match="already reserved"):
        bench.export_confirmation(plan_path, tmp_path / "retry-export")
    assert not (tmp_path / "retry-export").exists()
    # Calling run directly cannot bypass the restored opportunity guard.
    with pytest.raises(FileExistsError):
        bench.run_plan(plan_path, tmp_path / "retry-run", diagnostic=True)


def test_native_history_pagination_preserves_older_reservations(monkeypatch):
    import restore_candidate_history as restore

    endpoints = []

    def api(endpoint):
        endpoints.append(endpoint)
        return json.dumps(
            {
                "artifacts": [{"id": i} for i in range(100)]
                if endpoint.endswith("page=1")
                else [{"id": 100}]
            }
        ).encode()

    monkeypatch.setattr(restore, "api", api)
    assert restore.pages("repos/owner/repo/artifacts", "artifacts")[-1] == {"id": 100}
    assert len(endpoints) == 2


@pytest.mark.parametrize("member", ["../outside.json", "0" * 64 + ".json"])
def test_confirmation_reservation_rejects_wrong_path_or_identity(member):
    import io
    import zipfile
    import restore_candidate_history as restore

    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        z.writestr(member, json.dumps({"image": "registry/image@sha256:" + "a" * 64}))
    with zipfile.ZipFile(io.BytesIO(data.getvalue())) as z:
        with pytest.raises(ValueError, match="invalid confirmation"):
            restore.read_reservations(z, "")


def test_kernel_reserved_host_memory_is_not_a_container_limit_failure():
    bench.require_native_host(
        {"native": True, "docker_cpus": 4, "docker_memory": 16766418944}
    )
    with pytest.raises(ValueError, match="native linux"):
        bench.require_native_host(
            {"native": False, "docker_cpus": 4, "docker_memory": 32 * 1024**3}
        )


def test_promotion_refuses_mixed_platform_history(state, monkeypatch, tmp_path):
    directory, g1s = state
    local = decision()
    local.update(
        execution_platform="linux/arm64",
        caps={**bench.CAPS, "disk_bytes": 64 * 1024**3},
    )
    monkeypatch.setattr(bench, "assess", lambda _: local)
    with pytest.raises(ValueError, match="another platform/resource policy"):
        bench.decide(directory, tmp_path / "evidence", g1s, "a")


@pytest.mark.parametrize("decoded_rows", [1000, 999])
def test_full_assessment_preserves_exemplar_unknown(
    full_evidence, monkeypatch, decoded_rows
):
    evidence_path, frozen = full_evidence
    # Treat one fixture unit as the exemplar without weakening checks for the rest.
    monkeypatch.setattr(bench, "EXEMPLAR", "unit-00")
    evidence = json.loads(evidence_path.read_text())
    for entry in evidence["runs"]:
        raw = bench.read_index(entry)
        if raw["unit"] != "unit-00":
            continue
        raw["verification"] = {
            "admissible": True,
            "semantic_status": "unknown",
            "local_trace_checks": {"passed": True, "decoded_rows": decoded_rows},
            "gates": {
                "g0_integrity": {"passed": True},
                "g1_schema": {"passed": True},
                "g2_cutoff_resource": {"passed": True},
                "g3_domain_semantics": {"passed": None},
            },
        }
        path = Path(entry["path"])
        path.write_text(json.dumps(raw))
        entry["sha256"] = bench.file_digest(path)
    evidence_path.write_text(json.dumps(evidence))
    result = bench.assess(evidence_path)
    assert result["G2"] == ("pass" if decoded_rows == 1000 else "missing")
    assert result["exemplar_semantics"] == "unknown"
