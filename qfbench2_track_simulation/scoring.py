"""Track 3 (Market Simulation) CodaBench scoring package.

Exposes ``build_verifier(ctx)`` + ``LEADERBOARD_SORT`` for the shared track-agnostic driver
(`common/codabench/scoring_program/score.py`), which calls ``build_verifier(ctx).run(ctx)``
per unit.

## Two factories, and only one of them ranks

``build_verifier`` is the **production** factory. It requires the trusted evidence the driver now
supplies — ``ctx["run_record"]`` (C2) and ``ctx["plan"]`` (C1) — and it has **no participant-rate
fallback of any kind**. The ranked events/sec comes from
:mod:`qfbench2_track_simulation.telemetry`: host-measured wall clock, Runner-measured parquet-footer
row counts (frozen ruling R-3), telemetry meeting the frozen C7 thresholds, and every repeat
validated. Missing or inadequate evidence is an organizer fault that aborts the evaluation; it is
never a low participant score, and it is never quietly replaced by the submission's own number.

``build_developer_verifier`` is the **developer** factory, for local practice against a harness
that cannot produce trusted timing. It ranks on the local ``host_metrics.json`` measurement or, as
a last resort, the submission's self-report — and every result it emits carries
``rankable = False`` and ``profile = "developer"``. It is not reachable from the platform driver:
the driver calls ``build_verifier`` by name.

That split is the whole remediation. Previously one factory did both, and because the production
ingestion path never wrote the handoff file, the production factory took the developer branch on
every unit.

## Admissibility (shared ``HierarchicalVerifier`` gates g0..g3)

  g0 integrity   — trusted evidence: telemetry admissible, instance exclusive, plan ceiling sane
  g1 schema      — declared fields present, self-consistent, and bound to the TRUSTED scenario
                   (scenario_id, seed and a recomputed trace digest, not merely present)
  g2 cutoff/resource — closed-resource is enforced at ``docker run --network=none``
  g3 domain semantics — exact event coverage + fill sequence + ordering, stylized facts for
                   Family 5, and the message ledger wherever the CARD declares one

Admissible submissions are ranked by events/sec (``LEADERBOARD_SORT = "desc"``).
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import tomllib
from typing import Any

import pandas as pd

from qfbench2_common.contracts import OrganizerFault, ParticipantFailure
from qfbench2_common.failure_labels import FailureLabel
from qfbench2_common.scoring import stylized_facts
from qfbench2_common.verifier import Gate, GateResult, HierarchicalVerifier

from qfbench2_track_simulation import batch as _batch
from qfbench2_track_simulation import domain, host_metrics, semantics, telemetry

#: The scorer version, SHARED BY ALL FOUR TRACKS and bumped together (owner ruling 2026-09-11).
#:
#: Before this the four packages declared 2.0.0, 2.1.0, 0.1.0 and 3.0.0 -- numbers with no
#: relationship to each other, to the toolkit, or to anything a participant could see, and three of
#: the four exposed no version at all. A participant asking which scorer produced their number had
#: nothing to resolve. 3.1.0 was chosen because nothing may appear to go backwards: Track 4 was
#: already at 3.0.0, so a lower shared number would have been a downgrade for it.
#:
#: `pyproject.toml` must agree with this, and a test in this repository asserts it -- the Track 2
#: package previously said 2.1.0 there and 2.0.0 here, so even a participant who found a version
#: could not trust it.
SCORER_VERSION = "3.1.0"


LEADERBOARD_SORT = "desc"  # higher events/sec wins

#: Fallback stylized-fact ceilings if a unit card omits them (frozen Phase-D values).
_DEFAULT_CEILINGS: dict[str, float] = {
    "ks": 0.08,
    "acf_abs_l2": 0.12,
    "hill_abs": 1.5,
    "depth_js": 0.10,
}

#: card [task].scenario_family (string) -> family number.
_FAMILY_NUM: dict[str, int] = {
    "matching-engine-semantics": 1,
    "agent-mix": 2,
    "latency-profile": 3,
    "oracle-noise": 4,
    "calibration-stylized-facts": 5,
    "throughput-scale": 6,
    "exchange-protocol": 7,  # MP / GPU-LOB-Core: Layer-2 exchange responses (g3.5). Tier-A via card.
    "reactive-agent": 8,  # RA: endogenous reaction to a scheduled intervention. Tier-B + mandatory ledger.
}

#: Families whose single-market units carry a message ledger unless the card says otherwise.
#:
#: This is the DEFAULT behind the card's ``requires_message_ledger`` key, not a substitute for it.
#: The rule it replaces was "run the ledger gate when the reference directory happens to contain
#: message_trace.parquet", which means failing to ship a reference ledger silently switches the
#: JAX-resistance gate off for that unit — an invisible failure pointing the wrong way. Measured at
#: `origin/main`: every single-market public unit ships one except the throughput-scale worked
#: exemplar, so family 6 defaults to not-required and everything else defaults to required. An
#: unrecognised family defaults to REQUIRED, because the fail-closed direction is to ask for the
#: evidence.
_LEDGER_OPTIONAL_FAMILIES: frozenset[int] = frozenset({6})

_REQUIRED_EVENTS: frozenset[str] = frozenset(
    {
        "scenario_id",
        "n_events",
        "wall_clock_sec",
        "events_per_sec",
        "seed",
        "trace_sha256",
    }
)

#: Self-reported ``events_per_sec`` must equal ``n_events / wall_clock_sec`` within this relative
#: tolerance, and ``n_events`` must equal the real trace row count. These remain as CROSS-CHECKS on
#: the submission's own arithmetic — an inconsistent sidecar is a malformed output — but they no
#: longer stand in for a measurement: the ranked number does not come from this file at all.
_EPS_CONSISTENCY_TOL = 0.05

#: Message-semantics breaches carry their own shared label.
_MSG_SEMANTIC_LABEL = FailureLabel.T3_LATENCY_CAUSALITY_VIOLATION

_PROFILE_KEY = "_t3_profile"


# --------------------------------------------------------------------------- helpers
def _load_json(p: pathlib.Path) -> dict[str, Any] | None:
    return json.loads(p.read_text()) if p.exists() else None


def _load_trace(p: pathlib.Path) -> pd.DataFrame | None:
    """Read a parquet trace, or ``None`` when absent.

    Deliberately unguarded here: every call site wraps the g3 body in
    :func:`_contain_parse_errors`. A corrupt parquet used to raise ``ArrowInvalid`` straight out of
    ``build_verifier(ctx).run(ctx)``, which aborted the driver's whole unit loop on one malformed
    file — a participant-triggerable denial of service against every other submission in the run.
    """
    return pd.read_parquet(p) if p.exists() else None


def _load_reference_trace(path: pathlib.Path) -> pd.DataFrame:
    """Read an ORGANIZER parquet, turning any read failure into a reference-integrity fault.

    The broad handler in :func:`_g3_domain_semantics` classifies an unreadable parquet as a
    participant parse error, which is right for the candidate's file and wrong for ours. A corrupt
    reference must abort the evaluation, not be charged to whichever submission happened to be
    scored against it.
    """
    if not path.exists():
        raise semantics.ReferenceIncomplete(f"no reference {path.name} for this unit")
    try:
        return pd.read_parquet(path)
    except Exception as exc:  # noqa: BLE001 - pyarrow raises its own hierarchy
        raise semantics.ReferenceIncomplete(
            f"the reference {path.name} is unreadable ({type(exc).__name__})"
        ) from exc


def _sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cluster_key(unit_dir: str | pathlib.Path) -> str | None:
    """Group correlated units so the hub's bootstrap does not overstate precision.

    The shared scorer resamples whole clusters, but only if it is handed a real grouping: one
    cluster per unit makes the cluster branch mathematically identical to treating every unit as
    independent, and the published interval comes out too narrow. It keys on
    ``[provenance].data_cutoff`` by default and lets a track override with this function.

    Track 3 overrides because an as-of date is meaningless here: these are generated simulation
    scenarios, not market snapshots, and every card carries ``data_cutoff = ""``.

    ``[task].scenario_family`` is the grouping that is actually correlated. Units in a family share
    a generator, an agent mix and a semantic tier, so a submission's errors move together within
    one and the family is exactly the resampling unit the cluster bootstrap models.

    Never raises: the hub swallows an exception here and falls back to per-unit clusters, which is
    the silent degradation this function exists to remove, so a failure must be a visible ``None``
    rather than a caught error.
    """
    try:
        card_path = pathlib.Path(unit_dir) / "card.toml"
        if not card_path.exists():
            return None
        family = (
            tomllib.loads(card_path.read_text()).get("task", {}).get("scenario_family")
        )
    except (OSError, ValueError, TypeError):
        return None
    return family if isinstance(family, str) and family else None


class _CardPolicy:
    """Everything the organizer's card says about how this unit is graded."""

    __slots__ = (
        "family",
        "tier",
        "ceilings",
        "timestamp_tolerance_ns",
        "kendall_tau_floor",
        "spread_bps_tolerance",
        "requires_message_ledger",
    )

    def __init__(self, unit_dir: pathlib.Path) -> None:
        cp = unit_dir / "card.toml"
        card = tomllib.loads(cp.read_text()) if cp.exists() else {}
        self.family = _FAMILY_NUM.get(
            card.get("task", {}).get("scenario_family", ""), 0
        )
        params = card.get("scoring", {}).get("params", {})
        self.tier = params.get("semantic_tier")
        ceilings = dict(_DEFAULT_CEILINGS)
        ceilings.update(params.get("stylized_fact_ceilings", {}))
        self.ceilings = ceilings
        self.timestamp_tolerance_ns = int(
            params.get(
                "timestamp_tolerance_ns", semantics.DEFAULT_TIMESTAMP_TOLERANCE_NS
            )
        )
        self.kendall_tau_floor = float(
            params.get("kendall_tau_floor", semantics.DEFAULT_KENDALL_TAU_FLOOR)
        )
        self.spread_bps_tolerance = float(params.get("spread_bps_tolerance", 10.0))
        declared = params.get("requires_message_ledger")
        if isinstance(declared, bool):
            self.requires_message_ledger = declared
        else:
            self.requires_message_ledger = self.family not in _LEDGER_OPTIONAL_FAMILIES


def _card_policy(ctx: dict[str, Any]) -> _CardPolicy:
    policy = ctx.get("_t3_card")
    if policy is None:
        policy = _CardPolicy(pathlib.Path(ctx["unit_dir"]))
        ctx["_t3_card"] = policy
    return policy


def _profile(ctx: dict[str, Any]) -> str:
    return str(ctx.get(_PROFILE_KEY, telemetry.PROFILE_OFFICIAL))


def _reference_event_count(ctx: dict[str, Any]) -> int | None:
    """The organizer's deterministic event count for this unit, or ``None`` when undeclared."""
    unit_dir = pathlib.Path(ctx["unit_dir"])
    if ctx.get("_batch"):
        return _batch.declared_reference_event_count(_batch.load_subs(unit_dir))
    ref_events = _load_json(unit_dir / "events.json")
    if isinstance(ref_events, dict):
        value = ref_events.get("n_events")
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    ref_trace = unit_dir / "trace.parquet"
    if ref_trace.exists():
        import pyarrow.parquet as pq  # lazy: only needed when the sidecar omits the count

        return int(pq.ParquetFile(ref_trace).metadata.num_rows)
    return None


# --------------------------------------------------------------------------- gates
def _g0_integrity(ctx: dict[str, Any]) -> GateResult:
    """Trusted evidence first. Nothing about the submission is read before this passes."""
    if _profile(ctx) == telemetry.PROFILE_OFFICIAL:
        record = ctx.get("run_record")
        plan = ctx.get("plan")
        if record is None or plan is None:
            raise OrganizerFault(
                "the production Track 3 verifier requires the trusted C2 run record and the "
                "signed C1 plan in ctx. Both are supplied by the shared CodaBench driver; a "
                "context without them is a harness that cannot produce an official timing, and "
                "the developer factory (build_developer_verifier) is the supported route for it."
            )
        # A clip ceiling below an attainable honest score silently flattens the top of the board.
        # Refusing here is what stops an invented constant governing the ranking again.
        try:
            domain.assert_domain_max_covers_roster(
                plan.metric.domain_max,
                pathlib.Path(ctx["unit_dir"]).parent,
                plan.expected_handles,
            )
        except ValueError as exc:
            raise OrganizerFault(str(exc)) from exc
        telemetry.require_official_telemetry(record)
        telemetry.require_exclusive_instance(record)
    else:
        # Developer profile: the local harness handoff, read from the parent of the unit's output
        # directory. A file that exists but is corrupt fails the gate rather than falling through.
        try:
            ctx["_host_metrics"] = host_metrics.load(
                pathlib.Path(ctx["output_dir"]).parent
            )
        except ValueError as exc:
            return GateResult(
                False, FailureLabel.SCHEMA_INVALID_OUTPUT, {"host_metrics": str(exc)}
            )

    # A batch (BatchMarketSim) unit reports one aggregate batch_events.json instead of a single
    # events.json; every gate below branches on this once, cached in ctx.
    ctx["_batch"] = _batch.batch_meta(ctx["unit_dir"])
    try:
        if ctx["_batch"]:
            be = _load_json(pathlib.Path(ctx["output_dir"]) / "batch_events.json")
            if be is None:
                return GateResult(
                    False,
                    FailureLabel.SCHEMA_INVALID_OUTPUT,
                    {"reason": "missing batch_events.json"},
                )
            ctx["_batch_events"] = be
            return GateResult(True)
        ev = _load_json(pathlib.Path(ctx["output_dir"]) / "events.json")
    except (OSError, ValueError) as exc:
        return GateResult(
            False,
            FailureLabel.T3_PARSE_ERROR,
            {"reason": f"unreadable events sidecar: {type(exc).__name__}"},
        )
    if ev is None:
        return GateResult(
            False, FailureLabel.SCHEMA_INVALID_OUTPUT, {"reason": "missing events.json"}
        )
    ctx["_events"] = ev
    return GateResult(True)


def _g1_schema(ctx: dict[str, Any]) -> GateResult:
    if ctx.get("_batch"):
        # Aggregate anti-inflation: batch events_per_sec == total_events / wall_clock, each sub's
        # reported n_events == its real row count, total == their sum.
        subs = _batch.load_subs(pathlib.Path(ctx["unit_dir"]))
        try:
            ok, info = _batch.check_aggregate(
                pathlib.Path(ctx["output_dir"]), subs, tol=_EPS_CONSISTENCY_TOL
            )
        except Exception as exc:  # noqa: BLE001 - a corrupt sub parquet is one unit's failure
            return GateResult(
                False,
                FailureLabel.T3_PARSE_ERROR,
                {"reason": f"unreadable batch output: {type(exc).__name__}"},
            )
        if not ok:
            return GateResult(False, FailureLabel.SCHEMA_INVALID_OUTPUT, info)
        plausible = _developer_plausibility(
            ctx, float(ctx["_batch_events"]["events_per_sec"]), len(subs)
        )
        if not plausible.passed:
            return plausible
        return _resolve_official_timing(ctx)

    ev = ctx["_events"]
    missing = _REQUIRED_EVENTS - set(ev)
    if missing:
        return GateResult(
            False, FailureLabel.SCHEMA_INVALID_OUTPUT, {"missing": sorted(missing)}
        )
    try:
        n, wc, eps = (
            float(ev["n_events"]),
            float(ev["wall_clock_sec"]),
            float(ev["events_per_sec"]),
        )
    except (TypeError, ValueError):
        return GateResult(
            False,
            FailureLabel.SCHEMA_INVALID_OUTPUT,
            {"reason": "non-numeric events fields"},
        )
    if n <= 0 or wc <= 0 or eps < 0:
        return GateResult(
            False,
            FailureLabel.SCHEMA_INVALID_OUTPUT,
            {
                "reason": "non-positive events fields",
                "n_events": n,
                "wall_clock_sec": wc,
            },
        )
    recomputed = n / wc
    if abs(eps - recomputed) / recomputed > _EPS_CONSISTENCY_TOL:
        return GateResult(
            False,
            FailureLabel.SCHEMA_INVALID_OUTPUT,
            {
                "reason": "events_per_sec inconsistent with n_events / wall_clock_sec",
                "reported": eps,
                "recomputed": recomputed,
                "tol": _EPS_CONSISTENCY_TOL,
            },
        )

    bound = _bind_to_trusted_scenario(ctx, ev)
    if not bound.passed:
        return bound
    plausible = _developer_plausibility(ctx, eps, 1)
    if not plausible.passed:
        return plausible
    return _resolve_official_timing(ctx)


def _resolve_official_timing(ctx: dict[str, Any]) -> GateResult:
    """Resolve the ranked timing from C2 inside a GATE, so a participant defect gets a T3 label.

    :func:`telemetry.ranked_timing` distinguishes the two fault domains by exception type. Doing it
    here rather than in the scorer means a submission whose repeats disagree, or whose emitted row
    count differs from the deterministic reference, becomes an ordinary inadmissible verdict with a
    Track 3 label — while an organizer fault still propagates and aborts the evaluation.
    """
    if _profile(ctx) != telemetry.PROFILE_OFFICIAL:
        return GateResult(True)
    sub_names = None
    if ctx.get("_batch"):
        sub_names = [
            entry["sub"] for entry in _batch.load_subs(pathlib.Path(ctx["unit_dir"]))
        ]
    try:
        ctx["_t3_timing"] = telemetry.ranked_timing(
            ctx["run_record"],
            ctx["plan"],
            reference_event_count=_reference_event_count(ctx),
            sub_names=sub_names,
        )
    except ParticipantFailure as exc:
        return GateResult(
            False, FailureLabel.T3_SEMANTIC_REGRESSION, {"reason": str(exc)}
        )
    return GateResult(True)


def _bind_to_trusted_scenario(ctx: dict[str, Any], ev: dict[str, Any]) -> GateResult:
    """``scenario_id`` and ``seed`` must EQUAL the organizer's scenario; the digest must recompute.

    The three fields were previously required only to *exist*. None was compared to anything, so a
    submission could declare a different scenario, a nonsense seed and an arbitrary digest and
    still be admissible — measured end to end, that combination scored 2.2M events/sec.
    """
    unit_dir = pathlib.Path(ctx["unit_dir"])
    scenario_path = unit_dir / "scenario.json"
    if scenario_path.exists():
        try:
            scenario = json.loads(scenario_path.read_text())
        except (OSError, ValueError) as exc:
            raise OrganizerFault(
                f"the organizer scenario for {unit_dir.name!r} is unreadable: {exc}"
            ) from exc
        for field in ("scenario_id", "seed"):
            expected = scenario.get(field)
            if expected is None:
                continue
            declared = ev.get(field)
            if declared != expected:
                return GateResult(
                    False,
                    FailureLabel.SCHEMA_INVALID_OUTPUT,
                    {
                        "reason": f"events.json {field} does not match the scenario it was run on",
                        "field": field,
                    },
                )

    declared_digest = ev.get("trace_sha256")
    trace_path = pathlib.Path(ctx["output_dir"]) / "trace.parquet"
    if isinstance(declared_digest, str) and trace_path.exists():
        try:
            actual = _sha256_file(trace_path)
        except OSError as exc:
            return GateResult(
                False,
                FailureLabel.T3_PARSE_ERROR,
                {"reason": f"cannot read the emitted trace: {type(exc).__name__}"},
            )
        if actual.lower() != declared_digest.strip().lower():
            return GateResult(
                False,
                FailureLabel.SCHEMA_INVALID_OUTPUT,
                {"reason": "trace_sha256 does not match the emitted trace.parquet"},
            )
    return GateResult(True)


def _developer_plausibility(
    ctx: dict[str, Any], self_reported: float, n_markets: int
) -> GateResult:
    """The plausibility ceiling, which now applies ONLY on the developer profile.

    On the official path the rate is host-measured on both halves of the fraction, so there is no
    self-reported magnitude left to bound. Keeping the check alive on the developer profile stops a
    local practice run from reporting a physically impossible number.
    """
    if _profile(ctx) == telemetry.PROFILE_OFFICIAL:
        return GateResult(True)
    violation = host_metrics.implausible_self_report(
        ctx.get("_host_metrics"),
        pathlib.Path(ctx["unit_dir"]).name,
        self_reported,
        n_markets,
        ceiling_per_market=domain.MAX_PER_MARKET_EVENTS_PER_SEC,
    )
    if violation is not None:
        return GateResult(
            False, FailureLabel.SCHEMA_INVALID_OUTPUT, {"host_metrics": violation}
        )
    return GateResult(True)


def _g2_cutoff_resource(ctx: dict[str, Any]) -> GateResult:
    # Closed-resource is enforced at `docker run --network=none`; nothing time-based here.
    return GateResult(True)


def _g3_domain_semantics(ctx: dict[str, Any]) -> GateResult:
    """Exact semantics. Organizer-side defects raise; participant defects return a gate result."""
    try:
        return _g3_body(ctx)
    except semantics.ReferenceIncomplete as exc:
        # Organizer material. T3_REFERENCE_INTEGRITY_ERROR is in the shared ORGANIZER_FAULT_LABELS
        # set, so the driver aborts the whole evaluation instead of charging this to the
        # submission or appending a warning and carrying on.
        return GateResult(
            False,
            FailureLabel.T3_REFERENCE_INTEGRITY_ERROR,
            {"reason": str(exc)},
        )
    except semantics.NonfiniteStatistic as exc:
        # A non-finite INTERMEDIATE STATISTIC is ours (frozen C4 rule); non-finite participant
        # DATA is caught by check_numeric_sanity and is a participant failure.
        raise OrganizerFault(
            f"unit {pathlib.Path(ctx['unit_dir']).name!r}: {exc}. A non-finite intermediate "
            "statistic is an organizer fault, never a participant zero."
        ) from exc
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as exc:
        return GateResult(
            False,
            FailureLabel.T3_PARSE_ERROR,
            {
                "reason": f"unreadable or malformed candidate output: {type(exc).__name__}"
            },
        )
    except Exception as exc:  # noqa: BLE001 - pyarrow raises its own hierarchy
        return GateResult(
            False,
            FailureLabel.T3_PARSE_ERROR,
            {
                "reason": f"unreadable or malformed candidate output: {type(exc).__name__}"
            },
        )


def _g3_body(ctx: dict[str, Any]) -> GateResult:
    policy = _card_policy(ctx)
    unit_dir = pathlib.Path(ctx["unit_dir"])
    output_dir = pathlib.Path(ctx["output_dir"])

    if ctx.get("_batch"):
        # Isolation gate: every sub-scenario's candidate output must reproduce its ISOLATED
        # reference. One leaky sub fails the batch.
        ok, failures = _batch.score_isolation(
            unit_dir,
            output_dir,
            policy.family,
            policy.tier,
            policy.ceilings,
            timestamp_tolerance_ns=policy.timestamp_tolerance_ns,
            kendall_tau_floor=policy.kendall_tau_floor,
        )
        return (
            GateResult(True)
            if ok
            else GateResult(
                False,
                FailureLabel.T3_SEMANTIC_REGRESSION,
                {"batch_isolation_failures": failures[:3]},
            )
        )

    ref = _load_reference_trace(unit_dir / "trace.parquet")
    cand = _load_trace(output_dir / "trace.parquet")
    if cand is None:
        return GateResult(
            False,
            FailureLabel.T3_PARSE_ERROR,
            {"reason": "missing candidate trace.parquet"},
        )

    # Anti-inflation cross-check: the reported n_events must match the actual output rows.
    reported_n = ctx["_events"].get("n_events")
    if reported_n is not None and int(float(reported_n)) != len(cand):
        return GateResult(
            False,
            FailureLabel.SCHEMA_INVALID_OUTPUT,
            {
                "reason": "n_events does not match trace row count",
                "reported": int(float(reported_n)),
                "actual": len(cand),
            },
        )

    # (a) semantic regression (Tier A exact / Tier B statistical, per family), with the CARD's
    # tolerances rather than module defaults.
    ok, breaches = semantics.semantic_regression_pass(
        cand,
        ref,
        family=policy.family,
        tier=policy.tier,
        ceilings=policy.ceilings,
        spread_bps_tolerance=policy.spread_bps_tolerance,
        timestamp_tolerance_ns=policy.timestamp_tolerance_ns,
        kendall_tau_floor=policy.kendall_tau_floor,
    )
    if not ok:
        return GateResult(
            False, FailureLabel.T3_SEMANTIC_REGRESSION, {"breaches": breaches}
        )

    # (b) stylized-fact admissibility (Family 5 only) — SHARED math, our extracted inputs.
    if policy.family == 5:
        report = stylized_facts.stylized_fact_report(
            semantics.mid_price_series(cand).to_numpy(dtype=float),
            semantics.mid_price_series(ref).to_numpy(dtype=float),
            cand_depth_hist=semantics.depth_histogram(cand),
            ref_depth_hist=semantics.depth_histogram(ref),
        )
        adm, sf_breaches = stylized_facts.admissible(report, policy.ceilings)
        if not adm:
            return GateResult(
                False,
                FailureLabel.T3_STYLIZED_FACT_BREACH,
                {"breaches": sf_breaches, "report": report},
            )
        ctx["_sf_report"] = report

    # (c) message-level kernel semantics, MANDATORY BY CARD DECLARATION.
    #
    # This used to trigger on whether `unit_dir/message_trace.parquet` happened to exist, which
    # means an unshipped reference ledger silently disabled the JAX-resistance gate for that unit.
    # The card decides now; a card that requires a ledger and a reference that has none is an
    # organizer fault.
    if not policy.requires_message_ledger:
        return GateResult(True)

    ref_msg_path = unit_dir / "message_trace.parquet"
    if not ref_msg_path.exists():
        raise semantics.ReferenceIncomplete(
            f"unit {unit_dir.name!r} requires a message ledger (card "
            "[scoring.params].requires_message_ledger, or its family default) but ships no "
            "reference message_trace.parquet"
        )
    ref_msg = _load_reference_trace(ref_msg_path)
    cand_msg = _load_trace(output_dir / "message_trace.parquet")
    if cand_msg is None:
        return GateResult(
            False,
            _MSG_SEMANTIC_LABEL,
            {
                "reason": "submission did not emit message_trace.parquet; the message-level "
                "kernel ledger is a required output for this scenario family"
            },
        )
    ok_msg, msg_breaches = semantics.check_message_semantics(cand_msg)
    if not ok_msg:
        return GateResult(
            False, _MSG_SEMANTIC_LABEL, {"message_semantics": msg_breaches}
        )
    ok_ref, ref_breaches = semantics.check_message_reference(cand_msg, ref_msg)
    if not ok_ref:
        return GateResult(
            False, _MSG_SEMANTIC_LABEL, {"message_reference": ref_breaches}
        )

    if policy.family == 7:
        ok_proto, proto_breaches = semantics.check_protocol_fidelity(cand_msg, ref_msg)
        if not ok_proto:
            return GateResult(
                False, _MSG_SEMANTIC_LABEL, {"protocol_fidelity": proto_breaches}
            )
    return GateResult(True)


# --------------------------------------------------------------------------- scorers
def _official_score(ctx: dict[str, Any]) -> dict[str, Any]:
    """The ranked events/sec, entirely from trusted evidence, clipped into the C1 domain.

    There is no branch here that reads the submission's own ``events_per_sec``.
    """
    plan = ctx["plan"]
    timing = ctx["_t3_timing"]
    return {
        "score": plan.clip(timing.events_per_sec),
        "profile": timing.profile,
        "rankable": True,
        "measured_repeats": timing.measured_repeats,
        "n_events": timing.n_events,
        "stylized_facts": None if ctx.get("_batch") else ctx.get("_sf_report"),
    }


def _developer_score(ctx: dict[str, Any]) -> dict[str, Any]:
    """A NON-RANKABLE events/sec for local practice. Never reachable from the platform driver."""
    unit = pathlib.Path(ctx["unit_dir"]).name
    self_reported = float(
        ctx["_batch_events"]["events_per_sec"]
        if ctx.get("_batch")
        else ctx["_events"]["events_per_sec"]
    )
    score, source = host_metrics.developer_events_per_sec(
        ctx.get("_host_metrics"), unit, self_reported
    )
    return {
        "score": score,
        "profile": telemetry.PROFILE_DEVELOPER,
        "rankable": False,
        "score_source": source,
        "stylized_facts": None if ctx.get("_batch") else ctx.get("_sf_report"),
    }


# Declared at the parameter's exact type. `HierarchicalVerifier.__init__` takes
# `list[tuple[str, Gate]]`, and list is INVARIANT, so the inferred type of this literal --
# tuples of the concrete function types -- is not assignable to it however compatible the
# functions are. Annotating here is the local fix; widening the toolkit parameter to
# Sequence, which is what mypy suggests, is the better one and belongs in a hub change.
_GATES: list[tuple[str, Gate]] = [
    ("g0_integrity", _g0_integrity),
    ("g1_schema", _g1_schema),
    ("g2_cutoff_resource", _g2_cutoff_resource),
    ("g3_domain_semantics", _g3_domain_semantics),
]


def build_verifier(ctx: dict[str, Any]) -> HierarchicalVerifier:
    """The PRODUCTION factory. Requires trusted C1 + C2 evidence; no participant-rate fallback."""
    ctx[_PROFILE_KEY] = telemetry.PROFILE_OFFICIAL
    return HierarchicalVerifier(_GATES, _official_score)


def build_developer_verifier(ctx: dict[str, Any]) -> HierarchicalVerifier:
    """The DEVELOPER factory: unofficial boards and local practice. Every result is ``rankable = False``.

    Separately named on purpose (global rule 5), and there is still no flag that turns the
    production factory into this one.

    WHAT CHANGED (2026-08-24). This docstring used to end "and no path by which this one reaches
    the platform driver". That was accurate, and it was the defect. Track 3's production score IS a
    host measurement — events/sec from Runner telemetry over Runner-measured parquet row counts,
    frozen ruling R-3 — and the CodaBench Development phase has no Runner. So
    ``require_official_telemetry`` raised ``OrganizerFault`` on every unit and the entire
    Development board aborted, while the factory written for exactly that situation sat
    unreachable. Measured against a Development-shaped C2 (``telemetry: None``, ``repeats: []``):

        build_verifier            OrganizerFault: no admissible ranked-timing telemetry
                                  (['telemetry_absent'])
        build_developer_verifier  admissible=True score=8.0 profile='developer'
                                  rankable=False score_source='self_reported'

    The shared driver now selects this factory when — and only when — the C1 plan verified against
    a DEVELOPMENT trust store, i.e. on a board already stamped ``rankable=false`` and
    ``trust_profile="development"``, and it records ``scorer_factory`` in ``scores.json`` so the
    two quantities are never confused. Under production trust the driver cannot reach this
    function at all; that is asserted from the driver's side by
    ``test_production_trust_never_reaches_the_developer_factory``.

    The distinction that makes this safe is the one this factory already draws: production reads a
    rate the organizer MEASURED, this reads one the participant REPORTED. They are different
    quantities, so a board built from this one is unofficial — which is what ``rankable=False``,
    ``profile="developer"`` and ``score_source="self_reported"`` have always said.
    """
    ctx[_PROFILE_KEY] = telemetry.PROFILE_DEVELOPER
    return HierarchicalVerifier(_GATES, _developer_score)


__all__ = [
    "LEADERBOARD_SORT",
    "build_developer_verifier",
    "build_verifier",
    "cluster_key",
]

# `ParticipantFailure` is re-exported for the private oracle, which classifies the repeat-divergence
# failure telemetry raises. Named here so the import is not mistaken for dead code.
_ = ParticipantFailure


def scorer_identity() -> dict:
    """The provenance block an entrypoint stamps onto its output.

    This is what a participant resolves when asking which revision scored them. It is deliberately
    small and stable: a name and a version, not a dump of internal configuration.
    """
    return {
        "scorer_package": "qfbench2_track_simulation.scoring",
        "scorer_version": SCORER_VERSION,
    }
