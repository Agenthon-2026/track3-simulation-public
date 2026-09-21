"""Ranked Track 3 timing, derived only from the trusted C2 run record.

## Executive summary (read this first)

Track 3's leaderboard number is a measurement, so the thing being measured must not supply it.
Until C2 carried host-measured timing there was no producer of a host measurement on the platform
path, and the read side fell back to the submission's own ``events_per_sec`` whenever no
measurement was present — which was always. Both halves of the fraction were then the participant's: ``n_events`` was pinned to the
emitted trace, but ``wall_clock_sec`` was compared against nothing, so an honest trace with a
shrunken clock passed every consistency check at an arbitrary rank.

C2 can carry host-measured ``timing``, per-repeat records, Runner-measured ``output_row_counts``
read from the parquet footer (frozen ruling R-3), and telemetry with GPU attribution. The candidate
stable-repeat consumer requires a matching producer; its existence does not establish that the
real host repeat launcher has run. **This module is the only source of a ranked Track 3 rate,
and it has no fallback.**
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
3. **Every repeat is validated, including discarded warmups.** The plan commits the repeat
   policy. The candidate ``t3-stable-output-v1`` protocol binds the fixed semantic file set from
   the organizer's card and batch shape in each signed C2 repeat. The scorer recomputes that
   binding from retained sanitized files. Timing sidecars may vary; traces and message ledgers
   may not. Missing evidence or a different policy is an organizer fault. Changed content or
   counts are participant failures. The final repeat must still carry the full C3 tree digest
   retained by the C2 record. This consumer requires the matching producer and coordinated release;
   it does not establish that a real repeat launcher or official timing service has been deployed.
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
import json
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from qfbench2_common.contracts import (
    ContractError,
    EvaluationPlan,
    OrganizerFault,
    ParticipantFailure,
    RunRecord,
    digest_members,
    normalize_tree_path,
    stable_output_binding,
    telemetry_admissible_for_timing,
)

from .limits import stable_repeat_policy_for

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
    "require_repeat_evidence",
    "trusted_event_count",
    "validate_repeat_sidecars",
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


def require_repeat_evidence(record: RunRecord, plan: EvaluationPlan) -> None:
    """Require candidate repeat evidence before participant gates can attribute a failure.

    The shared entrypoint verifies Ed25519 and production trust before calling the track. This
    additional check ensures a caller did not add bindings after the attested payload was made;
    it is not a replacement for verifying the signature against the organizer trust store.
    """
    measured_repeats(record, plan)
    if plan.every_repeat_must_pass is not True:
        raise OrganizerFault("official Track 3 requires every_repeat_must_pass")
    if (
        record.attestation is None
        or record.attestation.observation_verdict != "confirmed"
    ):
        raise OrganizerFault(
            "official Track 3 repeat evidence has no confirmed Runner attestation"
        )
    try:
        signed_digest = record.attestation_payload_digest()
    except ContractError:
        raise OrganizerFault(
            "official Track 3 repeat evidence has no attested payload"
        ) from None
    if signed_digest != record.attestation.signature.payload_digest:
        raise OrganizerFault(
            "official Track 3 repeat evidence is outside the attested payload"
        )
    raw_repeats = record.raw.get("repeats", [])
    if not isinstance(raw_repeats, list) or len(raw_repeats) != len(record.repeats):
        raise OrganizerFault("repeat evidence differs from the attested record")
    for index, repeat in enumerate(record.repeats):
        if "stable_output_binding" not in repeat:
            raise OrganizerFault(
                "official Track 3 requires signed stable-output evidence for every repeat; "
                "deploy the matching producer before enabling this profile"
            )
        if repeat["stable_output_binding"] != raw_repeats[index].get(
            "stable_output_binding"
        ):
            raise OrganizerFault(
                "stable-output evidence differs from the attested record"
            )
        elapsed = float(repeat["elapsed_sec"])
        if not math.isfinite(elapsed) or elapsed <= 0.0:
            raise OrganizerFault(
                "a HOST-measured repeat, including warmups, has invalid elapsed time"
            )
    if (
        record.repeats[-1]["output_tree_digest"]
        != record.bindings["sanitized_tree_digest"]
    ):
        raise OrganizerFault(
            "the final repeat does not bind the retained full C3 output tree; "
            "the producer and scorer disagree on retention"
        )


def _assert_repeats_reproduce_scored_tree(
    record: RunRecord,
    *,
    unit_dir: Path,
    output_dir: Path,
) -> None:
    """Compare the fixed organizer policy and retained semantic bytes to every signed repeat."""
    policy = stable_repeat_policy_for(unit_dir)
    try:
        binding = stable_output_binding(output_dir, **policy)
    except (ContractError, OSError):
        raise ParticipantFailure(
            "the retained output is missing or has invalid required stable artifacts"
        ) from None
    for repeat in record.repeats:
        if repeat["stable_output_binding"]["policy_digest"] != binding["policy_digest"]:
            raise OrganizerFault(
                "a repeat uses a different stable-output policy; deploy matching producer and scorer"
            )
    divergent_digest = [
        r
        for r in record.repeats
        if r["stable_output_binding"]["content_digest"] != binding["content_digest"]
    ]
    if divergent_digest:
        raise ParticipantFailure(
            f"{len(divergent_digest)} repeat(s) produced different stable output content. "
            "Every repeat, including discarded warmups, must reproduce the checked simulation."
        )


def validate_repeat_sidecars(
    unit_dir: str | Path,
    output_dir: str | Path,
    output_row_counts: Mapping[str, int],
) -> None:
    """Validate every repeat's volatile sidecars outside the timed window.

    The producer calls this for every sanitized repeat; the consumer calls it for retained output.
    Counts are Runner-read parquet-footer counts, never participant claims. This checks structure,
    scenario/seed, arithmetic and trace hashes, not the retained trace's full semantic regression.
    """
    unit, output = Path(unit_dir), Path(output_dir)
    policy = stable_repeat_policy_for(unit)
    traces = [
        name
        for name in policy["members"]
        if name.endswith("trace.parquet") and not name.endswith("message_trace.parquet")
    ]
    try:
        if traces == [TRACE_RELPATH]:
            scenarios = {TRACE_RELPATH: "scenario.json"}
        else:
            subs = json.loads((unit / "batch.json").read_text(encoding="utf-8"))["subs"]
            scenarios = {
                f"{entry['sub']}/trace.parquet": normalize_tree_path(
                    entry["scenario_file"]
                )
                for entry in subs
            }
        declared_scenarios = {}
        for trace, relative in scenarios.items():
            path = unit / relative
            if not path.resolve().is_relative_to(unit.resolve()):
                raise OrganizerFault("organizer repeat scenario escapes the unit root")
            scenario = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(scenario, dict):
                raise ValueError("scenario must be an object")
            declared_scenarios[trace] = scenario
    except OrganizerFault:
        raise
    except (OSError, ValueError, KeyError, TypeError, ContractError):
        raise OrganizerFault(
            "organizer repeat scenarios are missing or malformed"
        ) from None

    def read_object(path: Path) -> dict[str, Any]:
        try:
            if path.stat().st_size > 1024 * 1024:
                raise ValueError("oversized sidecar")
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("sidecar must be an object")
            return value
        except (OSError, ValueError, RecursionError):
            raise ParticipantFailure(
                "repeat sidecar is missing, oversized or malformed"
            ) from None

    def check_numbers(sidecar: Mapping[str, Any], field: str, count: int) -> None:
        n = sidecar.get(field)
        if isinstance(n, bool) or not isinstance(n, int) or n != count or n <= 0:
            raise ParticipantFailure(
                "repeat sidecar event count disagrees with the trace"
            )
        values = [sidecar.get("wall_clock_sec"), sidecar.get("events_per_sec")]
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in values):
            raise ParticipantFailure(
                "repeat sidecar timing fields must be finite numbers"
            )
        try:
            wall = float(sidecar["wall_clock_sec"])
            rate = float(sidecar["events_per_sec"])
        except (OverflowError, ValueError):
            raise ParticipantFailure(
                "repeat sidecar timing fields must be finite numbers"
            ) from None
        if not math.isfinite(wall) or not math.isfinite(rate) or wall <= 0 or rate < 0:
            raise ParticipantFailure(
                "repeat sidecar timing fields must be finite and positive"
            )
        computed = count / wall
        if not math.isfinite(computed) or abs(rate - computed) / computed > 0.05:
            raise ParticipantFailure(
                "repeat sidecar rate is inconsistent with its count and time"
            )

    for trace in traces:
        count = output_row_counts.get(trace)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise OrganizerFault(
                "Runner footer counts are missing for a declared repeat trace"
            )
        sidecar = read_object(output / Path(trace).parent / "events.json")
        check_numbers(sidecar, "n_events", count)
        if (
            not isinstance(sidecar.get("scenario_id"), str)
            or not sidecar["scenario_id"]
            or isinstance(sidecar.get("seed"), bool)
            or not isinstance(sidecar.get("seed"), int)
        ):
            raise ParticipantFailure(
                "repeat sidecar scenario identity or seed is malformed"
            )
        scenario = declared_scenarios[trace]
        for name in ("scenario_id", "seed"):
            if name not in sidecar or (
                name in scenario and sidecar[name] != scenario[name]
            ):
                raise ParticipantFailure(
                    "repeat sidecar does not match its organizer scenario"
                )
        try:
            actual = digest_members(output, [trace]).get(trace)
        except (OSError, ContractError):
            raise ParticipantFailure("repeat trace is unavailable or invalid") from None
        claimed = sidecar.get("trace_sha256")
        if (
            actual is None
            or not isinstance(claimed, str)
            or claimed.strip().lower() != actual.lower()
        ):
            raise ParticipantFailure(
                "repeat sidecar trace digest does not match the trace"
            )

    if traces != [TRACE_RELPATH]:
        aggregate = read_object(output / "batch_events.json")
        check_numbers(
            aggregate, "total_events", sum(output_row_counts[p] for p in traces)
        )
        rows = aggregate.get("per_scenario")
        if not isinstance(rows, list) or len(rows) != len(traces):
            raise ParticipantFailure(
                "repeat batch sidecar does not cover the declared subs"
            )
        expected = {str(Path(p).parent): output_row_counts[p] for p in traces}
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                raise ParticipantFailure("repeat batch sidecar has malformed sub rows")
            sub, count = row.get("sub"), row.get("n_events")
            if (
                not isinstance(sub, str)
                or sub not in expected
                or sub in seen
                or isinstance(count, bool)
                or not isinstance(count, int)
                or count != expected[sub]
            ):
                raise ParticipantFailure(
                    "repeat batch sidecar has inconsistent sub counts"
                )
            seen.add(sub)


def ranked_timing(
    record: RunRecord,
    plan: EvaluationPlan,
    *,
    reference_event_count: int | None = None,
    sub_names: Sequence[str] | None = None,
    unit_dir: str | Path | None = None,
    output_dir: str | Path | None = None,
) -> RankedTiming:
    """The ranked events/sec for one unit, or an exception naming whose fault it is.

    ``reference_event_count`` is the organizer's deterministic count for this unit. When supplied,
    the Runner's count must equal it exactly — the padding defence at the numerator, independent of
    the semantic gate that also refuses extra rows.
    """
    require_official_telemetry(record)
    require_exclusive_instance(record)
    require_repeat_evidence(record, plan)

    if unit_dir is None or output_dir is None:
        raise OrganizerFault(
            "official Track 3 requires organizer unit and retained output roots"
        )
    policy = stable_repeat_policy_for(unit_dir)
    traces = [name for name in policy["members"] if name.endswith("/trace.parquet")]
    declared_subs = [str(Path(name).parent) for name in traces] or None
    if sub_names is not None and list(sub_names) != declared_subs:
        raise OrganizerFault(
            "repeat event-count scope disagrees with the organizer unit shape"
        )

    # Missing participant artifacts remain participant failures even when no footer count
    # could be produced for them. A present trace with missing count is an organizer fault.
    _assert_repeats_reproduce_scored_tree(
        record, unit_dir=Path(unit_dir), output_dir=Path(output_dir)
    )
    n_events = trusted_event_count(record, sub_names=declared_subs)
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
    if any(int(r["event_count"]) != n_events for r in record.repeats):
        raise ParticipantFailure(
            "a repeat produced a different event count than the scored tree. "
            "Every repeat, including discarded warmups, must reproduce the checked simulation."
        )
    validate_repeat_sidecars(unit_dir, output_dir, record.output_row_counts)

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
