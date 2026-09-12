"""Ranked Track 3 timing, derived only from the trusted C2 run record.

## Executive summary (read this first)

Track 3's leaderboard number is a measurement, so the thing being measured must not supply it.
Until C2 carried host-measured timing there was no producer of a host measurement on the platform
path, and the read side fell back to the submission's own ``events_per_sec`` whenever no
measurement was present — which was always. Both halves of the fraction were then the participant's: ``n_events`` was pinned to the
emitted trace, but ``wall_clock_sec`` was compared against nothing, so an honest trace with a
shrunken clock passed every consistency check at an arbitrary rank.

The producer now exists. C2 carries host-measured ``timing``, per-repeat records, Runner-measured
``output_row_counts`` read from the parquet footer (frozen ruling R-3), and telemetry with GPU
attribution. **This module is the only source of a ranked Track 3 rate, and it has no fallback.**
There is no environment flag, no strict mode, and no "measurement absent" branch that still returns
a number. A developer profile that ranks on a self-report exists in
:mod:`qfbench2_track_simulation.host_metrics`, is reachable only through a separately named
factory, and stamps ``rankable = False`` on everything it emits.

## What is checked before a rate exists

1. **Telemetry meets the frozen C7 thresholds** — 50 ms sampling, coverage >= 0.95, at most five
   consecutive missed samples, GPU resolved by **UUID** and attributed to the participant cgroup.
   Device-index-only telemetry is inadmissible; the contract's own parser refuses it outright.
2. **The instance was otherwise idle.** Track 3's fairness rule is "same pinned, otherwise-idle
   instance", so a shared or thermally throttled window is not a comparable measurement.
3. **Every repeat is validated, not just the last.** The plan commits ``repeats`` and
   ``warmup_discarded``; C2 must carry exactly that many repeat records, each individually
   rankable, and each measured repeat's ``output_tree_digest`` and ``event_count`` must equal the
   tree that was actually scored. That is what closes alternating fast-invalid / slow-valid repeats
   *regardless of which repeat happened to be retained*: a repeat that produced different bytes
   cannot match the digest of the tree the semantic gates read.

   **Known defect, `track3-simulation-public#5` / `Agenthon2026#116`.** The digest half of that is
   currently unsatisfiable by an honest submission. ``output_tree_digest`` is a byte hash of the
   whole ``/output`` tree; ``events.json`` is inside it and must carry a real ``wall_clock_sec``;
   so the digest changes on every repeat for reasons that have nothing to do with the simulation.
   The determinism claim holds only of :data:`limits.STABLE_OUTPUT_FILES`, and C2 gives Track 3 no
   per-file digest to compare that subset against. The repair belongs to the producer of
   ``output_tree_digest`` and is tracked in those issues; the acceptance test for it is
   ``tests/test_telemetry_binding.py::test_an_honest_submission_is_not_refused_for_reporting_its_real_wall_clock``,
   which XFAILs until it lands.
4. **The numerator is the Runner's, and it must equal the reference.** R-3 gives the row count to
   the Runner; Track 3 verifies it against the organizer's reference count. A padded trace is
   refused at the numerator as well as by the semantic gate, so extra rows can never buy rank.

## Fault attribution

Unestablished telemetry, a missing repeat record, a contended box, a non-finite intermediate
statistic: **organizer faults**, which abort the whole evaluation. The participant did not cause
them and must never be charged a zero for them.

Repeats that disagree with the scored tree: a **participant failure**. The submission's own output
was not reproducible across the repeats the protocol ran.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from qfbench2_common.contracts import (
    EvaluationPlan,
    OrganizerFault,
    ParticipantFailure,
    RunRecord,
    telemetry_admissible_for_timing,
)

__all__ = [
    "MAX_CONSECUTIVE_MISSED_SAMPLES",
    "MIN_COVERAGE_FRACTION",
    "PROFILE_DEVELOPER",
    "PROFILE_OFFICIAL",
    "SAMPLING_INTERVAL_MS",
    "RankedTiming",
    "measured_repeats",
    "ranked_timing",
    "require_exclusive_instance",
    "require_official_telemetry",
    "trusted_event_count",
]

#: Frozen C7 telemetry thresholds for a ranked Track 3 rate (02A §3).
SAMPLING_INTERVAL_MS = 50
MIN_COVERAGE_FRACTION = 0.95
MAX_CONSECUTIVE_MISSED_SAMPLES = 5

#: Provenance stamped on every score. The developer profile is never `official`.
PROFILE_OFFICIAL = "official"
PROFILE_DEVELOPER = "developer"

#: Relative path, inside the sanitized tree, of the artifact whose rows are the ranked numerator.
TRACE_RELPATH = "trace.parquet"


@dataclass(frozen=True, slots=True)
class RankedTiming:
    """The ranked events/sec and the evidence it rests on."""

    events_per_sec: float
    n_events: int
    measured_repeats: int
    elapsed_sec_median: float
    profile: str
    rankable: bool


def require_official_telemetry(record: RunRecord) -> None:
    """Refuse a run whose telemetry cannot support a ranked timing. Organizer fault when it fails.

    Delegates the thresholds to the shared contract helper rather than restating them, so Track 3
    and the Runner cannot drift on what "admissible telemetry" means.
    """
    admissible, reasons = telemetry_admissible_for_timing(
        record,
        min_coverage=MIN_COVERAGE_FRACTION,
        sampling_interval_ms=SAMPLING_INTERVAL_MS,
        max_consecutive_missed=MAX_CONSECUTIVE_MISSED_SAMPLES,
    )
    if not admissible:
        raise OrganizerFault(
            f"unit {record.unit_handle!r} has no admissible ranked-timing telemetry "
            f"({list(reasons)}). Track 3's score IS a measurement; without the measurement there "
            "is no score, and an unestablished control is never charged to the participant."
        )


def require_exclusive_instance(record: RunRecord) -> None:
    """Refuse a measurement taken while the box was shared or throttled. Organizer fault.

    The load-bearing half of Track 3's fairness rule is "otherwise idle". A rate measured next to
    another workload is not comparable with one measured alone, and publishing both on one board
    ranks the scheduler rather than the submissions.
    """
    telemetry = record.telemetry
    if telemetry is None:  # pragma: no cover - guarded above
        raise OrganizerFault(f"unit {record.unit_handle!r} carries no telemetry block")
    problems: list[str] = []
    if not telemetry["exclusive"]:
        problems.append("the instance was not exclusive")
    if telemetry["contender_process_count"] != 0:
        problems.append(
            f"{telemetry['contender_process_count']} contender process(es) on the device"
        )
    if telemetry["throttled"]:
        problems.append("the device reported thermal or clock throttling")
    if problems:
        raise OrganizerFault(
            f"unit {record.unit_handle!r} was not measured on an otherwise-idle instance "
            f"({'; '.join(problems)}). Track 3 pins the instance, not just the SKU; a contended "
            "window is an organizer fault, not a participant result."
        )


def trusted_event_count(
    record: RunRecord, *, sub_names: Sequence[str] | None = None
) -> int:
    """The ranked numerator, from the Runner's parquet-footer counts in C2 (frozen ruling R-3).

    For a single-market unit this is the row count of ``trace.parquet``. For a batch unit it is the
    sum over the declared sub-scenarios' traces — declared by the ORGANIZER's ``batch.json``, so a
    submission cannot enlarge the numerator by emitting extra directories.

    A missing count is an organizer fault: R-3 gave the measurement to the Runner, and a track that
    fell back to counting rows itself would own the numerator of its own ranking metric.
    """
    counts = record.output_row_counts
    if sub_names is None:
        wanted = [TRACE_RELPATH]
    else:
        wanted = [f"{name}/{TRACE_RELPATH}" for name in sub_names]
    total = 0
    missing: list[str] = []
    for relpath in wanted:
        if relpath not in counts:
            missing.append(relpath)
            continue
        total += int(counts[relpath])
    if missing:
        raise OrganizerFault(
            f"unit {record.unit_handle!r}: C2 output_row_counts has no entry for {missing}. The "
            "Runner measures the ranked event count from the parquet footer (R-3); Track 3 does "
            "not count its own numerator, so an absent count is missing evidence, not a zero."
        )
    return total


def measured_repeats(
    record: RunRecord, plan: EvaluationPlan
) -> tuple[Mapping[str, Any], ...]:
    """The repeats that count towards the rank, after the plan's warm-up discard.

    Refuses, as an organizer fault, a repeat array that does not match the plan: the number of
    repeats is a pre-commitment, and deriving it from what was observed is the same defect as
    deriving Track 1's attempt count from observed attempts.
    """
    expected = plan.repeats
    discard = plan.warmup_discarded
    if expected is None or discard is None:  # pragma: no cover - plan is T3
        raise OrganizerFault(
            "the C1 plan carries no repeat policy; a Track 3 plan must commit repeats and "
            "warmup_discarded"
        )
    if len(record.repeats) != expected:
        raise OrganizerFault(
            f"unit {record.unit_handle!r}: C2 carries {len(record.repeats)} repeat record(s) but "
            f"the plan commits {expected}. The repeat count is a pre-commitment and is never "
            "derived from what the harness happened to record."
        )
    for repeat in record.repeats:
        if not repeat["rankability"].is_rankable:
            raise OrganizerFault(
                f"unit {record.unit_handle!r}: repeat {repeat['index']} is not rankable "
                f"({list(repeat['rankability'].unmet_controls)}). Every repeat is validated, not "
                "only the one whose output was retained."
            )
    return tuple(record.repeats[discard:])


def _assert_repeats_reproduce_scored_tree(
    record: RunRecord, repeats: Sequence[Mapping[str, Any]], n_events: int
) -> None:
    """Every measured repeat must have produced the bytes the semantic gates actually read.

    This is the only check that reaches the repeats whose output was *not* retained, which is what
    makes alternating fast-invalid / slow-valid repeats fail regardless of which one happened to be
    last. That protection is why the digest comparison has not simply been deleted in response to
    the defect below.

    **It is not a property an honest submission satisfies for free**, which this docstring used to
    claim. Determinism given the seed holds of the parquet outputs, not of the whole tree:
    ``events.json`` is inside the hashed tree and must report a real ``wall_clock_sec``, so an
    honest submission's digest differs on every repeat and is refused here
    (`track3-simulation-public#5`, `Agenthon2026#116`). Track 3 cannot narrow the comparison
    itself, because C2 carries one opaque whole-tree digest per repeat and the repeat key set is
    closed. See :data:`qfbench2_track_simulation.limits.VOLATILE_OUTPUT_FILES` for the set that
    has to come out of the producer's digest, and
    :data:`qfbench2_track_simulation.limits.VOLATILE_EVENTS_FIELDS` for the fields inside
    ``events.json`` that move.
    """
    scored_digest = record.bindings["sanitized_tree_digest"]
    divergent_digest = [
        int(r["index"]) for r in repeats if r["output_tree_digest"] != scored_digest
    ]
    divergent_count = [
        int(r["index"]) for r in repeats if int(r["event_count"]) != n_events
    ]
    if divergent_digest or divergent_count:
        raise ParticipantFailure(
            f"unit {record.unit_handle!r}: {len(divergent_digest)} measured repeat(s) produced a "
            f"different output tree and {len(divergent_count)} produced a different event count "
            "than the tree that was scored. A scenario is deterministic given its seed, so the "
            "repeats must agree; a submission whose repeats differ has not demonstrated the "
            "measured rate on the output that was checked."
        )


def ranked_timing(
    record: RunRecord,
    plan: EvaluationPlan,
    *,
    reference_event_count: int | None = None,
    sub_names: Sequence[str] | None = None,
) -> RankedTiming:
    """The ranked events/sec for one unit, or an exception naming whose fault it is.

    ``reference_event_count`` is the organizer's deterministic count for this unit. When supplied,
    the Runner's count must equal it exactly — the padding defence at the numerator, independent of
    the semantic gate that also refuses extra rows.
    """
    require_official_telemetry(record)
    require_exclusive_instance(record)

    n_events = trusted_event_count(record, sub_names=sub_names)
    if reference_event_count is not None and n_events != reference_event_count:
        raise ParticipantFailure(
            f"unit {record.unit_handle!r}: the emitted trace has {n_events} row(s) but the "
            f"deterministic reference has {reference_event_count}. The row count is the ranked "
            "numerator, so an inexact count is refused before it can be divided by anything."
        )

    repeats = measured_repeats(record, plan)
    if not repeats:  # pragma: no cover - plan-checked
        raise OrganizerFault(
            f"unit {record.unit_handle!r}: the plan's warm-up discard leaves no measured repeat"
        )
    _assert_repeats_reproduce_scored_tree(record, repeats, n_events)

    rates: list[float] = []
    for repeat in repeats:
        elapsed = float(repeat["elapsed_sec"])
        if not math.isfinite(elapsed) or elapsed <= 0.0:
            raise OrganizerFault(
                f"unit {record.unit_handle!r}: repeat {repeat['index']} reports "
                f"elapsed_sec={elapsed!r}. The wall clock is HOST-measured, so a non-positive or "
                "non-finite one is our instrument failing, not a participant result."
            )
        rates.append(n_events / elapsed)

    rate = statistics.median(rates)
    elapsed_median = statistics.median(float(r["elapsed_sec"]) for r in repeats)
    if not math.isfinite(rate):  # pragma: no cover - unreachable
        raise OrganizerFault(
            f"unit {record.unit_handle!r}: the median rate is not finite. A non-finite "
            "intermediate STATISTIC is an organizer fault (frozen C4 rule), never a participant "
            "zero."
        )
    return RankedTiming(
        events_per_sec=rate,
        n_events=n_events,
        measured_repeats=len(repeats),
        elapsed_sec_median=elapsed_median,
        profile=PROFILE_OFFICIAL,
        rankable=True,
    )
