"""Holdouts are schema-valid, fixed before runs, and cover the public families."""

import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import validate_candidate_parameters as parameters  # noqa: E402


def test_optional_ledger_differential_still_checks_exact_trace(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq

    candidate, reference = tmp_path / "candidate", tmp_path / "reference"
    candidate.mkdir()
    reference.mkdir()
    table = pa.table(
        {
            "t_ns": [1, 2],
            "agent_id": [1, 1],
            "msg_type": ["ORDER_FILLED", "ORDER_FILLED"],
            "side": ["BUY", "SELL"],
            "price": [100, 100],
            "size": [1, 1],
            "order_id": [1, 2],
        }
    )
    for root in (candidate, reference):
        pq.write_table(table, root / "trace.parquet")
    result = parameters.compare(candidate, reference, require_ledger=False)
    assert result["passed"] and result["ledger_checked"] is False
    with pytest.raises(FileNotFoundError):
        parameters.compare(candidate, reference, require_ledger=True)
    changed = table.set_column(4, "price", pa.array([100, 101]))
    pq.write_table(changed, candidate / "trace.parquet")
    assert (
        parameters.compare(candidate, reference, require_ledger=False)["passed"]
        is False
    )
    # An emitted optional ledger is still checked; malformed output isn't ignored.
    pq.write_table(table, candidate / "trace.parquet")
    pq.write_table(pa.table({"bad_column": [1]}), candidate / "message_trace.parquet")
    pq.write_table(pa.table({"bad_column": [1]}), reference / "message_trace.parquet")
    assert (
        parameters.compare(candidate, reference, require_ledger=False)["passed"]
        is False
    )


def test_holdouts_cover_families_and_all_batch_sources(tmp_path):
    result = parameters.generate(
        tmp_path / "heldout", 990000, "fixture@sha256:" + "a" * 64
    )
    cases = result["cases"]
    assert len(cases) == 27
    assert len({c["family"] for c in cases if not c["batch"]}) == 7
    assert len({c["source_unit"] for c in cases if c["batch"]}) == 6
    seeds = []
    for case in cases:
        for entry in case["inputs"]:
            config = parameters.bench.read_index(entry)
            seeds.append(config["seed"])
            assert config["horizon_ns"] % 1000000 != 0
            assert sum(config["agent_mix"].values()) == sum(
                a["count"] for a in config["agent_configs"]
            )
    assert len(set(seeds)) == len(seeds)
    with pytest.raises(FileExistsError):
        parameters.generate(tmp_path / "heldout", 990000, "fixture@sha256:" + "a" * 64)


def test_incomplete_holdout_cannot_qualify(tmp_path):
    result = parameters.generate(
        tmp_path / "heldout", 990000, "fixture@sha256:" + "a" * 64
    )
    evidence = {
        "kind": "heldout-result",
        "image": "candidate",
        "rankable": False,
        "plan": parameters.bench.index(tmp_path / "heldout/holdout.json"),
        "records": [],
    }
    assert result["cases"]
    with pytest.raises(ValueError, match="incomplete heldout"):
        parameters.validate(evidence, "candidate")


def test_seed_and_boundary_variant_does_not_mutate_original():
    source = json.loads((ROOT / "units/t3-fastlob-core/scenario.json").read_text())
    before = json.dumps(source, sort_keys=True)
    altered = parameters.variant(source, 123456, 1)
    assert altered["seed"] == 123456
    assert altered["horizon_ns"] != source["horizon_ns"]
    assert json.dumps(source, sort_keys=True) == before


@pytest.fixture
def bound_evidence(tmp_path):
    bench = parameters.bench
    plan_dir = tmp_path / "holdout"
    plan = parameters.generate(plan_dir, 991000, "reference@sha256:" + "a" * 64)
    records = []
    for case in plan["cases"]:
        case_output = tmp_path / "runs" / case["id"]
        prefixes = (
            [Path(e["path"]).stem for e in case["inputs"]] if case["batch"] else [""]
        )
        paths = [
            case_output / arm / prefix / name
            for arm in ("candidate", "abides")
            for prefix in prefixes
            for name in ("trace.parquet", "message_trace.parquet")
        ] + [
            case_output / (arm + suffix)
            for arm in ("candidate", "abides")
            for suffix in (".stdout.log", ".stderr.log")
        ]
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture differential artifact")
        record = {
            "case": case["id"],
            "family": case["family"],
            "batch": case["batch"],
            "image": "candidate",
            "reference_image": plan["reference_image"],
            "plan_sha256": plan["sha256"],
            "rankable": False,
            "passed": True,
            "comparisons": {prefix: {"passed": True} for prefix in prefixes},
            "artifacts": [bench.index(path) for path in paths],
        }
        result_path = case_output / "result.json"
        bench.save(result_path, record)
        records.append(bench.index(result_path))
    logs = []
    for name in ("stdout.log", "stderr.log"):
        path = tmp_path / name
        path.write_bytes(b"fixture fallback log")
        logs.append(bench.index(path))
    return {
        "kind": "heldout-result",
        "image": "candidate",
        "rankable": False,
        "validation_identity": parameters.validation_identity(),
        "plan": bench.index(plan_dir / "holdout.json"),
        "records": records,
        "fallback": {
            "image": "candidate",
            "plan_sha256": plan["sha256"],
            "returncode": 0,
            "logs": logs,
        },
    }


def test_complete_bound_differential_evidence_validates(bound_evidence):
    parameters.validate(bound_evidence, "candidate")


@pytest.mark.parametrize(
    "fault",
    [
        "relabel-image",
        "other-plan",
        "wrong-reference",
        "missing-artifacts",
        "missing-batch-sub",
        "fallback-image",
        "stale-validator",
    ],
)
def test_reused_or_incomplete_differential_evidence_refused(bound_evidence, fault):
    evidence = bound_evidence
    if fault == "relabel-image":
        evidence["image"] = "new-candidate"
    elif fault == "fallback-image":
        evidence["fallback"]["image"] = "another-candidate"
    elif fault == "stale-validator":
        evidence["validation_identity"] = {}
    else:
        entry = evidence["records"][-1 if fault == "missing-batch-sub" else 0]
        path = Path(entry["path"])
        record = parameters.bench.read_index(entry)
        if fault == "other-plan":
            record["plan_sha256"] = "another-plan"
        elif fault == "wrong-reference":
            record["reference_image"] = "another-reference"
        elif fault == "missing-artifacts":
            record["artifacts"] = []
        else:
            record["comparisons"].pop(next(iter(record["comparisons"])))
        path.write_text(json.dumps(record))
        entry.update(parameters.bench.index(path))
    with pytest.raises(ValueError, match="bound|incomplete|stale"):
        parameters.validate(evidence, evidence["image"])
