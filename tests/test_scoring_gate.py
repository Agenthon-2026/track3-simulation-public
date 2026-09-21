"""End-to-end through the two named factories, on a synthetic unit.

Covers the parts that only appear once the gates are wired together: which factory ranks, what an
organizer fault does to the evaluation, that a malformed parquet is contained to one unit instead
of aborting the driver's whole loop, and that the declared scenario/seed/digest are compared to the
organizer's scenario rather than merely required to exist.

    python -m pytest tests/test_scoring_gate.py
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd  # noqa: E402

import _contract_fixtures as F  # noqa: E402
from qfbench2_common.contracts import OrganizerFault  # noqa: E402
from qfbench2_common.failure_labels import (  # noqa: E402
    ORGANIZER_FAULT_LABELS,
    FailureLabel,
)

from qfbench2_track_simulation import scoring  # noqa: E402
from qfbench2_track_simulation.scoring import (  # noqa: E402
    build_developer_verifier,
    build_verifier,
)
from test_ledger_completeness import ledger  # noqa: E402
from test_semantics_exactness import trace  # noqa: E402

SCENARIO_ID = "synthetic-scenario-0001"
SEED = 424242
N_ROWS = 400

CARD = """schema_version = "2.0"

[task]
id              = "t3-synthetic"
track           = "simulation"
scenario_family = "matching-engine-semantics"
scenario_file   = "scenario.json"

[scoring.params]
semantic_tier          = "A"
timestamp_tolerance_ns = 1000
requires_message_ledger = true
"""


def _events(n: int, wall: float, digest: str) -> dict[str, object]:
    return {
        "scenario_id": SCENARIO_ID,
        "seed": SEED,
        "n_events": n,
        "wall_clock_sec": wall,
        "events_per_sec": n / wall,
        "trace_sha256": digest,
    }


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_unit(root: Path, *, card: str = CARD) -> tuple[Path, Path]:
    """An organizer reference tree and a faithful candidate output tree."""
    ref_root, res_root = root / "ref", root / "res"
    unit = ref_root / F.UNIT_HANDLE
    out = res_root / F.UNIT_HANDLE
    unit.mkdir(parents=True)
    out.mkdir(parents=True)

    frame = trace(N_ROWS)
    frame.to_parquet(unit / "trace.parquet")
    frame.to_parquet(out / "trace.parquet")
    msg = ledger(200)
    msg.to_parquet(unit / "message_trace.parquet")
    msg.to_parquet(out / "message_trace.parquet")

    (unit / "card.toml").write_text(card)
    (unit / "scenario.json").write_text(
        json.dumps({"scenario_id": SCENARIO_ID, "seed": SEED, "schema_version": 2})
    )
    (unit / "events.json").write_text(
        json.dumps(_events(N_ROWS, 3.0, _sha(unit / "trace.parquet")))
    )
    (out / "events.json").write_text(
        json.dumps(_events(N_ROWS, 1.5, _sha(out / "trace.parquet")))
    )
    return unit, out


def official_ctx(root: Path, **record_over: object) -> dict[str, object]:
    unit, out = build_unit(root)
    record_over.setdefault("n_events", N_ROWS)
    return {
        "unit_dir": unit,
        "output_dir": out,
        "unit_handle": F.UNIT_HANDLE,
        "plan": F.plan(),
        "run_record": F.bind_output(F.run_record_mapping(**record_over), unit, out),
    }


def developer_ctx(root: Path) -> dict[str, object]:
    unit, out = build_unit(root)
    return {"unit_dir": unit, "output_dir": out}


def _expect(exc_type, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc_type as exc:
        return exc
    raise AssertionError(f"expected {exc_type.__name__}, nothing raised")


# --------------------------------------------------------------------------- positive controls
def test_the_official_factory_ranks_a_clean_submission(tmp_path: Path) -> None:
    ctx = official_ctx(tmp_path)
    verdict = build_verifier(ctx).run(ctx)
    assert verdict.admissible, verdict.gate_results
    assert verdict.detail["rankable"] is True
    assert verdict.detail["profile"] == "official"
    # The rate is the HOST's: 400 rows over a 1.0 s median repeat, not 400 / 1.5 s from the
    # submission's own sidecar.
    assert abs(verdict.score - 400.0) < 1e-9
    assert verdict.detail["measured_repeats"] == 4


def test_the_developer_factory_runs_the_same_gates_and_never_ranks(tmp_path: Path) -> None:
    ctx = developer_ctx(tmp_path)
    verdict = build_developer_verifier(ctx).run(ctx)
    assert verdict.admissible, verdict.gate_results
    assert verdict.detail["rankable"] is False
    assert verdict.detail["profile"] == "developer"
    assert verdict.detail["score_source"] == "self_reported"


# --------------------------------------------------------------------------- factory separation
def test_the_official_factory_refuses_a_context_without_trusted_evidence(tmp_path: Path) -> None:
    """A harness that cannot supply C1+C2 cannot use the production factory at all. Pre-fix the
    same factory silently ranked the submission's own number in that situation."""
    ctx = developer_ctx(tmp_path)
    exc = _expect(OrganizerFault, lambda: build_verifier(ctx).run(ctx))
    assert "build_developer_verifier" in str(exc)


def test_there_is_no_flag_that_turns_the_developer_profile_into_the_official_one() -> None:
    source = Path(scoring.__file__).read_text(encoding="utf-8")
    assert "QFB2_T3_REQUIRE_HOST_TELEMETRY" not in source
    assert "os.environ" not in source, (
        "no environment variable may decide whether the ranked path has a fallback"
    )


# --------------------------------------------------------------------------- organizer faults
def test_a_ceiling_that_would_clip_an_honest_score_aborts(tmp_path: Path) -> None:
    ctx = official_ctx(tmp_path)
    ctx["plan"] = F.plan(domain_max=2_000_000.0)
    exc = _expect(OrganizerFault, lambda: build_verifier(ctx).run(ctx))
    assert "metric.domain.max" in str(exc)


def test_a_missing_reference_trace_is_an_organizer_fault_label(tmp_path: Path) -> None:
    """Reference integrity aborts rather than appending a warning: the label is in the shared
    ORGANIZER_FAULT_LABELS set, which the driver turns into a whole-evaluation abort."""
    ctx = official_ctx(tmp_path)
    (Path(ctx["unit_dir"]) / "trace.parquet").unlink()  # type: ignore[arg-type]
    verdict = build_verifier(ctx).run(ctx)
    assert not verdict.admissible
    assert FailureLabel.T3_REFERENCE_INTEGRITY_ERROR in verdict.labels
    assert FailureLabel.T3_REFERENCE_INTEGRITY_ERROR in ORGANIZER_FAULT_LABELS


def test_a_card_that_requires_a_ledger_with_no_reference_is_an_organizer_fault(
    tmp_path: Path,
) -> None:
    ctx = official_ctx(tmp_path)
    (Path(ctx["unit_dir"]) / "message_trace.parquet").unlink()  # type: ignore[arg-type]
    verdict = build_verifier(ctx).run(ctx)
    assert not verdict.admissible
    assert FailureLabel.T3_REFERENCE_INTEGRITY_ERROR in verdict.labels


# --------------------------------------------------------------------------- participant failures
def test_a_corrupt_candidate_parquet_fails_one_unit_and_does_not_escape(tmp_path: Path) -> None:
    """Pre-fix: `pd.read_parquet` raised ArrowInvalid straight out of `build_verifier(...).run()`,
    and the hub driver called it with no try — so one malformed file aborted the whole unit loop.
    That is a participant-triggerable denial of service against every other submission."""
    ctx = official_ctx(tmp_path)
    out = Path(ctx["output_dir"])  # type: ignore[arg-type]
    (out / "trace.parquet").write_bytes(b"not a parquet file")
    # Declare a digest that matches the corrupt bytes, so the schema gate passes it through and
    # the corruption is only discovered when a parser touches it -- the case that used to escape.
    (out / "events.json").write_text(
        json.dumps(_events(N_ROWS, 1.5, _sha(out / "trace.parquet")))
    )
    ctx["run_record"] = F.bind_output(dict(ctx["run_record"].raw), Path(ctx["unit_dir"]), out)
    verdict = build_verifier(ctx).run(ctx)
    assert not verdict.admissible
    assert FailureLabel.T3_PARSE_ERROR in verdict.labels


def test_a_corrupt_reference_parquet_is_ours_not_the_submission_s(tmp_path: Path) -> None:
    """The same corruption on the ORGANIZER's side must abort, not be charged to whichever
    submission happened to be scored against it."""
    ctx = official_ctx(tmp_path)
    (Path(ctx["unit_dir"]) / "trace.parquet").write_bytes(b"not a parquet file")  # type: ignore[arg-type]
    verdict = build_verifier(ctx).run(ctx)
    assert not verdict.admissible
    assert FailureLabel.T3_REFERENCE_INTEGRITY_ERROR in verdict.labels
    assert FailureLabel.T3_PARSE_ERROR not in verdict.labels


def test_a_corrupt_events_sidecar_fails_one_unit(tmp_path: Path) -> None:
    ctx = official_ctx(tmp_path)
    (Path(ctx["output_dir"]) / "events.json").write_text("{not json")  # type: ignore[arg-type]
    verdict = build_verifier(ctx).run(ctx)
    assert not verdict.admissible
    assert FailureLabel.T3_PARSE_ERROR in verdict.labels


def test_a_wrong_scenario_id_is_refused(tmp_path: Path) -> None:
    """Pre-fix, scenario_id / seed / trace_sha256 were required only to EXIST. Measured end to
    end, a wrong scenario id with seed -1 and digest 'deadbeef' scored 2,200,000 events/sec."""
    ctx = official_ctx(tmp_path)
    out = Path(ctx["output_dir"])  # type: ignore[arg-type]
    payload = json.loads((out / "events.json").read_text())
    payload["scenario_id"] = "some-other-scenario"
    (out / "events.json").write_text(json.dumps(payload))
    verdict = build_verifier(ctx).run(ctx)
    assert not verdict.admissible
    assert FailureLabel.SCHEMA_INVALID_OUTPUT in verdict.labels


def test_a_wrong_seed_is_refused(tmp_path: Path) -> None:
    ctx = official_ctx(tmp_path)
    out = Path(ctx["output_dir"])  # type: ignore[arg-type]
    payload = json.loads((out / "events.json").read_text())
    payload["seed"] = -1
    (out / "events.json").write_text(json.dumps(payload))
    verdict = build_verifier(ctx).run(ctx)
    assert not verdict.admissible


def test_a_fabricated_trace_digest_is_refused(tmp_path: Path) -> None:
    ctx = official_ctx(tmp_path)
    out = Path(ctx["output_dir"])  # type: ignore[arg-type]
    payload = json.loads((out / "events.json").read_text())
    payload["trace_sha256"] = "deadbeef" * 8
    (out / "events.json").write_text(json.dumps(payload))
    verdict = build_verifier(ctx).run(ctx)
    assert not verdict.admissible


def test_a_fabricated_wall_clock_cannot_change_the_rank(tmp_path: Path) -> None:
    """The whole point. The submission claims a 1,000x faster run; the host clock decides."""
    ctx = official_ctx(tmp_path)
    out = Path(ctx["output_dir"])  # type: ignore[arg-type]
    payload = json.loads((out / "events.json").read_text())
    payload["wall_clock_sec"] = 0.0015
    payload["events_per_sec"] = N_ROWS / 0.0015
    (out / "events.json").write_text(json.dumps(payload))
    verdict = build_verifier(ctx).run(ctx)
    assert verdict.admissible, verdict.gate_results
    assert abs(verdict.score - 400.0) < 1e-9, "the ranked rate must be the host's, not the claim"


def test_a_padded_candidate_trace_is_refused(tmp_path: Path) -> None:
    ctx = official_ctx(tmp_path)
    out = Path(ctx["output_dir"])  # type: ignore[arg-type]
    frame = trace(N_ROWS)
    padded = pd.concat([frame, frame.iloc[:50]], ignore_index=True)
    padded.to_parquet(out / "trace.parquet")
    payload = _events(len(padded), 1.5, _sha(out / "trace.parquet"))
    (out / "events.json").write_text(json.dumps(payload))
    verdict = build_verifier(ctx).run(ctx)
    assert not verdict.admissible


def test_a_missing_candidate_ledger_is_refused_when_the_card_requires_one(
    tmp_path: Path,
) -> None:
    ctx = official_ctx(tmp_path)
    (Path(ctx["output_dir"]) / "message_trace.parquet").unlink()  # type: ignore[arg-type]
    verdict = build_verifier(ctx).run(ctx)
    assert not verdict.admissible
    assert FailureLabel.T3_SEMANTIC_REGRESSION in verdict.labels


def test_a_card_can_waive_the_ledger_and_the_unit_still_scores(tmp_path: Path) -> None:
    """Positive control for the declaration: a card that says no ledger is required must not have
    its unit refused for not having one."""
    ctx = official_ctx(tmp_path)
    unit = Path(ctx["unit_dir"])  # type: ignore[arg-type]
    unit.joinpath("card.toml").write_text(
        CARD.replace("requires_message_ledger = true", "requires_message_ledger = false")
    )
    unit.joinpath("message_trace.parquet").unlink()
    Path(ctx["output_dir"]).joinpath("message_trace.parquet").unlink()  # type: ignore[arg-type]
    ctx.pop("_t3_card", None)
    ctx["run_record"] = F.bind_output(
        dict(ctx["run_record"].raw), unit, Path(ctx["output_dir"])
    )
    verdict = build_verifier(ctx).run(ctx)
    assert verdict.admissible, verdict.gate_results


def _run_all() -> int:
    import tempfile

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            if "tmp_path" in t.__code__.co_varnames[: t.__code__.co_argcount]:
                with tempfile.TemporaryDirectory() as tmp:
                    t(Path(tmp))
            else:
                t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
