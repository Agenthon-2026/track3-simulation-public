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
import pytest  # noqa: E402

import _contract_fixtures as F  # noqa: E402
from qfbench2_common.contracts import OrganizerFault, ParticipantFailure  # noqa: E402
from qfbench2_common.contracts import run_record as C2  # noqa: E402
from qfbench2_common.failure_labels import (  # noqa: E402
    ORGANIZER_FAULT_LABELS,
    FailureLabel,
    public_failure_code,
)

from qfbench2_track_simulation import scoring, telemetry  # noqa: E402
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


def _developer_ctx_reporting(root: Path, rate: float) -> dict[str, object]:
    """A developer context whose candidate self-reports `rate`, with a consistent triple."""
    unit, out = build_unit(root)
    (out / "events.json").write_text(
        json.dumps(_events(N_ROWS, N_ROWS / rate, _sha(out / "trace.parquet")))
    )
    return {"unit_dir": unit, "output_dir": out}


def test_a_normal_developer_rate_scores_unchanged(tmp_path: Path) -> None:
    ctx = developer_ctx(tmp_path)
    verdict = build_developer_verifier(ctx).run(ctx)
    assert abs(verdict.score - N_ROWS / 1.5) < 1e-6
    assert "score_capped_from" not in verdict.detail


def test_a_rate_above_the_cap_is_admitted_and_scores_the_cap(tmp_path: Path) -> None:
    """A synthetic 1.2e7 sits above the 1e7 cap and far below the 1e9 refusal line: it is admitted,
    not zeroed, and scores exactly the cap."""
    ctx = _developer_ctx_reporting(tmp_path, 1.2e7)
    verdict = build_developer_verifier(ctx).run(ctx)
    assert verdict.admissible, verdict.gate_results
    assert verdict.score == 1e7
    assert abs(verdict.detail["score_capped_from"] - 1.2e7) < 1.0


def test_no_admitted_rate_scores_above_the_cap(tmp_path: Path) -> None:
    """A self-reported rate cannot be verified on the developer path, so any admitted claim, however
    close to the 1e9 refusal line, scores at most the 1e7 cap."""
    ctx = _developer_ctx_reporting(tmp_path, 9e8)
    verdict = build_developer_verifier(ctx).run(ctx)
    assert verdict.admissible, verdict.gate_results
    assert verdict.score == 1e7


def test_an_absurd_developer_rate_is_still_refused(tmp_path: Path) -> None:
    ctx = _developer_ctx_reporting(tmp_path, 1e11)
    verdict = build_developer_verifier(ctx).run(ctx)
    assert not verdict.admissible


def test_a_batch_unit_is_capped_at_1e7_like_any_other_unit(tmp_path: Path) -> None:
    """The cap is per unit, batch units included, which is where the live Development average
    already clips every unit. Its market count does not raise it."""
    unit = tmp_path / "bunit"
    unit.mkdir()
    (unit / "batch.json").write_text(json.dumps({"subs": [{"sub": f"sub_{i:02d}"} for i in range(3)]}))
    detail = scoring._developer_score(
        {
            "unit_dir": unit,
            "_batch": {"n_subs": 3},
            "_batch_events": {"events_per_sec": 5e8, "n_scenarios": 3},
        }
    )
    assert detail["score"] == 1e7
    assert detail["score_capped_from"] == 5e8


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
def _failed_raw(runs: int, **lifecycle: object) -> dict:
    """A C2 whose Runner stopped at a failed run, carrying the ``runs`` so far (a legacy 1.1.0 body,
    as the Hub fixture is)."""
    raw = F.run_record_mapping(n_events=N_ROWS)
    raw["repeats"] = raw["repeats"][:runs]
    raw["lifecycle"].update({"exit_code": 1, **lifecycle})
    raw["participant_outcome"] = "failure"
    return raw


def _failed_ctx(root: Path, raw: dict, *, sign: bool = True) -> dict:
    """An official context over ``raw``. The retained output is a valid one, so an admissible verdict
    would mean the scorer graded an earlier run's output instead of the failure."""
    ctx = official_ctx(root)
    ctx["run_record"] = F.signed_record(raw) if sign else F.RunRecord.from_mapping(raw)
    return ctx


def _charged(ctx: dict) -> tuple[list, dict]:
    verdict = build_verifier(ctx).run(ctx)
    assert not verdict.admissible
    assert not set(verdict.labels) & set(ORGANIZER_FAULT_LABELS)
    (g0,) = verdict.gate_results.values()
    return verdict.labels, g0.detail


def test_a_run_that_fails_part_way_is_a_participant_failure(tmp_path: Path) -> None:
    """The Runner stops at the first failed run, so run 3 of 5 crashing leaves 3 signed repeats. A
    direct caller of build_verifier used to get an organizer fault here, because the gate demanded
    all 5 repeats before reading anything else."""
    labels, detail = _charged(_failed_ctx(tmp_path, _failed_raw(3)))
    assert labels == [] and detail["code"] == "container_crashed"  # no label maps to this code
    assert detail["repeats_recorded"] == 3


def test_a_failure_with_no_runs_recorded_is_still_charged(tmp_path: Path) -> None:
    labels, detail = _charged(_failed_ctx(tmp_path, _failed_raw(0)))
    assert detail["code"] == "container_crashed" and detail["repeats_recorded"] == 0


def test_a_failure_reports_the_platforms_code_for_its_lifecycle(tmp_path: Path) -> None:
    """The same codes the platform reports: a kill is a crash, a container that never started is
    an unusable image, and a timeout or OOM keeps its own label."""
    cases = [
        ({"exit_code": 137}, "container_crashed", None),
        ({"phase_reached": "created", "daemon_status": "created", "exit_code": None},
         "image_unusable", FailureLabel.INTEGRITY_BAD_IMAGE_HASH),
        ({"timed_out": True}, "resource_timeout", FailureLabel.RESOURCE_TIMEOUT),
        ({"oom_killed": True}, "resource_oom", FailureLabel.RESOURCE_OOM),
        # Both flags: timeout wins, as on the platform.
        ({"timed_out": True, "oom_killed": True}, "resource_timeout", FailureLabel.RESOURCE_TIMEOUT),
    ]
    for i, (lifecycle, code, label) in enumerate(cases):
        labels, detail = _charged(_failed_ctx(tmp_path / str(i), _failed_raw(2, **lifecycle)))
        assert detail["code"] == code, lifecycle
        assert labels == ([label] if label else []), lifecycle
        assert public_failure_code(labels) == code or label is None


def test_a_failed_run_is_never_graded_on_an_earlier_runs_output(tmp_path: Path) -> None:
    """Even with all 5 repeats present and a valid retained output, a recorded failure decides."""
    _charged(_failed_ctx(tmp_path, _failed_raw(5)))


def test_the_cross_check_path_sees_the_failure_too(tmp_path: Path) -> None:
    """``ranked_timing`` is what the private cross-check scorer calls, and it reaches the same
    failure through ``require_repeat_evidence``: a participant failure, never a ranked rate."""
    ctx = _failed_ctx(tmp_path, _failed_raw(3))
    exc = _expect(
        telemetry.RunFailed,
        lambda: telemetry.ranked_timing(
            ctx["run_record"], ctx["plan"], unit_dir=ctx["unit_dir"], output_dir=ctx["output_dir"]
        ),
    )
    assert isinstance(exc, ParticipantFailure)


def test_a_failure_is_held_without_admissible_telemetry_on_an_idle_instance(tmp_path: Path) -> None:
    """Checked inside ``require_repeat_evidence`` itself, so no caller can charge a failure first."""
    for i, block in enumerate(
        [F.telemetry_block(exclusive=False), F.telemetry_block(samples_taken=10, samples_missed=90)]
    ):
        raw = _failed_raw(3)
        raw["telemetry"] = block
        ctx = _failed_ctx(tmp_path / str(i), raw)
        _expect(OrganizerFault, lambda: build_verifier(ctx).run(ctx))
        _expect(OrganizerFault, lambda: telemetry.require_repeat_evidence(ctx["run_record"], ctx["plan"]))


def test_a_failure_whose_record_is_unrankable_is_held(tmp_path: Path) -> None:
    raw = _failed_raw(3)
    raw["rankability"] = {"state": "unrankable", "unmet_controls": ["tier_unenforced"]}
    ctx = _failed_ctx(tmp_path, raw)
    _expect(OrganizerFault, lambda: build_verifier(ctx).run(ctx))


def test_a_legacy_create_timeout_is_held_as_the_hub_holds_it(tmp_path: Path) -> None:
    """A 1.1.0 record carries no execution_fault, so a container that timed out while still being
    created must not become a participant timeout here."""
    raw = _failed_raw(0, timed_out=True, phase_reached="created", daemon_status="created", exit_code=None)
    ctx = _failed_ctx(tmp_path, raw)
    exc = _expect(OrganizerFault, lambda: build_verifier(ctx).run(ctx))
    assert "create_timeout" in str(exc)


_REFUSALS = getattr(C2, "PARTICIPANT_REFUSAL_FAILURE_CODES", None)
_needs_refusal = pytest.mark.skipif(_REFUSALS is None, reason="toolkit older than v2.5.1 (C2 1.3.0)")


def _refused_raw(refusal: str | None, runs: int = 5) -> dict:
    """A C2 1.3.0 body: a clean exit whose output the Runner refused (or did not, for ``None``)."""
    raw = F.run_record_mapping(n_events=N_ROWS)
    raw["repeats"] = raw["repeats"][:runs]
    raw["schema_version"] = "1.3.0"
    raw["execution_fault"] = {"attribution": "none", "reason": "none", "evidence": "host_lifecycle"}
    raw["participant_refusal"] = refusal
    raw["participant_outcome"] = "failure" if refusal else "success"
    return raw


@_needs_refusal
def test_a_refusal_reports_the_toolkits_code_for_it(tmp_path: Path) -> None:
    """The code comes from the toolkit's own mapping, so the Hub and this scorer report the same."""
    assert _REFUSALS
    for i, (refusal, code) in enumerate(_REFUSALS.items()):
        labels, detail = _charged(_failed_ctx(tmp_path / str(i), _refused_raw(refusal, runs=3)))
        assert detail["code"] == code.value, refusal
        assert detail["repeats_recorded"] == 3
        # Exactly the one label whose public code is this code; none where the closed label set has
        # none (`no_output`, like `container_crashed`), and then detail alone carries the code.
        label = next((lb for lb in FailureLabel if public_failure_code([lb]) == code), None)
        assert labels == ([label] if label else []), refusal


@_needs_refusal
def test_a_refusal_is_held_without_admissible_telemetry_on_an_idle_instance(tmp_path: Path) -> None:
    raw = _refused_raw("repeats_differ")
    raw["telemetry"] = F.telemetry_block(exclusive=False)
    ctx = _failed_ctx(tmp_path, raw)
    _expect(OrganizerFault, lambda: build_verifier(ctx).run(ctx))


@_needs_refusal
def test_a_refusal_needs_the_same_evidence_as_any_failure(tmp_path: Path) -> None:
    """A refusal is held, never charged, on an unrankable run, on an unattested record, or past the
    plan's repeat count. The telemetry checks above are also run by the gate itself, so these are
    the cases only ``require_failure_evidence`` catches."""
    raw = _refused_raw("repeats_differ", runs=3)
    raw["repeats"][0]["rankability"] = {"state": "unrankable", "unmet_controls": ["telemetry_absent"]}
    ctx = _failed_ctx(tmp_path / "unrankable", raw)
    _expect(OrganizerFault, lambda: build_verifier(ctx).run(ctx))

    ctx = _failed_ctx(tmp_path / "unattested", _refused_raw("trace_missing", runs=3), sign=False)
    _expect(OrganizerFault, lambda: build_verifier(ctx).run(ctx))

    raw = _refused_raw("no_stable_output", runs=5)
    raw["repeats"].append(dict(raw["repeats"][-1], index=5))
    ctx = _failed_ctx(tmp_path / "too_many", raw)
    _expect(OrganizerFault, lambda: build_verifier(ctx).run(ctx))


@_needs_refusal
def test_a_refusal_the_toolkit_cannot_map_is_held(tmp_path: Path, monkeypatch) -> None:
    """Unreachable with a matching toolkit, which rejects unknown codes when it parses the record;
    held rather than charged under a guessed code, as the Hub does."""
    ctx = _failed_ctx(tmp_path, _refused_raw("sidecar_invalid", runs=3))
    monkeypatch.setattr(
        telemetry,
        "_REFUSAL_FAILURE_CODES",
        {k: v for k, v in telemetry._REFUSAL_FAILURE_CODES.items() if k != "sidecar_invalid"},
    )
    exc = _expect(OrganizerFault, lambda: build_verifier(ctx).run(ctx))
    assert "no public failure code" in str(exc)


@_needs_refusal
def test_a_record_with_no_refusal_still_ranks(tmp_path: Path) -> None:
    ctx = official_ctx(tmp_path)
    ctx["run_record"] = F.bind_output(_refused_raw(None), ctx["unit_dir"], ctx["output_dir"])
    verdict = build_verifier(ctx).run(ctx)
    assert verdict.admissible, verdict.gate_results


def test_an_unattested_failure_is_held_not_charged(tmp_path: Path) -> None:
    ctx = _failed_ctx(tmp_path, _failed_raw(3), sign=False)
    _expect(OrganizerFault, lambda: build_verifier(ctx).run(ctx))


def test_a_failure_on_an_unrankable_run_or_past_the_plan_is_held(tmp_path: Path) -> None:
    raw = _failed_raw(3)
    raw["repeats"][0]["rankability"] = {"state": "unrankable", "unmet_controls": ["telemetry_absent"]}
    ctx = _failed_ctx(tmp_path / "unrankable", raw)
    _expect(OrganizerFault, lambda: build_verifier(ctx).run(ctx))

    raw = _failed_raw(5)
    raw["repeats"].append(dict(raw["repeats"][-1], index=5))
    ctx = _failed_ctx(tmp_path / "too_many", raw)
    exc = _expect(OrganizerFault, lambda: build_verifier(ctx).run(ctx))
    assert "more than the 5" in str(exc)


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
