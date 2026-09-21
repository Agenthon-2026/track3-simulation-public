"""Synthetic C1/C2 builders for the Track 3 test suite. Public tests use synthetic data only.

Everything here is derived from the Hub's own golden fixtures inside
``qfbench2_common.contracts.fixtures``, so the shapes cannot drift from the frozen contract
without a test going red. Nothing is copied from a sealed tree, and every identifier is an
obviously synthetic handle.

Not a test module: the name has no ``test_`` prefix, so pytest does not collect it.
"""

from __future__ import annotations

import copy
import json
import pathlib
import tempfile
from typing import Any

import qfbench2_common.contracts as _contracts
from qfbench2_common.contracts import (
    EvaluationPlan,
    RunRecord,
    compute_roster_digest,
    digest_json,
)

_FIXTURES = pathlib.Path(_contracts.__file__).resolve().parent / "fixtures"

#: A synthetic sealed-phase handle. Matches the frozen opaque grammar `^u-[0-9a-f]{8,32}$`.
UNIT_HANDLE = "u-0123456789abcdef"

#: Digest placeholders. Any well-formed sha256 works: the tests exercise Track 3's logic, not the
#: signing path, and a trust store is deliberately not configured here.
_TREE_DIGEST = "sha256:" + "1a" * 32
_OTHER_TREE_DIGEST = "sha256:" + "2b" * 32


def _load(name: str) -> dict[str, Any]:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def plan_mapping(
    *,
    handles: list[str] | None = None,
    domain_max: float = 1e7,
    repeats: int = 5,
    warmup_discarded: int = 1,
    statistic: str = "mean",
) -> dict[str, Any]:
    """A valid Track 3 C1 body with a recomputed roster digest and payload digest."""
    raw = copy.deepcopy(_load("c1/simulation_final.expanded.json"))
    handles = list(handles or [UNIT_HANDLE])
    raw["roster"]["expected_units"] = [{"unit_handle": h} for h in handles]
    raw["roster"]["count"] = len(handles)
    raw["roster"]["digest"] = compute_roster_digest(handles)
    raw["metric"]["domain"]["max"] = float(domain_max)
    raw["metric"]["statistic"] = statistic
    raw["aggregation"]["statistic"] = statistic
    raw["repeats"] = repeats
    raw["warmup_discarded"] = warmup_discarded
    body = {k: v for k, v in raw.items() if k != "signature"}
    raw["signature"]["payload_digest"] = digest_json(body)
    return raw


def plan(**kwargs: Any) -> EvaluationPlan:
    return EvaluationPlan.from_mapping(plan_mapping(**kwargs))


def _repeat(index: int, *, elapsed: float, events: int, digest: str) -> dict[str, Any]:
    return {
        "index": index,
        "elapsed_sec": elapsed,
        "output_tree_digest": digest,
        "event_count": events,
        "rankability": {"state": "rankable", "unmet_controls": []},
    }


def run_record_mapping(
    *,
    unit_handle: str = UNIT_HANDLE,
    n_events: int = 72_061,
    elapsed_per_repeat: list[float] | None = None,
    repeat_digests: list[str] | None = None,
    repeat_events: list[int] | None = None,
    repeat_rankable: list[bool] | None = None,
    telemetry: dict[str, Any] | None = ...,  # type: ignore[assignment]
    row_counts: dict[str, int] | None = None,
    tree_digest: str = _TREE_DIGEST,
) -> dict[str, Any]:
    """A valid Track 3 C2 body: five repeats, one warm-up, host timing and admissible telemetry."""
    raw = copy.deepcopy(_load("c2_run_record.json"))
    raw["unit_handle"] = unit_handle
    raw["bindings"]["sanitized_tree_digest"] = tree_digest
    elapsed = list(elapsed_per_repeat or [1.4, 1.0, 1.0, 1.0, 1.0])
    digests = list(repeat_digests or [tree_digest] * len(elapsed))
    events = list(repeat_events or [n_events] * len(elapsed))
    rankable = list(repeat_rankable or [True] * len(elapsed))
    raw["repeats"] = []
    for index, (secs, digest, count, ok) in enumerate(zip(elapsed, digests, events, rankable)):
        entry = _repeat(index, elapsed=secs, events=count, digest=digest)
        if not ok:
            entry["rankability"] = {
                "state": "unrankable",
                "unmet_controls": ["telemetry_absent"],
            }
        raw["repeats"].append(entry)
    raw["output_row_counts"] = (
        {"trace.parquet": n_events} if row_counts is None else dict(row_counts)
    )
    if telemetry is not ...:
        raw["telemetry"] = telemetry
    return raw


def run_record(**kwargs: Any) -> RunRecord:
    return RunRecord.from_mapping(run_record_mapping(**kwargs))


def signed_record(raw: dict[str, Any]) -> RunRecord:
    """Sign a synthetic record with the public development key, never production trust."""
    from qfbench2_common.contracts.fixtures import DEV_KEY_ID, DEV_SEED
    from qfbench2_common.contracts.run_record import attestation_payload
    from qfbench2_common.contracts.signing import sign_payload

    raw["attestation"]["signature"] = sign_payload(
        attestation_payload(raw), seed=DEV_SEED, key_id=DEV_KEY_ID,
        signed_at="2026-08-21T10:05:00Z",
    ).to_mapping()
    return RunRecord.from_mapping(raw)


def bind_output(raw: dict[str, Any], unit: pathlib.Path, out: pathlib.Path) -> RunRecord:
    """Synthetic identical-repeat evidence over real sanitized file bytes."""
    from qfbench2_common.contracts import stable_output_binding
    from qfbench2_common.sanitize import materialize_tree
    from qfbench2_track_simulation.limits import allowed_paths_for, stable_repeat_policy_for

    with tempfile.TemporaryDirectory() as tmp:
        target = pathlib.Path(tmp) / "sanitized"
        result = materialize_tree(out, target, allowed_paths=allowed_paths_for(unit))
        assert not result.rejections
        raw["bindings"]["sanitized_tree_digest"] = result.tree_digest()
        binding = stable_output_binding(target, **stable_repeat_policy_for(unit))
    for repeat in raw["repeats"]:
        repeat["output_tree_digest"] = raw["bindings"]["sanitized_tree_digest"]
        repeat["stable_output_binding"] = dict(binding)
    return signed_record(raw)


def telemetry_block(**over: Any) -> dict[str, Any]:
    """The fixture's admissible telemetry block, with overrides."""
    block = copy.deepcopy(_load("c2_run_record.json")["telemetry"])
    block.update(over)
    if "samples_taken" in over or "samples_missed" in over:
        block["samples_expected"] = block["samples_taken"] + block["samples_missed"]
        block["coverage_fraction"] = block["samples_taken"] / block["samples_expected"]
    return block


OTHER_TREE_DIGEST = _OTHER_TREE_DIGEST
TREE_DIGEST = _TREE_DIGEST
