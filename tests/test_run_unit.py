"""Tests for the LOCAL DEVELOPER unit runner and the node fingerprint.

Docker-free and hermetic: these cover routing, parsing, staging, comparability, the bounds on the
container invocation, and the non-rankability stamp. The end-to-end container behaviour lives in
``tests/integration/`` behind an explicit marker.

Two things are asserted here that used to be asserted the other way round:

* ``_reclaim_output`` is GONE. It re-invoked the PARTICIPANT'S image after the timed window to
  chown the output tree, and the old tests pinned its argv. The replacement is no re-invocation at
  all: cleanup is host-side and best-effort, with ``--run-as-host-user`` as the opt-in that makes
  it clean. Running a participant image for a housekeeping task is a production dependency on
  executing untrusted code for organizer convenience, and it does not belong in either path.
  (Those tests were also the only non-hermetic ones in the suite: one of them called the un-mocked
  helper, which shelled out to a real ``docker run`` and, on a networked runner, attempted a
  registry pull.)
* the record says ``rankable = False``. The old record called itself "authoritative".

    python tests/test_run_unit.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from throughput.awards import (  # noqa: E402
    best_gpu_acceleration,
    best_systems_diagnosis,
    entry_from_diagnostics,
)
from throughput.diagnostics import compute as compute_diagnostics  # noqa: E402
from throughput.node_fingerprint import NodeFingerprint, collect  # noqa: E402
from throughput.report import build_report  # noqa: E402
from throughput import run_unit as ru  # noqa: E402
from throughput.run_unit import (  # noqa: E402
    UnitRecord,
    UnitRun,
    _host_n_events,
    _reported_n_events,
    _stage_input,
    is_batch_unit,
    merge_host_metrics,
)


def _fp(**over: object) -> NodeFingerprint:
    base = dict(
        cpu_model="Xeon",
        cpu_count=16,
        memory_bytes=1 << 36,
        kernel="6.8.0",
        platform="Linux",
        docker_version="27.0",
        gpu_name="L40S",
        gpu_count=1,
        nvidia_driver="550.54",
        cuda_version="12.8",
    )
    base.update(over)
    return NodeFingerprint(**base)  # type: ignore[arg-type]


def test_fingerprint_collect_never_raises() -> None:
    fp = collect()
    assert isinstance(fp.to_dict(), dict)
    # cpu_count is the one field every host can answer.
    assert fp.cpu_count is None or fp.cpu_count > 0


def test_fingerprint_comparability() -> None:
    a = _fp()
    assert a.comparable_to(_fp()) is True
    # Kernel / docker version churn does NOT break comparability: same SKU, same timings.
    assert a.comparable_to(_fp(kernel="6.9.1", docker_version="27.1")) is True
    # A hardware change does, and is reported.
    moved = _fp(gpu_name="A100")
    assert a.comparable_to(moved) is False
    assert a.differences(moved) == {"gpu_name": ("L40S", "A100")}
    assert a.differences(_fp(cpu_count=8))["cpu_count"] == (16, 8)


def test_is_batch_unit() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        single = root / "single"
        (single).mkdir()
        (single / "scenario.json").write_text("{}")
        assert is_batch_unit(single) is False

        batch = root / "batch"
        (batch / "scenarios").mkdir(parents=True)
        (batch / "batch.json").write_text("{}")
        assert is_batch_unit(batch) is True


def test_stage_input_never_copies_reference_answers() -> None:
    # The runner mounts what it stages. Reference traces must never reach the submission.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        unit = root / "unit"
        unit.mkdir()
        (unit / "scenario.json").write_text("{}")
        (unit / "trace.parquet").write_text("ANSWER")
        (unit / "message_trace.parquet").write_text("ANSWER")
        (unit / "events.json").write_text("{}")
        staging = root / "staging"
        staging.mkdir()

        _stage_input(unit, staging, batch=False)
        staged = {p.name for p in staging.rglob("*")}
        assert staged == {"scenario.json"}, staged

        # Batch: only the scenarios tree, never the per-sub reference material. That material
        # lives at checks/reference_data/<sub>/ (the toolkit's documented self-grading location).
        # The legacy references/<sub>/ spelling is planted alongside it on purpose: staging is an
        # allowlist, so neither spelling may ever be copied, and a future edit that turns it back
        # into a denylist naming only one of them fails here.
        bunit = root / "bunit"
        (bunit / "scenarios").mkdir(parents=True)
        (bunit / "scenarios" / "sub_00.json").write_text("{}")
        (bunit / "checks" / "reference_data" / "sub_00").mkdir(parents=True)
        (bunit / "checks" / "reference_data" / "sub_00" / "trace.parquet").write_text("ANSWER")
        (bunit / "references" / "sub_00").mkdir(parents=True)
        (bunit / "references" / "sub_00" / "trace.parquet").write_text("ANSWER")
        (bunit / "batch.json").write_text("{}")
        bstaging = root / "bstaging"
        bstaging.mkdir()

        _stage_input(bunit, bstaging, batch=True)
        names = {p.name for p in bstaging.rglob("*")}
        assert "trace.parquet" not in names
        assert "checks" not in names
        assert "reference_data" not in names
        assert "references" not in names
        assert names == {"scenarios", "sub_00.json"}, names


def test_reported_n_events_both_verbs() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        (out / "events.json").write_text(json.dumps({"n_events": 10669}))
        assert _reported_n_events(out, batch=False) == 10669
        (out / "batch_events.json").write_text(
            json.dumps({"total_events": 24751, "n_scenarios": 3})
        )
        assert _reported_n_events(out, batch=True) == 24751


def test_host_n_events_counts_the_trace_not_the_claim() -> None:
    # The ranked numerator must come from the emitted trace. A submission over-declaring n_events
    # in its own events.json must not be able to raise its events/sec.
    import pyarrow as pa
    import pyarrow.parquet as pq

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        pq.write_table(pa.table({"t_ns": list(range(7))}), out / "trace.parquet")
        (out / "events.json").write_text(
            json.dumps({"n_events": 999999})
        )  # inflated claim
        assert _host_n_events(out, batch=False) == 7
        assert _reported_n_events(out, batch=False) == 999999

        # Batch: the host count is the sum of the per-sub trace row counts.
        for name, rows in (("sub_00", 3), ("sub_01", 4)):
            sub = out / name
            sub.mkdir()
            pq.write_table(pa.table({"t_ns": list(range(rows))}), sub / "trace.parquet")
        (out / "batch_events.json").write_text(json.dumps({"total_events": 123456}))
        assert _host_n_events(out, batch=True) == 7


def _record(unit: str, gpu: float | None, mem: int | None) -> UnitRecord:
    # A realistic record: a deliberately slow warm-up followed by the scored run. run_unit refuses
    # runs < 2 when discarding the warm-up, so a one-run record is not a shape the runner produces.
    # The 10x warm-up is what makes the warm-up-exclusion assertions below meaningful — if any of
    # the four merged statistics regressed to averaging over all runs, the wall clock would come out
    # at 5.5 s rather than 1.0 s and every speedup assertion here would move.
    rec = UnitRecord(unit=unit, verb="simulate", image="img")
    rec.runs = [
        UnitRun(100.0, 1000, 1000, 10.0, gpu, mem, 0),  # warm-up, discarded
        UnitRun(1000.0, 1000, 1000, 1.0, gpu, mem, 0),  # scored
    ]
    rec.median_events_per_sec = 1000.0
    rec.median_host_gpu_seconds = gpu
    rec.median_host_peak_memory_bytes = mem
    return rec


def test_merge_host_metrics_accumulates_units() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "out" / "host_metrics.json"
        merge_host_metrics(_record("t3-fixture-unit-a", 2.0, 4096), path)
        merged = merge_host_metrics(_record("t3-fixture-unit-b", None, 8192), path)
        # Merging, not overwriting: units can be timed independently and appended as they finish.
        assert set(merged) == {"t3-fixture-unit-a", "t3-fixture-unit-b"}
        assert merged["t3-fixture-unit-a"] == {
            # The ranked quantity, carried explicitly so the scorer reads the median of the per-run
            # rates rather than re-deriving a ratio of medians from the two fields below.
            "host_events_per_sec": 1000.0,
            # 1.0, not median(10.0, 1.0) = 5.5: every statistic here is built from the SCORED runs,
            # the same population the ranked median comes from.
            "host_wall_clock_sec": 1.0,
            "host_n_events": 1000,
            "host_gpu_seconds": 2.0,
            "host_peak_memory_bytes": 4096,
            # Stamped into the map itself: an offline consumer that reads only this file must
            # still be told it is holding a developer-profile measurement.
            "profile": "developer",
            "rankable": False,
            "node_fingerprint": {},
        }
        assert json.loads(path.read_text()) == merged


def test_merge_host_metrics_excludes_the_warmup_run() -> None:
    # The ranked median is taken over record.runs[1:]; the merged telemetry must describe that same
    # population. Mixing them let the diagnostics report a different run set than the leaderboard,
    # and at the minimum permitted runs=2 the divergence is the entire warm-up.
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "host_metrics.json"
        entry = merge_host_metrics(_record("t3-fixture-unit-a", 2.0, 4096), path)["t3-fixture-unit-a"]
        assert entry["host_wall_clock_sec"] == 1.0  # scored run only
        assert entry["host_events_per_sec"] == 1000.0  # matches the ranked median

        # With the warm-up kept, every statistic must widen to cover both runs.
        rec = _record("t3-fixture-unit-b", 2.0, 4096)
        rec.warmup_discarded = False
        kept = merge_host_metrics(rec, path)["t3-fixture-unit-b"]
        assert kept["host_wall_clock_sec"] == 5.5  # median(10.0, 1.0)


def test_worker_to_scorer_loop_makes_diagnostics_integrity_backed() -> None:
    # The whole point of the handoff: what the worker measured must override what the submission
    # claimed, and a unit the worker could not fully measure must stay flagged.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        hm = root / "out" / "host_metrics.json"
        merge_host_metrics(_record("t3-fixture-unit-a", 2.0, 4096), hm)  # fully measured
        merge_host_metrics(_record("t3-fixture-unit-b", None, 8192), hm)  # GPU sample missing

        for name in ("t3-fixture-unit-a", "t3-fixture-unit-b"):
            (root / "out" / name).mkdir(parents=True, exist_ok=True)
            (root / "ref" / name).mkdir(parents=True, exist_ok=True)
            (root / "out" / name / "events.json").write_text(
                json.dumps(
                    {
                        "events_per_sec": 1e9,  # fabricated
                        "n_events": 1000,
                        "wall_clock_sec": 0.001,
                        "peak_memory_bytes": 1,
                        "gpu_seconds": 1e-4,
                    }
                )
            )
            (root / "ref" / name / "events.json").write_text(
                json.dumps({"events_per_sec": 100.0})
            )

        rep = build_report(
            root / "out",
            root / "ref",
            host_metrics=json.loads(hm.read_text()),
            env_for=lambda name: (4.0, True),
        )
        ss1, ss2 = rep["units"]["t3-fixture-unit-a"], rep["units"]["t3-fixture-unit-b"]
        assert ss1["telemetry_self_reported"] is False
        assert ss1["efficiency_unit"] == "events_per_gpu_hour"
        # 1000 events over the HOST's 1.0 s against a 100 ev/s baseline, not the claimed 1e9.
        assert abs(ss1["speedup_vs_cpu_abides"] - 10.0) < 1e-9
        assert ss2["telemetry_self_reported"] is True
        assert rep["aggregate"]["n_telemetry_self_reported"] == 1


def test_host_event_count_beats_the_declared_one_in_the_report() -> None:
    # The runner counts events host-side; the scorer must use that count, not the declared one.
    # Otherwise half the ranked fraction would still come from the submission.
    from throughput.report import unit_diagnostics as ud

    cand = {
        "events_per_sec": 1e9,
        "n_events": 1_000_000,  # inflated claim
        "wall_clock_sec": 0.001,
        "peak_memory_bytes": 1,
        "gpu_seconds": 1e-4,
    }
    ref = {"events_per_sec": 100.0}
    host = {
        "host_wall_clock_sec": 1.0,
        "host_n_events": 1000,  # what the harness counted in the trace
        "host_peak_memory_bytes": 4096,
        "host_gpu_seconds": 2.0,
    }
    d = ud(cand, ref, cpus=4.0, gpu=True, host=host)
    assert d.telemetry_self_reported is False
    # 1000 host-counted events over the host's 1.0 s against a 100 ev/s baseline = 10.0,
    # not the 1e6 / 1.0 / 100 = 10_000 the declared count would have produced.
    assert d.speedup_vs_cpu_abides is not None
    assert abs(d.speedup_vs_cpu_abides - 10.0) < 1e-9
    assert d.memory_efficiency is not None
    assert abs(d.memory_efficiency - 1000.0 / 4096.0) < 1e-9


def test_the_participant_image_is_never_re_invoked_for_cleanup() -> None:
    """The regression. ``_reclaim_output`` ran ``docker run <participant image> chown ...`` after
    the timed window; it is removed, and nothing replaced it with another participant invocation."""
    assert not hasattr(ru, "_reclaim_output")
    source = Path(ru.__file__).read_text(encoding="utf-8")
    # The reclaim container was the only place that built a second `docker run` argv. Its two
    # signatures were `--entrypoint chown` and a `-R uid:gid` argv tail; neither may come back.
    assert "--entrypoint" not in source
    assert '"-R"' not in source


def test_retained_output_goes_through_the_no_follow_sanitizer() -> None:
    """``shutil.copytree(..., symlinks=False)`` flattened participant symlinks and copied the
    TARGET's bytes into the retained tree. The retention path must name the shared C3 sanitizer."""
    assert hasattr(ru, "retain_output")
    import inspect

    body = inspect.getsource(ru.retain_output)
    assert "sanitize_participant_tree" in body
    assert "copytree" not in body.split('"""')[-1], (
        "the retention path must not fall back to a following copy"
    )
    # And run_once must actually route through it.
    assert "retain_output(out_dir, keep_output, unit_dir, byte_limit=disk_limit_bytes)" in inspect.getsource(ru.run_once)


def test_the_record_says_it_cannot_rank() -> None:
    record = UnitRecord(unit="t3-x", verb="simulate", image="img:x")
    payload = record.to_dict()
    assert payload["rankable"] is False
    assert payload["profile"] == "developer"
    assert payload["telemetry_source"] == "local_harness"


def test_the_host_metrics_map_says_it_cannot_rank() -> None:
    record = UnitRecord(unit="t3-x", verb="simulate", image="img:x", warmup_discarded=False)
    record.runs.append(UnitRun(100.0, 100, 100, 1.0, None, None, 0))
    record.median_events_per_sec = 100.0
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "host_metrics.json"
        merged = merge_host_metrics(record, path)
    assert merged["t3-x"]["rankable"] is False
    assert merged["t3-x"]["profile"] == "developer"


def test_the_container_invocation_carries_a_hard_deadline() -> None:
    """Pre-fix: ``subprocess.run(cmd, stdout=PIPE, stderr=PIPE)`` with no ``timeout=``, so an image
    that never exits hung the harness and buffered its whole stdout in harness memory."""
    from throughput import timer

    assert timer.DEFAULT_RUN_TIMEOUT_SEC > 0
    assert timer.LOG_TAIL_BYTES > 0
    source = Path(timer.__file__).read_text(encoding="utf-8")
    assert "subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)" not in source


def test_a_hung_container_is_killed_and_reported_as_a_negative_code() -> None:
    """Bounded-runtime case, with no Docker: a shell that sleeps forever stands in for the image."""
    from throughput import timer

    with tempfile.TemporaryDirectory() as tmp:
        cidfile = Path(tmp) / "cid"
        cidfile.write_text("")  # no container to kill; the client kill is what is exercised
        proc = timer.bounded_container_run(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cidfile=cidfile,
            timeout_sec=0.5,
        )
    assert proc.returncode < 0, "a killed run must not look like a clean exit"


def test_log_capture_is_bounded_by_the_tail_cap() -> None:
    """Stdout-flood case. The stream is fully drained -- so the child never blocks -- but only the
    tail is retained, so a flood costs a fixed number of bytes rather than the harness."""
    from throughput import timer

    with tempfile.TemporaryDirectory() as tmp:
        cidfile = Path(tmp) / "cid"
        proc = timer.bounded_container_run(
            [
                sys.executable,
                "-c",
                "import sys\nsys.stdout.write('x' * 5_000_000)\n",
            ],
            cidfile=cidfile,
            timeout_sec=60,
            log_tail_bytes=4096,
        )
    assert proc.returncode == 0
    assert len(proc.stdout) == 4096


def test_a_clean_bounded_run_is_unaffected() -> None:
    """Positive control: the bounds must not fail the legitimate case."""
    from throughput import timer

    with tempfile.TemporaryDirectory() as tmp:
        cidfile = Path(tmp) / "cid"
        proc = timer.bounded_container_run(
            [sys.executable, "-c", "print('hello')"], cidfile=cidfile, timeout_sec=60
        )
    assert proc.returncode == 0
    assert b"hello" in proc.stdout


def test_kill_container_never_raises_on_a_missing_or_empty_cidfile() -> None:
    from throughput import timer

    with tempfile.TemporaryDirectory() as tmp:
        timer.kill_container(Path(tmp) / "absent")
        (Path(tmp) / "empty").write_text("")
        timer.kill_container(Path(tmp) / "empty")


def test_entry_from_diagnostics_carries_provenance() -> None:
    # The cross-submission assembler lives outside this repo. Building entries through this
    # constructor is what stops it from dropping the provenance fields and silently producing an
    # entry that is either ineligible or (before the defaults were fixed) fabricable.
    measured = compute_diagnostics(
        events_per_sec=1000.0,
        n_events=1000,
        wall_clock_sec=1.0,
        peak_memory_bytes=4096,
        gpu_seconds=2.0,
        baseline_events_per_sec=100.0,
        cpus=4.0,
        gpu=True,
        telemetry_self_reported=False,
    )
    claimed = compute_diagnostics(
        events_per_sec=1e9,
        n_events=1000,
        wall_clock_sec=0.001,
        peak_memory_bytes=1,
        gpu_seconds=1e-4,
        baseline_events_per_sec=100.0,
        cpus=4.0,
        gpu=True,
    )  # telemetry_self_reported defaults to True

    honest = entry_from_diagnostics("honest", measured, is_gpu=True)
    cheater = entry_from_diagnostics("cheater", claimed, is_gpu=True)

    assert honest.telemetry_self_reported is False
    assert honest.efficiency_unit == "events_per_gpu_hour"
    assert cheater.telemetry_self_reported is True
    # Despite a vastly larger efficiency, the self-reported entry cannot win either award.
    assert cheater.efficiency > honest.efficiency
    assert best_gpu_acceleration([cheater, honest]) == "honest"
    assert best_gpu_acceleration([cheater]) is None
    assert best_systems_diagnosis([cheater]) is None


def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
