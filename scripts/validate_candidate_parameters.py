"""Freeze public parameter holdouts, then compare a fixed candidate with pinned ABIDES.

Cases are fixed before results are inspected. A failed set becomes regression data;
a repaired candidate needs a new holdout set. All evidence remains non-rankable.
"""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
from importlib.resources import files
from importlib.metadata import distribution, version
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import benchmark_candidates as bench  # noqa: E402


def validation_identity() -> dict:
    package = distribution("qfbench2-common")
    return {
        "sources": {
            name: bench.file_digest(ROOT / name)
            for name in (
                "scripts/validate_candidate_parameters.py",
                "qfbench2_track_simulation/semantics.py",
                "throughput/timer.py",
                "tests/fallback_probe.py",
                "units/t3-fastlob-core/scenario.json",
            )
        },
        "toolkit_version": package.version,
        "toolkit_source": json.loads(package.read_text("direct_url.json") or "null"),
        "numerical_versions": {
            name: version(name) for name in ("numpy", "pandas", "pyarrow", "scipy")
        },
    }


def variant(config: dict, seed: int, n: int) -> dict:
    import jsonschema

    result = copy.deepcopy(config)
    result["agent_mix"] = {}
    for agent in result["agent_configs"]:
        kind = agent["agent_type"]
        result["agent_mix"][kind] = result["agent_mix"].get(kind, 0) + agent["count"]
    result["seed"] = seed
    result["horizon_ns"] = min(result["horizon_ns"], 400_000_000) + n + 1
    oracle = result["oracle_config"].get("params", {})
    if "scheduled_jump" in oracle:
        oracle["scheduled_jump"]["time_ns"] = result["horizon_ns"] // 2 + 1
    if result["scenario_family"] == "exchange-protocol":
        result["exchange_config"]["stp_policy"] = [
            "cancel_newest",
            "cancel_oldest",
            "cancel_newest",
        ][n % 3]
    schema = json.loads(
        files("qfbench2_common")
        .joinpath("schemas/sim_scenario.schema.json")
        .read_text()
    )
    jsonschema.validate({"scenario": result}, {**schema, "required": ["scenario"]})
    return result


def generate(
    output: Path,
    seed: int,
    reference_image: str,
    execution_platform: str = "linux/amd64",
) -> dict:
    if "@sha256:" not in reference_image:
        raise ValueError("reference image must use a fixed digest")
    bench.execution_platform({"execution_platform": execution_platform})
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    roster = bench.roster()
    families = sorted({u["family"] for u in roster})
    cases = []
    for family in families:
        choices = [
            u
            for u in roster
            if u["family"] == family and not u["batch"] and u["unit"] != bench.EXEMPLAR
        ]
        for n in range(3):
            unit = choices[n % len(choices)]
            name = f"case-{len(cases):03d}"
            path = output / name / "scenario.json"
            original = json.loads(
                (ROOT / "units" / unit["unit"] / "scenario.json").read_text()
            )
            config = variant(original, seed + len(cases), n)
            bench.save(path, config)
            cases.append(
                {
                    "id": name,
                    "family": family,
                    "source_unit": unit["unit"],
                    "batch": False,
                    "inputs": [bench.index(path)],
                }
            )
    for unit in (u for u in roster if u["batch"]):
        name = f"case-{len(cases):03d}"
        inputs = []
        for n, original in enumerate(
            sorted((ROOT / "units" / unit["unit"] / "scenarios").glob("*.json"))
        ):
            path = output / name / "scenarios" / original.name
            bench.save(
                path,
                variant(
                    json.loads(original.read_text()),
                    seed + 1000 + len(cases) * 100 + n,
                    n,
                ),
            )
            inputs.append(bench.index(path))
        cases.append(
            {
                "id": name,
                "family": unit["family"],
                "source_unit": unit["unit"],
                "batch": True,
                "inputs": inputs,
            }
        )
    plan = {
        "kind": "heldout-plan",
        "execution_platform": execution_platform,
        "profile": "developer",
        "rankable": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "seed": seed,
        "cases": cases,
        "reference_image": reference_image,
        "coverage": [
            "STP",
            "close-boundary",
            "scheduled-jump",
            "random-latency",
            "nanosecond",
            "native-exception",
        ],
        "fallback_probe_sha256": bench.file_digest(ROOT / "tests/fallback_probe.py"),
    }
    plan["sha256"] = bench.digest(plan)
    bench.save(output / "holdout.json", plan)
    return plan


def compare(candidate: Path, reference: Path, *, require_ledger: bool = True) -> dict:
    import pandas as pd
    from qfbench2_track_simulation.semantics import (
        check_tier_a,
        check_message_semantics,
        check_message_reference,
    )

    c = pd.read_parquet(candidate / "trace.parquet")
    r = pd.read_parquet(reference / "trace.parquet")
    checks = [
        check_tier_a(c, r, timestamp_tolerance_ns=0, kendall_tau_floor=0.999),
    ]
    pairs = [("trace", c, r)]
    # Omission is an organizer-policy choice, never inferred from a missing file.
    # If the candidate emits an optional ledger, validate it in full as usual.
    ledger_checked = require_ledger or (candidate / "message_trace.parquet").exists()
    if ledger_checked:
        cm = pd.read_parquet(candidate / "message_trace.parquet")
        rm = pd.read_parquet(reference / "message_trace.parquet")
        message_check = check_message_semantics(cm)
        checks.append(message_check)
        if message_check[0]:
            checks.append(check_message_reference(cm, rm))
        pairs.append(("ledger", cm, rm))
    # Exact values/order in addition to the published admissibility checks. Allow
    # equivalent nullable integer representation, never round nanosecond timestamps.
    for name, left, right in pairs:
        try:
            pd.testing.assert_frame_equal(
                left, right, check_dtype=False, check_exact=True
            )
            checks.append((True, []))
        except AssertionError as exc:
            checks.append((False, [name + ": " + str(exc)[:1500]]))
    return {
        "passed": all(ok for ok, _ in checks),
        "ledger_checked": ledger_checked,
        "checks": [{"passed": ok, "breaches": breaches} for ok, breaches in checks],
    }


def case_requires_ledger(case: dict) -> bool:
    from qfbench2_track_simulation.scoring import _CardPolicy

    return (
        case["batch"]
        or _CardPolicy(ROOT / "units" / case["source_unit"]).requires_message_ledger
    )


def run(plan_path: Path, image: str, output: Path, timeout: float) -> dict:
    from throughput.timer import bounded_container_run

    if "@sha256:" not in image:
        raise ValueError("candidate must use a registry digest")
    plan = json.loads(plan_path.read_text())
    execution_target = bench.execution_platform(plan)
    for selected in {image, plan["reference_image"]}:
        inspected = json.loads(
            subprocess.check_output(["docker", "image", "inspect", selected])
        )[0]
        if inspected["Os"] + "/" + inspected["Architecture"] != execution_target:
            raise ValueError("heldout image architecture differs from plan")
    validator = validation_identity()
    if plan["sha256"] != bench.digest({k: v for k, v in plan.items() if k != "sha256"}):
        raise ValueError("heldout plan changed")
    if (
        bench.file_digest(ROOT / "tests/fallback_probe.py")
        != plan["fallback_probe_sha256"]
    ):
        raise ValueError("fallback probe changed")
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    results = []
    for case in plan["cases"]:
        for entry in case["inputs"]:
            bench.read_index(entry)
        source = Path(case["inputs"][0]["path"]).parent
        case_output = output / case["id"]
        case_output.mkdir()
        record = {
            "case": case["id"],
            "family": case["family"],
            "batch": case["batch"],
            "image": image,
            "reference_image": plan["reference_image"],
            "plan_sha256": plan["sha256"],
            "rankable": False,
        }
        try:
            for arm, selected in (
                ("candidate", image),
                ("abides", plan["reference_image"]),
            ):
                dest = case_output / arm
                dest.mkdir()
                target = "/input/scenarios" if case["batch"] else "/input/scenario.json"
                mount = source if case["batch"] else source / "scenario.json"
                verb = (
                    ["simulate-batch", "--batch-dir", target, "--out-dir", "/output"]
                    if case["batch"]
                    else [
                        "simulate",
                        "--config",
                        target,
                        "--out",
                        "/output/trace.parquet",
                    ]
                )
                if arm == "abides":
                    verb = [
                        "python",
                        "-m",
                        "abides_fork.simulate_batch"
                        if case["batch"]
                        else "abides_fork.simulate",
                        *verb[1:],
                    ]
                command = [
                    "docker",
                    "run",
                    "--rm",
                    f"--platform={execution_target}",
                    "--network=none",
                    "--cpus=4",
                    "--memory=16g",
                    "--cidfile",
                    str(case_output / (arm + ".cid")),
                    "-v",
                    f"{mount}:{target}:ro",
                    "-v",
                    f"{dest}:/output",
                    selected,
                    *verb,
                ]
                proc = bounded_container_run(
                    command, cidfile=case_output / (arm + ".cid"), timeout_sec=timeout
                )
                (case_output / (arm + ".stdout.log")).write_bytes(proc.stdout)
                (case_output / (arm + ".stderr.log")).write_bytes(proc.stderr)
                proc.check_returncode()
            prefixes = (
                [Path(e["path"]).stem for e in case["inputs"]]
                if case["batch"]
                else [""]
            )
            record["comparisons"] = {
                prefix: compare(
                    case_output / "candidate" / prefix,
                    case_output / "abides" / prefix,
                    require_ledger=case_requires_ledger(case),
                )
                for prefix in prefixes
            }
            record["passed"] = all(c["passed"] for c in record["comparisons"].values())
        except Exception as exc:
            record.update(
                passed=False, error={"kind": type(exc).__name__, "message": str(exc)}
            )
        record["artifacts"] = [
            bench.index(p) for p in sorted(case_output.rglob("*")) if p.is_file()
        ]
        bench.save(case_output / "result.json", record)
        results.append(bench.index(case_output / "result.json"))
        if not record["passed"]:
            # Do not consume all holdout cases after a reproducible first failure.
            break
    # Run fault injection in the frozen candidate, without source overlays.
    fallback_dir = output / "fallback"
    fallback_dir.mkdir()
    probe = ROOT / "tests/fallback_probe.py"
    cmd = [
        "docker",
        "run",
        "--rm",
        f"--platform={execution_target}",
        "--network=none",
        "--cpus=4",
        "--memory=16g",
        "--cidfile",
        str(fallback_dir / "cid"),
        "-v",
        f"{probe}:/probe.py:ro",
        "-v",
        f"{ROOT / 'units/t3-fastlob-core/scenario.json'}:/scenario.json:ro",
        image,
        "python",
        "/probe.py",
    ]
    proc = bounded_container_run(cmd, cidfile=fallback_dir / "cid", timeout_sec=timeout)
    (fallback_dir / "stdout.log").write_bytes(proc.stdout)
    (fallback_dir / "stderr.log").write_bytes(proc.stderr)
    result = {
        "kind": "heldout-result",
        "execution_platform": execution_target,
        "profile": "developer",
        "rankable": False,
        "image": image,
        "plan": bench.index(plan_path),
        "records": results,
        "identity": bench.identity(),
        "validation_identity": validator,
        "fallback": {
            "image": image,
            "plan_sha256": plan["sha256"],
            "returncode": proc.returncode,
            "logs": [
                bench.index(fallback_dir / n) for n in ("stdout.log", "stderr.log")
            ],
        },
    }
    result["status"] = (
        "passed"
        if len(results) == len(plan["cases"])
        and all(bench.read_index(r)["passed"] for r in results)
        and proc.returncode == 0
        else "failed"
    )
    bench.save(output / "heldout-result.json", result)
    return result


def validate(result: dict, image: str, execution_platform: str = "linux/amd64") -> None:
    if (
        result.get("kind") != "heldout-result"
        or result.get("image") != image
        or result.get("rankable") is not False
    ):
        raise ValueError("heldout identity mismatch")
    plan = bench.read_index(result["plan"])
    if plan["sha256"] != bench.digest({k: v for k, v in plan.items() if k != "sha256"}):
        raise ValueError("heldout plan changed")
    if (
        bench.execution_platform(plan) != execution_platform
        or bench.execution_platform(result) != execution_platform
    ):
        raise ValueError("heldout execution platform mismatch")
    for case in plan["cases"]:
        for entry in case["inputs"]:
            bench.read_index(entry)
    records = [bench.read_index(entry) for entry in result["records"]]
    if {r["case"] for r in records} != {c["id"] for c in plan["cases"]} or len(
        records
    ) != len(plan["cases"]):
        raise ValueError("incomplete heldout cases")
    if result.get("validation_identity") != validation_identity():
        raise ValueError("stale heldout validator evidence")
    if plan["fallback_probe_sha256"] != bench.file_digest(
        ROOT / "tests/fallback_probe.py"
    ):
        raise ValueError("fallback probe changed")
    cases = {case["id"]: case for case in plan["cases"]}
    for entry, record in zip(result["records"], records, strict=True):
        case = cases[record["case"]]
        prefixes = (
            {Path(e["path"]).stem for e in case["inputs"]} if case["batch"] else {""}
        )
        if (
            record.get("image") != image
            or record.get("reference_image") != plan["reference_image"]
            or record.get("plan_sha256") != plan["sha256"]
            or record.get("rankable") is not False
            or record.get("family") != case["family"]
            or record.get("batch") != case["batch"]
            or set(record.get("comparisons", {})) != prefixes
        ):
            raise ValueError("heldout record is not bound to this image/plan/case")
        case_output = Path(entry["path"]).parent
        required = {
            str(case_output / arm / prefix / name)
            for arm in ("candidate", "abides")
            for prefix in prefixes
            for name in ("trace.parquet", "message_trace.parquet")
            if arm == "abides" or name == "trace.parquet" or case_requires_ledger(case)
        } | {
            str(case_output / (arm + suffix))
            for arm in ("candidate", "abides")
            for suffix in (".stdout.log", ".stderr.log")
        }
        if not required.issubset({e["path"] for e in record.get("artifacts", [])}):
            raise ValueError("heldout raw artifacts incomplete")
    fallback = result["fallback"]
    if (
        fallback.get("image") != image
        or fallback.get("plan_sha256") != plan["sha256"]
        or {Path(e["path"]).name for e in fallback.get("logs", [])}
        != {"stdout.log", "stderr.log"}
    ):
        raise ValueError("fallback evidence is not bound to this image/plan")
    families = {u["family"] for u in bench.roster()}
    if (
        any(
            sum(c["family"] == f and not c["batch"] for c in plan["cases"]) < 3
            for f in families
        )
        or sum(c["batch"] for c in plan["cases"]) < 6
    ):
        raise ValueError("heldout coverage incomplete")
    if (
        any(
            not r["passed"]
            or not r.get("comparisons")
            or not all(c["passed"] for c in r["comparisons"].values())
            for r in records
        )
        or result["fallback"]["returncode"] != 0
    ):
        raise ValueError("heldout differential or fallback failed")
    for entry in [e for r in records for e in r["artifacts"]] + result["fallback"][
        "logs"
    ]:
        if bench.file_digest(Path(entry["path"])) != entry["sha256"]:
            raise ValueError("heldout raw evidence changed")


def reference_wheels(output: Path) -> dict:
    """Download the two pinned, universal reference-only dependencies at build time."""
    from urllib.request import urlopen
    import hashlib

    output.mkdir(parents=True, exist_ok=True)
    entries = []
    for line in (ROOT / "baselines/reference-wheels.sha256").read_text().splitlines():
        checksum, filename = line.split()
        name, version = filename.split("-")[:2]
        destination = output / filename
        if not destination.exists():
            with urlopen(
                f"https://pypi.org/pypi/{name}/{version}/json", timeout=30
            ) as response:
                metadata = json.load(response)
            url = next(w["url"] for w in metadata["urls"] if w["filename"] == filename)
            with urlopen(url, timeout=30) as response:
                payload = response.read()
            if hashlib.sha256(payload).hexdigest() != checksum:
                raise ValueError("reference wheel checksum mismatch")
            with destination.open("xb") as f:
                f.write(payload)
        if bench.file_digest(destination) != checksum:
            raise ValueError("reference wheel checksum mismatch")
        entries.append(bench.index(destination))
    return {"rankable": False, "wheels": entries}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    gen = sub.add_parser("generate")
    gen.add_argument("--seed", type=int, required=True)
    gen.add_argument("--out", type=Path, required=True)
    gen.add_argument("--reference-image", required=True)
    gen.add_argument(
        "--execution-platform",
        choices=("linux/amd64", "linux/arm64"),
        default="linux/amd64",
    )
    execute = sub.add_parser("run")
    execute.add_argument("--plan", type=Path, required=True)
    execute.add_argument("--image", required=True)
    execute.add_argument("--out", type=Path, required=True)
    execute.add_argument("--timeout", type=float, default=300)
    wheels = sub.add_parser("reference-wheels")
    wheels.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.action == "reference-wheels":
            result = reference_wheels(args.out)
        elif args.action == "generate":
            result = generate(
                args.out, args.seed, args.reference_image, args.execution_platform
            )
        else:
            result = run(args.plan, args.image, args.out, args.timeout)
        print(json.dumps(result, indent=2))
        return int(result.get("status") == "failed")
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
