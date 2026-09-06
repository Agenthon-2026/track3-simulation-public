"""Track-3 semantic checks + data extraction for the CodaBench scoring gate.

Track-specific logic (NOT shared scoring math): reconstruct the mid-price / spread / depth
series from a canonical trace, and run the Tier-A (exact event coverage + fill sequence +
Kendall-tau event ordering) and Tier-B (statistical proximity) semantic-regression checks. The
stylized-fact MATH (KS / ACF / Hill / Jensen-Shannon) lives in the shared
``qfbench2_common.scoring.stylized_facts`` and is NOT reimplemented here — this module only
prepares its inputs (mid-price series + depth histograms) and runs the semantic gate.

The same module is imported by the official CodaBench gate (``scoring.py``), the batch isolation
gate (``batch.py``), the private offline oracle and the public practice harness
(``run_regression.py``), so there is exactly one implementation.

## What changed, and why the old comparison was not a comparison

Three properties were previously **not** enforced, and each was independently sufficient to let a
padded or truncated trace pass:

1. **Row-count equality was never checked** on the single-unit path. A Track 3 scenario is
   deterministic given its seed, so a faithful run emits exactly as many rows as the reference.
   Only the batch path asserted that; the single-unit path did not, and the emitted row count is
   the ranked numerator.
2. **The ordering comparison intersected the two key sets** (``common = [k for k in rp if k in
   cp]``), so candidate-only rows were not merely tolerated — they were *invisible*. A candidate
   carrying double the reference's rows scored a perfect Kendall-tau with zero breaches.
3. **The ordering comparison truncated at 50,000 events.** Anything past that index was outside
   the comparison entirely.

All three are gone. Coverage is now exact in both directions: the multiset of
``(identity key, occurrence)`` pairs must match, so a missing event and an extra event are each a
breach, and the ordering statistic runs over the whole trace. Cost is bounded because the row
counts must match first, and the reference row count is organizer-controlled.

## Fault attribution

Breaches returned in the ``(ok, breaches)`` tuple are the **participant's**. Two conditions are
*ours* and are raised instead, so a caller cannot accidentally charge them to a submission:

* :class:`ReferenceIncomplete` — the organizer's reference material cannot support the check that
  the card declares (an empty reference ledger where one is mandatory, for instance).
* :class:`NonfiniteStatistic` — an intermediate statistic came out NaN or infinite. Non-finite
  values in participant *data* are a participant failure and are caught by
  :func:`check_numeric_sanity`; a non-finite *statistic* after that is an instrument fault. The old
  code did neither: it took ``np.log`` of an unguarded price series, and a NaN KS statistic
  compared ``False`` against its ceiling, so an all-zero and an all-negative price stream both
  passed Tier B.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt
import pandas as pd
from scipy import stats

__all__ = [
    "DEFAULT_KENDALL_TAU_FLOOR",
    "DEFAULT_TIMESTAMP_TOLERANCE_NS",
    "FILL_MSG_TYPES",
    "KENDALL_KEY_COLUMNS",
    "MESSAGE_TRACE_COLUMNS",
    "MIN_CANDIDATE_LEDGER_COVERAGE",
    "TIER_A_FAMILIES",
    "NonfiniteStatistic",
    "OrganizerSemanticFault",
    "ReferenceIncomplete",
    "check_message_reference",
    "check_message_semantics",
    "check_numeric_sanity",
    "check_protocol_fidelity",
    "check_tier_a",
    "check_tier_b",
    "depth_histogram",
    "mid_price_series",
    "semantic_regression_pass",
]


class OrganizerSemanticFault(Exception):
    """A semantic check could not be performed because OUR material was inadequate."""


class ReferenceIncomplete(OrganizerSemanticFault):
    """The reference cannot support a check the card declares to be mandatory."""


class NonfiniteStatistic(OrganizerSemanticFault):
    """An intermediate statistic was NaN or infinite after participant data passed sanity."""


FILL_MSG_TYPES: frozenset[str] = frozenset({"ORDER_FILLED", "PARTIAL_FILL"})
#: Family -> semantic tier when the card does not state it explicitly. Families 7
#: (exchange-protocol) and 8 (reactive-agent) are exact-fill Tier-A families; listing
#: them here makes Tier-A the default so a tier-less card cannot silently fall back to
#: the weaker statistical Tier-B check.
TIER_A_FAMILIES: frozenset[int] = frozenset({1, 3, 6, 7, 8})
KENDALL_KEY_COLUMNS: tuple[str, ...] = (
    "order_id",
    "msg_type",
    "agent_id",
    "side",
    "price",
    "size",
)
DEFAULT_TIMESTAMP_TOLERANCE_NS: int = 1_000
DEFAULT_KENDALL_TAU_FLOOR: float = 0.999

#: Columns whose values must be finite, and (where a price or a size) strictly positive.
_NUMERIC_TRACE_COLUMNS: tuple[str, ...] = ("t_ns", "price", "size")

#: A candidate message ledger must cover at least this fraction of the reference ledger's rows.
#: Reference-RELATIVE on purpose: the previous rule was an absolute ``>= 10`` on BOTH sides, so a
#: candidate could disable the strongest asynchronous-semantics comparison simply by emitting nine
#: rows. Measured: a 5,000-row ledger with ~500x wrong latencies is rejected; the same ledger
#: truncated to nine rows passed both the self-consistency and the reference check.
MIN_CANDIDATE_LEDGER_COVERAGE: float = 0.9


# --------------------------------------------------------------------------- #
# Numeric sanity (runs before any statistic touches participant data)
# --------------------------------------------------------------------------- #
def check_numeric_sanity(
    df: pd.DataFrame, *, what: str = "trace"
) -> tuple[bool, list[str]]:
    """Finite, physical numbers in the participant's trace. Breaches are the PARTICIPANT's.

    Non-finite values in participant data are a participant failure by the frozen C4 rule, and they
    have to be refused *here* rather than downstream, because a NaN propagated into a KS statistic
    compares ``False`` against every ceiling and so makes the gate pass rather than fail. Prices
    and sizes must additionally be strictly positive: a zero-priced or negative-priced quote stream
    is not a market, and both previously passed Tier B unremarked.
    """
    breaches: list[str] = []
    missing = [c for c in _NUMERIC_TRACE_COLUMNS if c not in df.columns]
    if missing:
        return False, [
            f"{what} is missing required numeric column(s): {sorted(missing)}."
        ]
    if df.empty:
        return False, [f"{what} has no rows."]
    for column in _NUMERIC_TRACE_COLUMNS:
        values = pd.to_numeric(df[column], errors="coerce").to_numpy(dtype=float)
        n_nonfinite = int((~np.isfinite(values)).sum())
        if n_nonfinite:
            breaches.append(
                f"{what}.{column} has {n_nonfinite} non-finite value(s) (NaN or infinity)."
            )
    for column in ("price", "size"):
        values = pd.to_numeric(df[column], errors="coerce").to_numpy(dtype=float)
        finite = values[np.isfinite(values)]
        n_nonpositive = int((finite <= 0).sum())
        if n_nonpositive:
            breaches.append(
                f"{what}.{column} has {n_nonpositive} non-positive value(s); a {column} of zero or "
                "less is not physical."
            )
    return (len(breaches) == 0), breaches


# --------------------------------------------------------------------------- #
# Series extraction (feeds the shared stylized-fact math)
# --------------------------------------------------------------------------- #
def mid_price_series(df: pd.DataFrame) -> pd.Series:
    """Mid-price series from QUOTE_UPDATE bid/ask (ffill-paired); fills as fallback. (T,)"""
    quotes = df[df["msg_type"] == "QUOTE_UPDATE"]
    if len(quotes) >= 2:
        bids = quotes[quotes["side"] == "BID"].set_index("t_ns")["price"]
        asks = quotes[quotes["side"] == "ASK"].set_index("t_ns")["price"]
        combined = pd.DataFrame({"bid": bids, "ask": asks}).ffill().dropna()
        if len(combined) >= 2:
            mid = (combined["bid"].astype(float) + combined["ask"].astype(float)) / 2.0
            return mid.reset_index(drop=True)
    fills = df[df["msg_type"].isin(FILL_MSG_TYPES)]
    return fills["price"].dropna().astype(float).reset_index(drop=True)


def _spread_series(df: pd.DataFrame) -> pd.Series | None:
    quotes = df[df["msg_type"] == "QUOTE_UPDATE"]
    if len(quotes) < 2:
        return None
    bids = quotes[quotes["side"] == "BID"].set_index("t_ns")["price"]
    asks = quotes[quotes["side"] == "ASK"].set_index("t_ns")["price"]
    combined = pd.DataFrame({"bid": bids, "ask": asks}).ffill().dropna()
    return combined["ask"].astype(float) - combined["bid"].astype(float)


def depth_histogram(
    df: pd.DataFrame, n_bins: int = 20
) -> npt.NDArray[np.float64] | None:
    """Normalized histogram of QUOTE_UPDATE order sizes (top-of-book depth). (n_bins,) or None."""
    quotes = df[df["msg_type"] == "QUOTE_UPDATE"]
    sizes = quotes["size"].dropna().astype(float).to_numpy()
    if len(sizes) < 10:
        return None
    counts, _ = np.histogram(sizes, bins=n_bins, density=False)
    total = counts.sum()
    return counts.astype(float) / total if total else None


# --------------------------------------------------------------------------- #
# Tier A: exact coverage + exact fill sequence + Kendall-tau ordering
# --------------------------------------------------------------------------- #
def _canonical_key_matrix(df: pd.DataFrame) -> "np.ndarray":
    """The identity key columns rendered so that VALUE equality decides, not dtype.

    This was `df[cols].astype(str)`, and `str(100)` is `"100"` while `str(100.0)` is `"100.0"` --
    so a trace identical in every value failed 100% of its keys if a column arrived as float.
    Measured on a real unit: casting the four integer key columns to float64, changing no value,
    produced "Event coverage is not exact: 14820 reference event(s) absent ... and 14820
    candidate-only" -- a message that reads as a simulation-fidelity failure when nothing about the
    simulation is wrong.

    That is not an exotic mistake. A pandas round-trip promotes int64 to float64 the moment a NaN
    appears anywhere in the frame, which a merge, a reindex or a groupby can do on its own.

    An integral float is rendered as the integer it equals, so 100.0 keys as "100". A non-integral
    value keeps full precision via `repr`, so 100.5 still does not collide with 100 -- the check is
    normalised, not loosened.
    """
    columns = []
    for name in KENDALL_KEY_COLUMNS:
        col = df[name]
        if pd.api.types.is_float_dtype(col):
            values = col.to_numpy()
            integral = np.isfinite(values) & (values == np.floor(values))
            rendered = np.where(
                integral,
                pd.Series(values)
                .astype("object")
                .map(
                    lambda v: str(int(v))
                    if np.isfinite(v) and v == np.floor(v)
                    else repr(v)
                )
                .to_numpy(),
                pd.Series(values).astype("object").map(repr).to_numpy(),
            )
            columns.append(rendered.astype(str))
        else:
            columns.append(col.astype(str).to_numpy())
    return np.column_stack(columns)


def _file_order_positions(
    df: pd.DataFrame,
) -> dict[tuple[tuple[str, ...], int], int]:
    """Map (identity-key, per-key occurrence) -> position in the trace's row order.

    No truncation. The previous ``max_events`` window meant a candidate could reorder or replace
    everything past row 50,000 and the ordering statistic would never see it. Cost is bounded by
    the row-count equality check that runs first: the candidate cannot be larger than the
    organizer's reference.
    """
    key_matrix = _canonical_key_matrix(df)
    positions: dict[tuple[tuple[str, ...], int], int] = {}
    occ: dict[tuple[str, ...], int] = {}
    for pos, row in enumerate(key_matrix):
        key = tuple(row)
        c = occ.get(key, 0)
        occ[key] = c + 1
        positions[(key, c)] = pos
    return positions


def check_tier_a(
    candidate_df: pd.DataFrame,
    reference_df: pd.DataFrame,
    timestamp_tolerance_ns: int = DEFAULT_TIMESTAMP_TOLERANCE_NS,
    kendall_tau_floor: float = DEFAULT_KENDALL_TAU_FLOOR,
) -> tuple[bool, list[str]]:
    """Exact event coverage, exact fill sequence, and Kendall-tau over the WHOLE trace.

    ``timestamp_tolerance_ns`` and ``kendall_tau_floor`` are per-scenario card values. Both used to
    be silently ignored by the official gate — ``semantic_regression_pass`` called this function
    with no tolerance argument, and the tau floor was read from a module constant unconditionally —
    so a scenario that declared a different tolerance was graded one way in Dev and another in
    Final. They are threaded now.
    """
    breaches: list[str] = []

    # 0. Exact event count. The emitted trace must match the reference's count.
    # Extra rows raise the ranked events/sec, so tolerating them would pay for padding.
    if len(candidate_df) != len(reference_df):
        breaches.append(
            f"Event count mismatch: candidate={len(candidate_df)}, "
            f"reference={len(reference_df)}. The emitted row count must match exactly. "
            "A shared seed alone does not guarantee the reference trace: different random "
            "choices can change message delivery and market events. The check compares "
            "outputs, not RNG implementations. See the Tier-A note in README.md."
        )

    ref_fills = reference_df[reference_df["msg_type"].isin(FILL_MSG_TYPES)].reset_index(
        drop=True
    )
    cand_fills = candidate_df[
        candidate_df["msg_type"].isin(FILL_MSG_TYPES)
    ].reset_index(drop=True)

    if len(cand_fills) != len(ref_fills):
        breaches.append(
            f"Fill count mismatch: candidate={len(cand_fills)}, reference={len(ref_fills)}."
        )
        n = min(len(cand_fills), len(ref_fills))
    else:
        n = len(ref_fills)

    if n > 0:
        cs, rs = cand_fills.iloc[:n], ref_fills.iloc[:n]
        for col in ("order_id", "price", "size"):
            mm = (cs[col].to_numpy() != rs[col].to_numpy()).nonzero()[0]
            if len(mm):
                breaches.append(
                    f"Fill {col} mismatch at {len(mm)} position(s); first at index {int(mm[0])}."
                )
        ts = np.abs(
            cs["t_ns"].to_numpy().astype(np.int64)
            - rs["t_ns"].to_numpy().astype(np.int64)
        )
        v = (ts > timestamp_tolerance_ns).nonzero()[0]
        if len(v):
            breaches.append(
                f"Fill timestamp tolerance exceeded at {len(v)} position(s); max |dt_ns|={int(ts[v].max())}."
            )

    # Exact bidirectional coverage, then Kendall-tau over every matched event.
    if len(candidate_df) > 1 and len(reference_df) > 1:
        rp, cp = (
            _file_order_positions(reference_df),
            _file_order_positions(candidate_df),
        )
        n_missing = sum(1 for k in rp if k not in cp)
        n_extra = sum(1 for k in cp if k not in rp)
        if n_missing or n_extra:
            # Counts only: the keys are trace CONTENT, and on the sealed path the reference is
            # sealed material. A count says everything a participant needs and discloses nothing.
            breaches.append(
                f"Event coverage is not exact: {n_missing} reference event(s) absent from the "
                f"candidate and {n_extra} candidate-only event(s). Comparing only the events the "
                "two traces happen to share would let extra rows inflate the ranked event count "
                "while passing the ordering check."
            )
        common = [k for k in rp if k in cp]
        if len(common) >= 2:
            tau, _ = stats.kendalltau([rp[k] for k in common], [cp[k] for k in common])
            if not np.isfinite(tau):
                raise NonfiniteStatistic(
                    f"Kendall-tau over {len(common)} matched events is not finite"
                )
            if tau < kendall_tau_floor:
                breaches.append(
                    f"Kendall-tau on event sequence = {tau:.6f} < floor {kendall_tau_floor} "
                    f"({len(common)} matched events)."
                )
    return (len(breaches) == 0), breaches


# --------------------------------------------------------------------------- #
# Tier B: statistical proximity (Families 2, 4, 5)
# --------------------------------------------------------------------------- #
def check_tier_b(
    candidate_df: pd.DataFrame,
    reference_df: pd.DataFrame,
    ks_ceiling: float = 0.08,
    spread_bps_tolerance: float = 10.0,
) -> tuple[bool, list[str]]:
    """Mid-price log-return KS (single calibrated ceiling) + mean-spread proximity (bps).

    (Oracle-RMSE fidelity for Family 4 is not checked here: the oracle's fundamental path is
    not present in the candidate trace. KS + spread gate Family-4 admissibility; the sealed
    offline oracle retains the oracle-RMSE check for operator cross-validation.)
    """
    breaches: list[str] = []
    ref_mid, cand_mid = mid_price_series(reference_df), mid_price_series(candidate_df)
    if len(ref_mid) < 10 or len(cand_mid) < 10:
        return False, [
            f"Insufficient mid-price data (candidate={len(cand_mid)}, reference={len(ref_mid)})."
        ]
    # The log below is only defined on strictly positive prices. Guarding here rather than
    # letting NaN propagate is the fix for the fail-open case: `ks > ceiling` is False when `ks`
    # is NaN, so an all-zero and an all-negative price stream both used to pass.
    for name, series in (("candidate", cand_mid), ("reference", ref_mid)):
        values = series.to_numpy(dtype=float)
        if not np.all(np.isfinite(values)) or np.any(values <= 0):
            if name == "reference":
                raise ReferenceIncomplete(
                    "the reference mid-price series is not strictly positive and finite"
                )
            return False, [
                "Candidate mid-price series is not strictly positive and finite; a log-return "
                "comparison is undefined on it."
            ]

    cand_lr = np.diff(np.log(cand_mid.to_numpy(dtype=float)))
    ref_lr = np.diff(np.log(ref_mid.to_numpy(dtype=float)))
    ks = float(stats.ks_2samp(cand_lr, ref_lr).statistic)
    if not np.isfinite(ks):
        raise NonfiniteStatistic("the mid-price return KS statistic is not finite")
    if ks > ks_ceiling:
        breaches.append(f"Mid-price return KS = {ks:.5f} > ceiling {ks_ceiling:.5f}.")

    rspr, cspr = _spread_series(reference_df), _spread_series(candidate_df)
    if rspr is not None and cspr is not None and len(rspr) > 0:
        level = ref_mid.mean()
        if level > 0:
            ref_bps = rspr.mean() / level * 10_000
            cand_bps = cspr.mean() / level * 10_000
            dev = abs(cand_bps - ref_bps)
            if not np.isfinite(dev):
                raise NonfiniteStatistic("the mean-spread deviation is not finite")
            if dev > spread_bps_tolerance:
                breaches.append(
                    f"Spread mean deviation = {dev:.3f} bps > tolerance {spread_bps_tolerance:.1f} bps."
                )
    return (len(breaches) == 0), breaches


# --------------------------------------------------------------------------- #
# v2 message-level kernel semantics (companion message_trace.parquet)
# --------------------------------------------------------------------------- #
MESSAGE_TRACE_COLUMNS: tuple[str, ...] = (
    "seq",
    "t_recv_ns",
    "t_send_ns",
    "latency_ns",
    "src_id",
    "dst_id",
    "message_id",
    "msg_type",
    "order_id",
    "causal_parent",
)


def check_message_semantics(msg_df: pd.DataFrame) -> tuple[bool, list[str]]:
    """Self-consistency of the message-level trace — the ABIDES-kernel invariants a valid
    accelerated simulator must preserve and a naive JAX / static-batched port breaks.

    Candidate-only (no reference needed); implementation-robust (no internal-id matching):
      0. non-empty and uniquely identified — an empty ledger demonstrates nothing, and duplicate
         ``message_id`` values make the causal lookup ambiguous;
      1. latency identity  — ``t_recv_ns - t_send_ns == latency_ns`` and ``latency_ns >= 0``
         (a port that collapses / fabricates latency asymmetry fails);
      2. processing seq     — a contiguous ``0..N-1`` permutation (the kernel delivery order);
      3. causal ordering    — a message's causal parent must be delivered to the sender BEFORE
         the child is sent (effect cannot precede cause; a port that batches/reorders in-flight
         messages fails);
      4. wakeup structure   — ``AGENT_WAKEUP`` is a zero-latency self-message with no send time.

    Property 0 is new. An empty frame previously returned ``(True, [])`` — so a static-batched port
    could emit a zero-row ledger, evade every latency, causal and wakeup check, and still win on
    throughput. That is exactly the evasion the mandatory-ledger rule exists to close.
    """
    breaches: list[str] = []
    missing = set(MESSAGE_TRACE_COLUMNS) - set(msg_df.columns)
    if missing:
        return False, [f"message_trace missing columns: {sorted(missing)}."]
    if msg_df.empty:
        return False, [
            "message_trace has no rows; an empty ledger cannot demonstrate the kernel's "
            "latency-causality semantics."
        ]

    # 0. one delivery per (message, recipient) -- the key the causal lookup below is built on.
    #
    # NOT `message_id` alone. The ledger has one row per DELIVERY, and an ABIDES broadcast is one
    # message delivered to many agents: `MarketClosePriceMsg` goes from the exchange to every
    # trader carrying a single `message_id`. Requiring that id to be unique therefore rejects an
    # honest ledger for containing a broadcast, and measured on the shipped references it rejects
    # ALL 65 of them, 20 duplicate rows on a 20-agent unit. A submission reproducing the reference
    # byte for byte was refused as inadmissible with `t3.latency_causality_violation`.
    #
    # The pair is the right invariant on its own terms: `recv_by_key` below is keyed on
    # `(message_id, dst_id)`, so the pair is what has to be unique for that dictionary to be
    # well defined, and uniqueness of the id alone was always stricter than the thing it protects.
    # Measured on the same 65 references: zero duplicates on the pair.
    n_dupe = int(len(msg_df) - len(msg_df.drop_duplicates(["message_id", "dst_id"])))
    if n_dupe:
        breaches.append(
            f"(message_id, dst_id) is not unique: {n_dupe} duplicate row(s). The same message is "
            f"delivered to the same recipient more than once."
        )

    # 1. latency identity (sent rows carry a t_send_ns; wakeups do not).
    sent = msg_df[msg_df["t_send_ns"].notna()]
    if len(sent):
        tsend = sent["t_send_ns"].astype("int64").to_numpy()
        trecv = sent["t_recv_ns"].astype("int64").to_numpy()
        lat = sent["latency_ns"].astype("int64").to_numpy()
        n_bad = int((trecv - tsend != lat).sum())
        if n_bad:
            breaches.append(
                f"Latency identity t_recv-t_send != latency_ns at {n_bad} message(s)."
            )
        n_neg = int((lat < 0).sum())
        if n_neg:
            breaches.append(f"Negative latency at {n_neg} message(s).")

    # 2. processing seq is a contiguous 0..N-1 permutation.
    seq = msg_df["seq"].to_numpy()
    n = len(seq)
    if not np.array_equal(np.sort(seq), np.arange(n)):
        breaches.append(f"Processing seq is not a contiguous 0..{n - 1} permutation.")

    # 3. causal ordering: the parent's delivery TO THE SENDER precedes the child's send time.
    children = msg_df[msg_df["causal_parent"].notna()]
    if len(children):
        recv_by_key = {
            (int(m), int(d)): int(t)
            for m, d, t in zip(
                msg_df["message_id"], msg_df["dst_id"], msg_df["t_recv_ns"]
            )
        }
        parent_recv = np.array(
            [
                recv_by_key.get((int(p), int(s)), -1)
                for p, s in zip(children["causal_parent"], children["src_id"])
            ],
            dtype="int64",
        )
        dangling = parent_recv < 0
        n_dangling = int(dangling.sum())
        if n_dangling:
            breaches.append(
                f"Dangling causal_parent (not delivered to the sender) at {n_dangling} message(s)."
            )
        child_send = children["t_send_ns"].astype("int64").to_numpy()
        n_acausal = int(((~dangling) & (child_send < parent_recv)).sum())
        if n_acausal:
            breaches.append(
                f"Effect precedes cause (child sent before its parent was delivered) at {n_acausal} message(s)."
            )

    # 4. wakeup structure.
    wk = msg_df[msg_df["msg_type"] == "AGENT_WAKEUP"]
    if len(wk) and not (
        bool((wk["src_id"] == wk["dst_id"]).all())
        and bool((wk["latency_ns"] == 0).all())
        and bool(wk["t_send_ns"].isna().all())
    ):
        breaches.append(
            "AGENT_WAKEUP rows violate the self / zero-latency / no-send-time structure."
        )

    return (len(breaches) == 0), breaches


def check_message_reference(
    candidate_msg: pd.DataFrame,
    reference_msg: pd.DataFrame,
    latency_ks_ceiling: float = 0.08,
    wakeup_rel_tol: float = 0.05,
    min_coverage: float = MIN_CANDIDATE_LEDGER_COVERAGE,
) -> tuple[bool, list[str]]:
    """Reference-equivalence of the message-level trace — the microstructure a faithful
    accelerator must reproduce and a semantically-different (but fill-correct) port cannot.

    Implementation-robust (distributional, no internal-id matching): both properties compared are
    part of the SCENARIO SPEC, not the implementation:
      1. realized-latency distribution — the scenario fixes a latency model, so a faithful run
         reproduces its distribution; a port that collapses / perturbs latency fails a two-sample
         KS test;
      2. endogenous wakeup count — agent-scheduled, so a port that drops or fabricates wakeups
         (e.g. by batching over independent books) deviates beyond a relative tolerance.

    **The minimum-sample rule is reference-relative and can no longer disable the gate.** It used
    to be an absolute ``len >= 10`` on both sides, so truncating the candidate ledger to nine rows
    skipped the KS test entirely. The candidate must now carry at least ``min_coverage`` of the
    reference's sent-message count.

    An EMPTY reference ledger raises :class:`ReferenceIncomplete` rather than passing. Whether a
    ledger is required at all is a card declaration, decided by the caller; by the time this
    function is reached the card has already said the comparison is mandatory, so a reference that
    cannot support it is our defect and must not read as a pass.
    """
    breaches: list[str] = []
    if reference_msg.empty:
        raise ReferenceIncomplete(
            "the reference message ledger is empty, so reference-equivalence cannot be checked. "
            "Whether a ledger is required is declared by the card; an absent or empty reference "
            "for a card that requires one is an organizer fault, not a pass."
        )
    if candidate_msg.empty:
        return False, [
            "candidate message_trace is empty but the reference ships a non-empty ledger."
        ]

    # 1. realized-latency distribution (sent rows carry a latency; wakeups have latency 0).
    cand_lat = (
        candidate_msg.loc[candidate_msg["t_send_ns"].notna(), "latency_ns"]
        .astype("int64")
        .to_numpy()
    )
    ref_lat = (
        reference_msg.loc[reference_msg["t_send_ns"].notna(), "latency_ns"]
        .astype("int64")
        .to_numpy()
    )
    if len(ref_lat) == 0:
        raise ReferenceIncomplete(
            "the reference message ledger carries no sent messages, so the realized-latency "
            "distribution cannot be compared"
        )
    required = int(np.ceil(min_coverage * len(ref_lat)))
    if len(cand_lat) < required:
        breaches.append(
            f"Candidate ledger covers {len(cand_lat)} sent message(s), below the "
            f"{min_coverage:.0%} reference-relative floor of {required} "
            f"(reference has {len(ref_lat)}); truncating the ledger cannot be used to skip the "
            "latency comparison."
        )
    elif len(cand_lat) > 0:
        ks = float(stats.ks_2samp(cand_lat, ref_lat).statistic)
        if not np.isfinite(ks):
            raise NonfiniteStatistic("the realized-latency KS statistic is not finite")
        if ks > latency_ks_ceiling:
            breaches.append(
                f"Realized-latency distribution KS = {ks:.5f} > ceiling {latency_ks_ceiling:.5f}."
            )

    # 2. endogenous wakeup count.
    cand_wk = int((candidate_msg["msg_type"] == "AGENT_WAKEUP").sum())
    ref_wk = int((reference_msg["msg_type"] == "AGENT_WAKEUP").sum())
    if ref_wk > 0:
        dev = abs(cand_wk - ref_wk) / ref_wk
        if dev > wakeup_rel_tol:
            breaches.append(
                f"Wakeup count = {cand_wk} vs reference {ref_wk} (rel dev {dev:.3f} > {wakeup_rel_tol})."
            )
    return (len(breaches) == 0), breaches


#: Exchange execution-report / acknowledgement message types (Layer-2 exchange protocol). The kernel
#: ledger tags each distinctly, so g3.5 reads them straight from message_trace (no new trace schema).
_EXEC_REPORT_TYPES: frozenset[str] = frozenset(
    {
        "OrderAcceptedMsg",
        "OrderExecutedMsg",
        "OrderCancelledMsg",
        "OrderPartialCancelledMsg",
        "OrderModifiedMsg",
        "OrderReplacedMsg",
        "OrderRejectedMsg",
    }
)


def check_protocol_fidelity(
    candidate_msg: pd.DataFrame,
    reference_msg: pd.DataFrame,
    latency_ks_ceiling: float = 0.08,
    min_coverage: float = MIN_CANDIDATE_LEDGER_COVERAGE,
) -> tuple[bool, list[str]]:
    """g3.5 — exchange-protocol (Layer-2) fidelity on the execution-report subsequence.

    For the exchange-protocol family the discriminating semantics are the exchange's RESPONSES —
    order accepted / executed / cancelled / modified / replaced reports — and WHEN they are
    delivered. Two implementation-robust properties, both fixed by the scenario + reference:
      1. exec-report count-by-type equivalence — a port that ignores self-trade prevention (self-
         matches instead of cancelling) or mishandles order types produces a different mix of
         reports;
      2. exec-report realized-latency distribution — a port that returns confirmations instantly
         (ignoring the exchange's ack / pipeline delay) fails a two-sample KS on report latencies.

    A reference with no execution reports raises :class:`ReferenceIncomplete`: the caller only
    reaches this function for a family whose card declares protocol fidelity mandatory, so a
    reference that cannot express it is our defect. The latency sample floor is reference-relative
    for the same reason as in :func:`check_message_reference`.
    """
    breaches: list[str] = []
    if reference_msg.empty:
        raise ReferenceIncomplete(
            "the reference message ledger is empty, so exchange-protocol fidelity cannot be checked"
        )
    ref_er = reference_msg[reference_msg["msg_type"].isin(_EXEC_REPORT_TYPES)]
    if ref_er.empty:
        raise ReferenceIncomplete(
            "the reference emits no execution reports, so exchange-protocol fidelity cannot be "
            "checked for a family that declares it mandatory"
        )
    cand_er = candidate_msg[candidate_msg["msg_type"].isin(_EXEC_REPORT_TYPES)]
    if cand_er.empty:
        return False, [
            "candidate emitted no execution reports but the reference has them."
        ]

    # 1. exec-report count-by-type equivalence (exact; reports are deterministic given the fills).
    cand_counts = cand_er["msg_type"].value_counts().to_dict()
    ref_counts = ref_er["msg_type"].value_counts().to_dict()
    for t in sorted(set(ref_counts) | set(cand_counts)):
        c, r = int(cand_counts.get(t, 0)), int(ref_counts.get(t, 0))
        if c != r:
            breaches.append(f"Exec-report count for {t} = {c} vs reference {r}.")

    # 2. exec-report realized-latency distribution (ack / pipeline-delay timing).
    cand_lat = (
        cand_er.loc[cand_er["t_send_ns"].notna(), "latency_ns"]
        .astype("int64")
        .to_numpy()
    )
    ref_lat = (
        ref_er.loc[ref_er["t_send_ns"].notna(), "latency_ns"].astype("int64").to_numpy()
    )
    if len(ref_lat) > 0:
        required = int(np.ceil(min_coverage * len(ref_lat)))
        if len(cand_lat) < required:
            breaches.append(
                f"Candidate exec-report ledger covers {len(cand_lat)} report(s), below the "
                f"{min_coverage:.0%} reference-relative floor of {required} "
                f"(reference has {len(ref_lat)})."
            )
        else:
            ks = float(stats.ks_2samp(cand_lat, ref_lat).statistic)
            if not np.isfinite(ks):
                raise NonfiniteStatistic(
                    "the exec-report latency KS statistic is not finite"
                )
            if ks > latency_ks_ceiling:
                breaches.append(
                    f"Exec-report latency KS = {ks:.5f} > ceiling {latency_ks_ceiling:.5f}."
                )
    return (len(breaches) == 0), breaches


def semantic_regression_pass(
    candidate_df: pd.DataFrame,
    reference_df: pd.DataFrame,
    family: int,
    tier: str | None = None,
    ceilings: dict[str, float] | None = None,
    spread_bps_tolerance: float = 10.0,
    timestamp_tolerance_ns: int = DEFAULT_TIMESTAMP_TOLERANCE_NS,
    kendall_tau_floor: float = DEFAULT_KENDALL_TAU_FLOOR,
) -> tuple[bool, list[str]]:
    """Run the family-appropriate semantic check. ``tier`` overrides the family default.

    Numeric sanity runs first on BOTH sides and on every family: a non-finite or non-physical
    candidate value is a participant failure that must be refused before it can reach a statistic,
    and a non-finite reference is our fault and raises.
    """
    ref_ok, ref_breaches = check_numeric_sanity(reference_df, what="reference trace")
    if not ref_ok:
        raise ReferenceIncomplete("; ".join(ref_breaches))
    cand_ok, cand_breaches = check_numeric_sanity(candidate_df, what="candidate trace")
    if not cand_ok:
        return False, cand_breaches

    family = int(family)
    tier = (tier or ("A" if family in TIER_A_FAMILIES else "B")).upper()
    if tier == "A":
        return check_tier_a(
            candidate_df,
            reference_df,
            timestamp_tolerance_ns=timestamp_tolerance_ns,
            kendall_tau_floor=kendall_tau_floor,
        )
    ks_ceiling = float((ceilings or {}).get("ks", 0.08))
    return check_tier_b(
        candidate_df,
        reference_df,
        ks_ceiling=ks_ceiling,
        spread_bps_tolerance=spread_bps_tolerance,
    )
