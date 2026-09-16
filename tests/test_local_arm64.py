"""Local arm64 evidence must never qualify another platform or invented semantics."""

import json
from pathlib import Path
import shutil
import sys

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import benchmark_candidates as bench  # noqa: E402
import check_exemplar_output as exemplar  # noqa: E402
import prepare_submission as preparation  # noqa: E402


def test_native_arm64_requires_explicit_matching_platform():
    node = {
        "native": True,
        "docker_cpus": 4,
        "arch": "aarch64",
        "docker_arch": "aarch64",
    }
    bench.require_native_host(node, "linux/arm64")
    with pytest.raises(ValueError, match="native linux/amd64"):
        bench.require_native_host(node)
    with pytest.raises(ValueError):
        bench.require_native_host({**node, "arch": "x86_64"}, "linux/arm64")


def test_local_larger_disk_does_not_change_amd64_contract():
    local = {
        "execution_platform": "linux/arm64",
        "caps": {**bench.CAPS, "disk_bytes": 64 * 1024**3},
    }
    bench.validate_caps(local)
    with pytest.raises(ValueError):
        bench.validate_caps({**local, "execution_platform": "linux/amd64"})
    with pytest.raises(ValueError):
        bench.validate_caps(
            {**local, "caps": {**local["caps"], "memory_bytes": 32 * 1024**3}}
        )


def test_arm_delivery_cannot_be_used_as_amd64():
    proof = {
        "image": "arm-image",
        "status": "passed",
        "scope": "delivery",
        "profile": "developer",
        "rankable": False,
        "anonymous_pull": True,
        "platform": "linux/arm64",
        "identity": preparation.delivery_identity(),
        "runs": {
            verb: {"returncode": 0, "n_events": 1, "reported_n_events": 1}
            for verb in ("simulate", "simulate-batch")
        },
    }
    preparation.validate_delivery(proof, "arm-image", "linux/arm64")
    with pytest.raises(ValueError, match="delivery verification"):
        preparation.validate_delivery(proof, "arm-image")


@pytest.fixture
def sample(tmp_path):
    unit = tmp_path / exemplar.EXEMPLAR
    unit.mkdir()
    for name in ("card.toml", "scenario.json"):
        shutil.copyfile(ROOT / "units" / exemplar.EXEMPLAR / name, unit / name)
    output = tmp_path / "output"
    output.mkdir()
    schema = pa.schema(
        [
            (c["name"], pa.type_for_alias(c["dtype"]))
            for c in json.loads(
                (ROOT / "templates/trace_column_registry.json").read_text()
            )["columns"]
        ]
    )
    data = {
        "t_ns": [1],
        "agent_id": [1],
        "msg_type": ["ORDER_ACCEPTED"],
        "side": ["BID"],
        "price": [100],
        "size": [1],
        "order_id": [1],
    }
    pq.write_table(pa.Table.from_pydict(data, schema=schema), output / "trace.parquet")
    scenario = json.loads((unit / "scenario.json").read_text())
    events = {
        "scenario_id": scenario["scenario_id"],
        "seed": scenario["seed"],
        "n_events": 1,
        "wall_clock_sec": 1.0,
        "events_per_sec": 1.0,
        "trace_sha256": bench.file_digest(output / "trace.parquet"),
    }
    (output / "events.json").write_text(json.dumps(events))
    return unit, output, events


def test_exemplar_keeps_semantics_unknown(sample):
    unit, output, _ = sample
    result = bench.gate_output(unit, output)
    assert result["admissible"] and result["rankable"] is False
    assert result["semantic_status"] == "unknown"
    assert result["gates"]["g3_domain_semantics"]["passed"] is None
    assert result["local_trace_checks"]["decoded_rows"] == 1


@pytest.mark.parametrize(
    "field,value", [("seed", -1), ("scenario_id", "wrong"), ("n_events", 2)]
)
def test_exemplar_does_not_skip_identity_or_count_checks(sample, field, value):
    unit, output, events = sample
    events[field] = value
    (output / "events.json").write_text(json.dumps(events))
    assert not bench.gate_output(unit, output)["admissible"]


def test_reference_exception_is_not_available_to_other_units(sample):
    unit, output, _ = sample
    other = unit.with_name("another-unit")
    unit.rename(other)
    with pytest.raises(ValueError, match="only to the public exemplar"):
        exemplar.verify(other, output)


def test_exemplar_reads_data_not_just_a_valid_footer(sample):
    unit, output, events = sample
    table = pq.read_table(output / "trace.parquet")
    table = table.set_column(2, "msg_type", pa.array(["INVENTED_EVENT"]))
    pq.write_table(table, output / "trace.parquet")
    events["trace_sha256"] = bench.file_digest(output / "trace.parquet")
    (output / "events.json").write_text(json.dumps(events))
    result = bench.gate_output(unit, output)
    assert not result["admissible"]
    assert result["local_trace_checks"]["reason"] == "unknown event type"


def test_holdout_platform_is_frozen_and_cannot_be_relabelled(tmp_path):
    import validate_candidate_parameters as parameters

    plan = parameters.generate(
        tmp_path / "holdouts", 123456, "reference@sha256:" + "a" * 64, "linux/arm64"
    )
    assert plan["execution_platform"] == "linux/arm64"
    result = {
        "kind": "heldout-result",
        "image": "candidate",
        "rankable": False,
        "execution_platform": "linux/arm64",
        "plan": bench.index(tmp_path / "holdouts/holdout.json"),
        "records": [],
    }
    with pytest.raises(ValueError, match="platform mismatch"):
        parameters.validate(result, "candidate")
    with pytest.raises(ValueError, match="incomplete heldout"):
        parameters.validate(result, "candidate", "linux/arm64")


@pytest.mark.parametrize("batch", [False, True])
def test_holdout_mount_path_cannot_overwrite_docker_platform(
    tmp_path, monkeypatch, batch
):
    import subprocess
    import validate_candidate_parameters as parameters
    from throughput import timer

    plan_dir = tmp_path / "holdouts"
    plan = parameters.generate(
        plan_dir, 3456, "reference@sha256:" + "a" * 64, "linux/arm64"
    )
    plan["cases"] = [next(case for case in plan["cases"] if case["batch"] is batch)]
    plan["sha256"] = bench.digest({k: v for k, v in plan.items() if k != "sha256"})
    (plan_dir / "holdout.json").write_text(json.dumps(plan))
    monkeypatch.setattr(parameters, "validation_identity", lambda: {})
    monkeypatch.setattr(bench, "identity", lambda: {})
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda *a, **kw: b'[{"Os":"linux","Architecture":"arm64"}]',
    )
    commands = []

    def launch(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 1, b"", b"fixture launch failure")

    monkeypatch.setattr(timer, "bounded_container_run", launch)
    result = parameters.run(
        plan_dir / "holdout.json", "candidate@sha256:" + "b" * 64, tmp_path / "runs", 10
    )
    assert result["status"] == "failed"
    assert len(commands) == 2  # Candidate plus independent fallback probe.
    assert all("--platform=linux/arm64" in command for command in commands)
    target = "/input/scenarios" if batch else "/input/scenario.json"
    assert target in commands[0]
