"""The ranked rate comes from trusted C2 evidence, or it does not exist.

The defect this file pins closed: ``host_metrics.resolve()`` fell back to
``float(self_reported)`` when no harness measurement existed, and none ever existed, because the
common ingestion path never wrote the handoff. The consistency checks pinned
``events_per_sec == n_events / wall_clock_sec`` and pinned ``n_events`` to the real trace rows —
but ``wall_clock_sec`` came from the submission and was compared against nothing, so a
self-consistent triple with a shrunken clock passed every gate at an arbitrary rank.

Each test below fails on the pre-fix code for a stated reason.

    python -m pytest tests/test_telemetry_binding.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _contract_fixtures as F  # noqa: E402
import pandas as pd  # noqa: E402
import pytest  # noqa: E402
from qfbench2_common.contracts import OrganizerFault, ParticipantFailure  # noqa: E402
from qfbench2_common.sanitize import materialize_tree  # noqa: E402

from qfbench2_track_simulation import telemetry as T  # noqa: E402
from qfbench2_track_simulation.limits import allowed_paths_for  # noqa: E402

REPO = Path(__file__).resolve().parent.parent

N = 72_061


def _expect(exc_type, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc_type as exc:
        return exc
    raise AssertionError(f"expected {exc_type.__name__}, nothing raised")


# --------------------------------------------------------------------------- positive control
def test_a_clean_record_produces_the_host_measured_rate() -> None:
    """The positive control. A gate that rejects the legitimate case makes every rejection
    beside it uninterpretable, so this one runs first."""
    timing = T.ranked_timing(F.run_record(), F.plan(), reference_event_count=N)
    assert timing.rankable is True
    assert timing.profile == T.PROFILE_OFFICIAL
    assert timing.n_events == N
    assert timing.measured_repeats == 4  # 5 repeats, 1 warm-up discarded
    assert abs(timing.events_per_sec - N / 1.0) < 1e-9


# --------------------------------------------------------------------------- the headline
def test_no_telemetry_means_no_score_not_a_self_reported_score() -> None:
    """Pre-fix: `resolve(None, unit, self_reported)` returned the submission's number. Now it is
    an organizer fault, because a missing measurement is missing evidence, not a low result."""
    record = F.run_record(telemetry=None)
    exc = _expect(OrganizerFault, T.ranked_timing, record, F.plan(), reference_event_count=N)
    assert "telemetry_absent" in str(exc)


def test_a_participant_file_shaped_like_the_handoff_cannot_become_the_score() -> None:
    """A ``host_metrics.json`` planted in the output tree is not C2 and never reaches the rate.

    C2 exists only as a Runner-signed artifact in the organizer control root, so there is no
    filesystem path by which participant bytes are promoted. Asserted structurally: the ranked
    number is computed from the record alone, and a lookalike file in the output directory changes
    nothing about it."""
    with tempfile.TemporaryDirectory() as tmp:
        planted = Path(tmp) / "host_metrics.json"
        planted.write_text(
            json.dumps({F.UNIT_HANDLE: {"host_events_per_sec": 9.9e8}})
        )
        timing = T.ranked_timing(F.run_record(), F.plan(), reference_event_count=N)
    assert timing.events_per_sec < 1e6, "a planted lookalike must not influence the ranked rate"
    assert T.ranked_timing.__module__ == "qfbench2_track_simulation.telemetry"


# --------------------------------------------------------------------------- telemetry quality
def test_low_coverage_is_refused() -> None:
    record = F.run_record(telemetry=F.telemetry_block(samples_taken=4000, samples_missed=1040))
    exc = _expect(OrganizerFault, T.ranked_timing, record, F.plan(), reference_event_count=N)
    assert "coverage_fraction" in str(exc)


def test_a_wrong_sampling_interval_is_refused() -> None:
    record = F.run_record(telemetry=F.telemetry_block(sampling_interval_ms=200))
    exc = _expect(OrganizerFault, T.ranked_timing, record, F.plan(), reference_event_count=N)
    assert "sampling_interval_ms" in str(exc)


def test_device_index_only_telemetry_is_inadmissible() -> None:
    """C2 refuses an index in `gpu_uuid` outright; a null UUID reaches our gate and is refused
    there. Either way there is no route by which an index-attributed sample ranks."""
    record = F.run_record(telemetry=F.telemetry_block(gpu_uuid=None))
    exc = _expect(OrganizerFault, T.ranked_timing, record, F.plan(), reference_event_count=N)
    assert "UUID" in str(exc)


def test_missing_cgroup_attribution_is_refused() -> None:
    """Refused twice, at two layers, on purpose.

    C2 parsing rejects an empty ``participant_cgroup_id`` outright, so a record without cgroup
    attribution never reaches Track 3. The gate checks it as well, because the layer that refuses
    first today is not guaranteed to be the layer that refuses first tomorrow.
    """
    from qfbench2_common.contracts import ContractError

    _expect(
        ContractError,
        F.run_record,
        telemetry=F.telemetry_block(participant_cgroup_id=""),
    )
    admissible, reasons = _telemetry_reasons(participant_cgroup_id="")
    assert not admissible and any("cgroup" in r for r in reasons), reasons


def _telemetry_reasons(**over: object) -> tuple[bool, tuple[str, ...]]:
    """Ask the shared helper directly, bypassing C2's own stricter parse."""
    from qfbench2_common.contracts import telemetry_admissible_for_timing

    class _Fake:
        telemetry = dict(F.telemetry_block())
        telemetry.update(over)

    return telemetry_admissible_for_timing(
        _Fake(),  # type: ignore[arg-type]
        min_coverage=T.MIN_COVERAGE_FRACTION,
        sampling_interval_ms=T.SAMPLING_INTERVAL_MS,
        max_consecutive_missed=T.MAX_CONSECUTIVE_MISSED_SAMPLES,
    )


def test_a_contended_or_throttled_box_is_refused() -> None:
    """Track 3's fairness rule is 'same pinned, otherwise-idle instance'. A rate measured next to
    another workload ranks the scheduler."""
    for block, needle in (
        (F.telemetry_block(exclusive=False), "not exclusive"),
        (F.telemetry_block(contender_process_count=3), "contender"),
        (F.telemetry_block(throttled=True), "throttling"),
    ):
        record = F.run_record(telemetry=block)
        exc = _expect(OrganizerFault, T.ranked_timing, record, F.plan(), reference_event_count=N)
        assert needle in str(exc), (needle, str(exc))


# --------------------------------------------------------------------------- repeats
def test_every_repeat_is_validated_not_only_the_last() -> None:
    """Pre-fix: only the final repeat's output was retained and checked, while the median ran over
    every repeat. An alternating fast-invalid / slow-valid submission was invisible unless the
    invalid repeat happened to be last. Here the FIRST measured repeat diverges and the last three
    are clean, and it still fails."""
    record = F.run_record(
        elapsed_per_repeat=[1.4, 0.01, 1.0, 1.0, 1.0],
        repeat_digests=[F.TREE_DIGEST, F.OTHER_TREE_DIGEST] + [F.TREE_DIGEST] * 3,
    )
    exc = _expect(ParticipantFailure, T.ranked_timing, record, F.plan(), reference_event_count=N)
    assert "different output tree" in str(exc)


def test_alternating_fast_invalid_repeats_fail_in_either_order() -> None:
    for digests in (
        [F.TREE_DIGEST, F.OTHER_TREE_DIGEST, F.TREE_DIGEST, F.OTHER_TREE_DIGEST, F.TREE_DIGEST],
        [F.TREE_DIGEST, F.TREE_DIGEST, F.TREE_DIGEST, F.TREE_DIGEST, F.OTHER_TREE_DIGEST],
    ):
        record = F.run_record(repeat_digests=digests)
        _expect(ParticipantFailure, T.ranked_timing, record, F.plan(), reference_event_count=N)


def _honest_repeat_tree(root: Path, *, wall_clock_sec: float) -> str:
    """Write a well-formed output tree and return the REAL digest the Runner would record.

    Identical in every respect across calls except `wall_clock_sec`, which is what an honest
    submission reports and cannot hold constant.
    """
    out = root / "out"
    out.mkdir(parents=True)
    frame = pd.DataFrame(
        {
            "t_ns": [1, 2, 3],
            "msg_type": ["ORDER_SUBMITTED", "ORDER_FILLED", "QUOTE_UPDATE"],
            "order_id": [0, 1, 2],
            "agent_id": [1, 1, 2],
            "side": ["BUY", "SELL", "BUY"],
            "price": [10, 11, 12],
            "size": [1, 1, 1],
        }
    )
    frame.to_parquet(out / "trace.parquet")
    frame.to_parquet(out / "message_trace.parquet")
    (out / "events.json").write_text(
        json.dumps(
            {
                "scenario_id": "synthetic",
                "n_events": 3,
                "seed": 7,
                "trace_sha256": "0" * 64,
                "wall_clock_sec": wall_clock_sec,
                "events_per_sec": 3.0 / wall_clock_sec,
            }
        ),
        encoding="utf-8",
    )
    unit = REPO / "units" / "t3-s001-price-time-priority"
    result = materialize_tree(
        out, root / "staging", allowed_paths=allowed_paths_for(unit)
    )
    assert not result.rejections, result.rejections
    return result.tree_digest()


@pytest.mark.xfail(
    strict=True,
    reason=(
        "track3-simulation-public#5 / Agenthon2026#116. The repeat check compares a byte digest "
        "of the WHOLE output tree, events.json is inside that tree, and events.json must carry a "
        "real wall_clock_sec. So an honest submission diverges on every repeat and is refused. "
        "Track 3 cannot repair this alone: C2 carries one opaque digest per repeat and the repeat "
        "key set is closed, so there is nothing to recompute a narrower digest from. When the "
        "producer digests only limits.STABLE_OUTPUT_FILES this test XPASSes and the marker goes."
    ),
)
def test_an_honest_submission_is_not_refused_for_reporting_its_real_wall_clock() -> None:
    """THE acceptance test, and the one the suite never had.

    Every other digest in this module is the placeholder `F.TREE_DIGEST`, handed to each repeat by
    the fixture, so the positive controls pass by construction regardless of what is on disk. The
    suite therefore asserted this defect was absent by assuming it. Here both digests are real,
    produced by `materialize_tree` over two trees that differ only in the timing field an honest
    submission has no choice but to vary.
    """
    with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
        scored = _honest_repeat_tree(Path(first), wall_clock_sec=1.000000)
        repeat = _honest_repeat_tree(Path(second), wall_clock_sec=1.000001)

    # One microsecond of honest measurement, and the trees no longer hash alike.
    assert scored != repeat, (
        "precondition failed: the two trees hashed identically, so this test cannot "
        "demonstrate anything"
    )

    record = F.run_record(
        n_events=3,
        tree_digest=scored,
        repeat_digests=[scored, repeat, repeat, repeat, repeat],
        repeat_events=[3] * 5,
        row_counts={"trace.parquet": 3},
    )
    timing = T.ranked_timing(record, F.plan(), reference_event_count=3)
    assert timing.rankable is True


def test_a_repeat_with_a_different_event_count_fails() -> None:
    record = F.run_record(repeat_events=[N, N, N + 1, N, N])
    exc = _expect(ParticipantFailure, T.ranked_timing, record, F.plan(), reference_event_count=N)
    assert "different event count" in str(exc)


def test_the_repeat_count_is_the_plan_s_commitment_not_what_was_observed() -> None:
    record = F.run_record(elapsed_per_repeat=[1.4, 1.0])  # only two repeats recorded
    exc = _expect(OrganizerFault, T.ranked_timing, record, F.plan(), reference_event_count=N)
    assert "pre-commitment" in str(exc)


def test_an_unrankable_repeat_is_an_organizer_fault() -> None:
    record = F.run_record(repeat_rankable=[True, True, False, True, True])
    exc = _expect(OrganizerFault, T.ranked_timing, record, F.plan(), reference_event_count=N)
    assert "not rankable" in str(exc)


def test_the_warm_up_repeat_is_excluded_from_the_median() -> None:
    """The warm-up is slow by construction; including it would drag the median down."""
    record = F.run_record(elapsed_per_repeat=[10.0, 1.0, 1.0, 1.0, 1.0])
    timing = T.ranked_timing(record, F.plan(), reference_event_count=N)
    assert abs(timing.elapsed_sec_median - 1.0) < 1e-9


# --------------------------------------------------------------------------- the numerator
def test_the_numerator_comes_from_c2_not_from_the_track() -> None:
    """Frozen ruling R-3: the Runner measures the parquet-footer row count. A track may not
    measure its own ranking numerator."""
    record = F.run_record(row_counts={})
    exc = _expect(OrganizerFault, T.ranked_timing, record, F.plan(), reference_event_count=N)
    assert "output_row_counts" in str(exc)


def test_a_padded_trace_is_refused_at_the_numerator() -> None:
    """Extra rows raise events/sec. Refusing them here is independent of the semantic gate that
    also refuses them, so neither is load-bearing alone."""
    record = F.run_record(n_events=2 * N)
    exc = _expect(ParticipantFailure, T.ranked_timing, record, F.plan(), reference_event_count=N)
    assert "deterministic reference" in str(exc)


def test_a_truncated_trace_is_refused_at_the_numerator() -> None:
    record = F.run_record(n_events=N // 2)
    _expect(ParticipantFailure, T.ranked_timing, record, F.plan(), reference_event_count=N)


def test_a_batch_sums_only_the_declared_subs() -> None:
    """A submission cannot enlarge the numerator by emitting extra sub-directories: the sub list
    comes from the organizer's batch.json."""
    record = F.run_record(
        row_counts={
            "sub_00/trace.parquet": 100,
            "sub_01/trace.parquet": 200,
            "sub_99/trace.parquet": 10_000_000,  # not declared
        },
        repeat_events=[300] * 5,
    )
    timing = T.ranked_timing(
        record, F.plan(), reference_event_count=300, sub_names=["sub_00", "sub_01"]
    )
    assert timing.n_events == 300


# --------------------------------------------------------------------------- malformed / faults
def test_a_non_positive_host_wall_clock_is_an_organizer_fault() -> None:
    """The clock is ours. A zero elapsed is our instrument failing, not a participant result —
    and it is precisely the value a self-reported path would have turned into an infinite rate."""
    record = F.run_record(elapsed_per_repeat=[1.4, 0.0, 1.0, 1.0, 1.0])
    exc = _expect(OrganizerFault, T.ranked_timing, record, F.plan(), reference_event_count=N)
    assert "HOST-measured" in str(exc)


def test_a_malformed_repeat_block_is_refused_by_the_contract() -> None:
    """Malformed input case: C2 parsing itself refuses a repeat array that is out of order, so a
    hand-edited record cannot reach the ranking code at all."""
    mapping = F.run_record_mapping()
    mapping["repeats"][2]["index"] = 4
    from qfbench2_common.contracts import ContractError, RunRecord

    _expect(ContractError, RunRecord.from_mapping, mapping)


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
