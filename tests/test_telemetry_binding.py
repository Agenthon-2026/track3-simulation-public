"""## Executive summary (read this first)

Official Track 3 timing requires signed host evidence and stable repeated file bytes.
These synthetic tests use real sanitized parquet trees whose measured sidecars vary.
They distinguish missing organizer evidence from participant divergence, including warmups.
Development signatures exercise the contract only; these are not real worker measurements.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _contract_fixtures as F
from qfbench2_common.contracts import ContractError, OrganizerFault, ParticipantFailure, RunRecord
from qfbench2_common.contracts import stable_output_binding
from qfbench2_common.contracts.fixtures import dev_trust_store
from qfbench2_common.contracts.signing import SignatureUnverifiable
from qfbench2_common.sanitize import materialize_tree
from qfbench2_track_simulation import telemetry as T
from qfbench2_track_simulation.limits import allowed_paths_for, stable_repeat_policy_for

N = 3


def case(root: Path, *, batch=False, ledger_required=True):
    unit = root / "unit"
    unit.mkdir(parents=True)
    card = ('[task]\nscenario_family = "matching-engine-semantics"\n'
            '[scoring.params]\nrequires_message_ledger = ' + str(ledger_required).lower())
    if batch:
        card += '\n[batch]\nbatch = true\n'
    (unit / "card.toml").write_text(card)
    names = ["sub_00", "sub_01"] if batch else [""]
    subs = []
    for name in names:
        scenario = unit / "scenarios" / f"{name}.json" if batch else unit / "scenario.json"
        scenario.parent.mkdir(exist_ok=True)
        scenario.write_text(json.dumps({"scenario_id": "synthetic" + name, "seed": 7}))
        if batch:
            subs.append({"sub": name, "scenario_file": f"scenarios/{name}.json", "n_events": N})
    if batch:
        (unit / "batch.json").write_text(json.dumps({"n": len(names), "subs": subs}))
    trees, tree_digests, bindings = [], [], []
    counts = {f"{name}/trace.parquet" if name else "trace.parquet": N for name in names}
    for index in range(5):
        out = root / f"raw-{index}"
        out.mkdir()
        wall = 1.0 + index * 0.000001
        for name in names:
            where = out / name
            where.mkdir(exist_ok=True)
            frame = pd.DataFrame({"t_ns": [1, 2, 3], "value": [1.0, 2.0, 3.0]})
            frame.to_parquet(where / "trace.parquet", index=False)
            if ledger_required or batch:
                frame.to_parquet(where / "message_trace.parquet", index=False)
            (where / "events.json").write_text(json.dumps({
                "scenario_id": "synthetic" + name, "seed": 7, "n_events": N,
                "wall_clock_sec": wall, "events_per_sec": N / wall,
                "trace_sha256": hashlib.sha256((where / "trace.parquet").read_bytes()).hexdigest(),
            }))
        if batch:
            (out / "batch_events.json").write_text(json.dumps({
                "total_events": N * len(names), "wall_clock_sec": wall,
                "events_per_sec": N * len(names) / wall,
                "per_scenario": [{"sub": name, "n_events": N} for name in names],
            }))
        retained = root / f"retained-{index}"
        result = materialize_tree(out, retained, allowed_paths=allowed_paths_for(unit))
        assert not result.rejections
        trees.append(retained)
        tree_digests.append(result.tree_digest())
        bindings.append(stable_output_binding(retained, **stable_repeat_policy_for(unit)))
    raw = F.run_record_mapping(
        n_events=N * len(names), row_counts=counts, repeat_digests=tree_digests,
        tree_digest=tree_digests[-1],
    )
    for repeat, binding in zip(raw["repeats"], bindings):
        repeat["stable_output_binding"] = binding
    return {"unit": unit, "out": trees[-1], "trees": trees, "raw": raw, "plan": F.plan(),
            "count": N * len(names)}


def timing(data, *, sign=True, **kwargs):
    raw = copy.deepcopy(data["raw"])
    record = F.signed_record(raw) if sign else RunRecord.from_mapping(raw)
    return T.ranked_timing(
        record, data["plan"], unit_dir=data["unit"], output_dir=data["out"],
        reference_event_count=data["count"], **kwargs,
    )


def test_an_honest_submission_is_not_refused_for_reporting_its_real_wall_clock(tmp_path):
    data = case(tmp_path)
    assert len({r["output_tree_digest"] for r in data["raw"]["repeats"]}) == 5
    assert len({r["stable_output_binding"]["content_digest"] for r in data["raw"]["repeats"]}) == 1
    record = F.signed_record(copy.deepcopy(data["raw"]))
    record.verify_attestation(dev_trust_store(), require_production_trust=False)
    got = timing(data)
    assert got.rankable and got.profile == "official"
    assert got.n_events == N and got.measured_repeats == 4
    assert got.events_per_sec == N


def test_self_report_and_planted_handoff_cannot_change_rank(tmp_path):
    data = case(tmp_path)
    (data["out"].parent / "host_metrics.json").write_text('{"events_per_sec": 990000000}')
    path = data["out"] / "events.json"
    raw = json.loads(path.read_text())
    raw.update(wall_clock_sec=0.000001, events_per_sec=N / 0.000001)
    path.write_text(json.dumps(raw))
    assert timing(data).events_per_sec == N


@pytest.mark.parametrize("over,match", [
    (None, "telemetry_absent"),
    ({"samples_taken": 4000, "samples_missed": 1040}, "coverage_fraction"),
    ({"sampling_interval_ms": 200}, "sampling_interval_ms"),
    ({"gpu_uuid": None}, "UUID"),
    ({"exclusive": False}, "not exclusive"),
    ({"contender_process_count": 3}, "contender"),
    ({"throttled": True}, "throttling"),
])
def test_unestablished_host_controls_are_organizer_faults(tmp_path, over, match):
    data = case(tmp_path)
    data["raw"]["telemetry"] = None if over is None else F.telemetry_block(**over)
    with pytest.raises(OrganizerFault, match=match):
        timing(data)


def test_missing_cgroup_attribution_is_refused_by_parser():
    with pytest.raises(ContractError):
        F.run_record(telemetry=F.telemetry_block(participant_cgroup_id=""))


@pytest.mark.parametrize("index", [0, 1, 4])
@pytest.mark.parametrize("field", ["content_digest", "event_count"])
def test_every_repeat_including_warmups_must_reproduce_scored_content(tmp_path, index, field):
    data = case(tmp_path)
    repeat = data["raw"]["repeats"][index]
    if field == "content_digest":
        repeat["stable_output_binding"][field] = F.OTHER_TREE_DIGEST
    else:
        repeat[field] += 1
    with pytest.raises(ParticipantFailure, match="Every repeat"):
        timing(data)


@pytest.mark.parametrize("filename", ["trace.parquet", "message_trace.parquet"])
def test_retained_semantic_bytes_are_recomputed(tmp_path, filename):
    data = case(tmp_path)
    (data["out"] / filename).write_bytes(b"changed semantic content")
    with pytest.raises(ParticipantFailure, match="different stable output"):
        timing(data)


@pytest.mark.parametrize("mutation", ["missing", "wrong_policy", "unsigned", "retention"])
def test_missing_or_incompatible_producer_evidence_is_not_charged_to_participant(tmp_path, mutation):
    data = case(tmp_path)
    repeat = data["raw"]["repeats"][0]
    if mutation == "missing":
        del repeat["stable_output_binding"]
    elif mutation == "wrong_policy":
        repeat["stable_output_binding"]["policy_digest"] = F.OTHER_TREE_DIGEST
    elif mutation == "retention":
        data["raw"]["bindings"]["sanitized_tree_digest"] = F.OTHER_TREE_DIGEST
    else:
        # Retain a signature made before these new bindings existed.
        old = copy.deepcopy(data["raw"])
        for r in old["repeats"]:
            del r["stable_output_binding"]
        data["raw"]["attestation"] = F.signed_record(old).raw["attestation"]
    with pytest.raises(OrganizerFault):
        timing(data, sign=mutation != "unsigned")


def test_crypto_verification_detects_binding_added_after_signature(tmp_path):
    data = case(tmp_path)
    signed = F.signed_record(copy.deepcopy(data["raw"]))
    raw = copy.deepcopy(signed.raw)
    raw["repeats"][0]["stable_output_binding"]["content_digest"] = F.OTHER_TREE_DIGEST
    with pytest.raises(SignatureUnverifiable):
        RunRecord.from_mapping(raw).verify_attestation(dev_trust_store(), require_production_trust=False)


def test_legacy_record_remains_parseable_but_cannot_rank_under_new_profile(tmp_path):
    data = case(tmp_path)
    for repeat in data["raw"]["repeats"]:
        del repeat["stable_output_binding"]
    F.signed_record(copy.deepcopy(data["raw"]))
    with pytest.raises(OrganizerFault, match="signed stable-output"):
        timing(data)


@pytest.mark.parametrize("kind", ["count", "rankability", "clock", "warmup_clock", "footer"])
def test_missing_or_invalid_host_measurements_abort(tmp_path, kind):
    data = case(tmp_path)
    if kind == "count":
        data["raw"]["repeats"].pop()
    elif kind == "rankability":
        data["raw"]["repeats"][0]["rankability"] = {"state": "unrankable", "unmet_controls": ["telemetry_absent"]}
    elif kind == "clock":
        data["raw"]["repeats"][1]["elapsed_sec"] = 0.0
    elif kind == "warmup_clock":
        data["raw"]["repeats"][0]["elapsed_sec"] = 0.0
    else:
        data["raw"]["output_row_counts"] = {}
    with pytest.raises(OrganizerFault):
        timing(data)


def test_warmup_is_validated_but_excluded_from_rate(tmp_path):
    data = case(tmp_path)
    data["raw"]["repeats"][0]["elapsed_sec"] = 1000.0
    assert timing(data).elapsed_sec_median == 1.0


@pytest.mark.parametrize("count", [N - 1, N + 1])
def test_numerator_is_bound_to_reference_not_participant_claim(tmp_path, count):
    data = case(tmp_path)
    data["raw"]["output_row_counts"]["trace.parquet"] = count
    with pytest.raises(ParticipantFailure, match="deterministic reference"):
        timing(data)


def test_batch_policy_covers_all_declared_subs_and_does_not_accept_narrowing(tmp_path):
    data = case(tmp_path, batch=True)
    data["raw"]["output_row_counts"]["sub_99/trace.parquet"] = 1_000_000
    assert timing(data).n_events == 2 * N
    with pytest.raises(OrganizerFault, match="scope"):
        timing(data, sub_names=["sub_00"])
    (data["out"] / "sub_01" / "trace.parquet").unlink()
    del data["raw"]["output_row_counts"]["sub_01/trace.parquet"]
    with pytest.raises(ParticipantFailure, match="required stable artifacts"):
        timing(data)


def test_optional_ledger_absence_is_allowed_but_presence_is_bound(tmp_path):
    data = case(tmp_path, ledger_required=False)
    assert timing(data).rankable
    (data["out"] / "message_trace.parquet").write_bytes(b"additional optional ledger")
    with pytest.raises(ParticipantFailure, match="stable output"):
        timing(data)


@pytest.mark.parametrize("field,value", [
    ("scenario_id", "wrong"), ("seed", -1), ("trace_sha256", "0" * 64),
    ("n_events", N + 1), ("wall_clock_sec", float("nan")),
    ("events_per_sec", float("inf")), ("wall_clock_sec", -1),
    ("events_per_sec", 900), ("n_events", True),
])
def test_every_repeat_sidecar_has_reusable_structural_validation(tmp_path, field, value):
    data = case(tmp_path, batch=True)
    first = data["trees"][0]
    sidecar = first / "sub_00" / "events.json"
    raw = json.loads(sidecar.read_text())
    raw[field] = value
    sidecar.write_text(json.dumps(raw))
    # The producer must call this on the warmup too, not only the retained last output.
    with pytest.raises(ParticipantFailure):
        T.validate_repeat_sidecars(data["unit"], first, data["raw"]["output_row_counts"])


def test_missing_or_corrupt_batch_sidecar_is_participant_failure(tmp_path):
    data = case(tmp_path, batch=True)
    (data["out"] / "batch_events.json").write_text('{"per_scenario": []}')
    with pytest.raises(ParticipantFailure):
        timing(data)


def test_missing_organizer_scenario_is_organizer_fault(tmp_path):
    data = case(tmp_path)
    (data["unit"] / "scenario.json").unlink()
    with pytest.raises(OrganizerFault, match="organizer repeat scenarios"):
        timing(data)


def test_out_of_order_repeat_is_refused_by_contract():
    raw = F.run_record_mapping()
    raw["repeats"][2]["index"] = 4
    with pytest.raises(ContractError):
        RunRecord.from_mapping(raw)


@pytest.mark.parametrize("mutation", ["missing_card", "mismatched_batch", "duplicate_sub", "escaping_sub"])
def test_malformed_organizer_policy_is_never_a_participant_fault(tmp_path, mutation):
    data = case(tmp_path, batch=True)
    if mutation == "missing_card":
        (data["unit"] / "card.toml").unlink()
    elif mutation == "mismatched_batch":
        (data["unit"] / "batch.json").unlink()
    else:
        path = data["unit"] / "batch.json"
        raw = json.loads(path.read_text())
        raw["subs"][1]["sub"] = raw["subs"][0]["sub"] if mutation == "duplicate_sub" else "../outside"
        path.write_text(json.dumps(raw))
    with pytest.raises(OrganizerFault):
        stable_repeat_policy_for(data["unit"])


def test_in_memory_binding_cannot_escape_attested_payload(tmp_path):
    data = case(tmp_path)
    record = F.signed_record(copy.deepcopy(data["raw"]))
    record.repeats[0]["stable_output_binding"]["content_digest"] = F.OTHER_TREE_DIGEST
    with pytest.raises(OrganizerFault, match="attested record"):
        T.ranked_timing(record, data["plan"], unit_dir=data["unit"], output_dir=data["out"])
