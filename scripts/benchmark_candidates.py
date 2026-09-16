"""Measure complete local candidate experiments and derive promotion from evidence.

Every result is non-rankable. A frozen plan names every unit, image, repeat and
analysis rule before execution. Missing or changed evidence never counts as a pass.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
from pathlib import Path
import platform
import subprocess
import sys
import time
import tomllib
from importlib.metadata import distribution

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import prepare_submission as preparation  # noqa: E402
from verify_candidate_output import bounded_verify as gate_output  # noqa: E402

REPEATS = 5
BOOTSTRAPS = 10000
ANALYSIS_SEED = 314159
CAPS = {
    "cpus": 4,
    "memory_bytes": 16 * 1024**3,
    "disk_bytes": 10 * 1024**3,
    "network": "none",
}
EXEMPLAR = "t3-EXAMPLE-vectorized-matching"


def execution_platform(plan: dict) -> str:
    value = plan.get("execution_platform", "linux/amd64")
    if value not in {"linux/amd64", "linux/arm64"}:
        raise ValueError("unsupported execution platform")
    return value


def validate_caps(plan: dict) -> dict:
    """The arm64 local experiment may declare a larger disk; amd64 stays frozen."""
    caps = plan["caps"]
    allowed_disks = {CAPS["disk_bytes"]}
    if execution_platform(plan) == "linux/arm64":
        allowed_disks.add(64 * 1024**3)
    if (
        caps.get("disk_bytes") not in allowed_disks
        or {**caps, "disk_bytes": CAPS["disk_bytes"]} != CAPS
    ):
        raise ValueError("unexpected resource policy")
    return caps


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def file_digest(path: Path) -> str:
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def save(path: Path, value: dict) -> None:
    payload = json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as f:
        f.write(payload)


def index(path: Path) -> dict:
    return {"path": str(path.resolve()), "sha256": file_digest(path)}


def read_index(entry: dict) -> dict:
    path = Path(entry["path"])
    if file_digest(path) != entry["sha256"]:
        raise ValueError("evidence bytes changed: " + str(path))
    return json.loads(path.read_text())


def identity() -> dict:
    package = distribution("qfbench2-common")
    # Documentation-only changes need not invalidate simulator/scorer evidence.
    paths = sorted(
        p
        for root in ("baselines", "qfbench2_track_simulation", "throughput", "scripts")
        for p in (ROOT / root).rglob("*")
        if p.suffix in {".py", ".pyx", ".patch"}
    )
    paths += [
        ROOT / "Dockerfile",
        ROOT / "baselines/Dockerfile.native-candidate",
        ROOT / "simulate",
        ROOT / "simulate-batch",
        ROOT / "templates/trace_column_registry.json",
    ]
    return {
        "source_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "code_sha256": digest(
            {str(p.relative_to(ROOT)): file_digest(p) for p in paths}
        ),
        "toolkit_version": package.version,
        "toolkit_source": json.loads(package.read_text("direct_url.json") or "null"),
        "scorer_sha256": file_digest(ROOT / "qfbench2_track_simulation/scoring.py"),
    }


def roster() -> list[dict]:
    units = []
    for path in sorted((ROOT / "units").iterdir()):
        if not path.is_dir():
            continue
        card = tomllib.loads((path / "card.toml").read_text())
        files = {
            str(p.relative_to(path)): file_digest(p)
            for p in sorted(path.rglob("*"))
            if p.is_file()
        }
        units.append(
            {
                "unit": path.name,
                "family": card["task"]["scenario_family"],
                "input_sha256": digest(files),
                "batch": (path / "batch.json").exists(),
            }
        )
    if len(units) != 72 or sum(u["batch"] for u in units) != 6:
        raise ValueError(
            "public roster must contain all 72 units including six batches"
        )
    return units


def host() -> dict:
    info = json.loads(
        subprocess.check_output(
            ["docker", "info", "--format", "{{json .}}"], timeout=30
        )
    )
    machine = Path("/etc/machine-id")
    boot = Path("/proc/sys/kernel/random/boot_id")
    return {
        "system": platform.system(),
        "arch": platform.machine(),
        "node": platform.node(),
        "machine_id": file_digest(machine) if machine.exists() else None,
        "boot_id": boot.read_text().strip() if boot.exists() else None,
        "docker_id": info["ID"],
        "docker_arch": info["Architecture"],
        "docker_cpus": info["NCPU"],
        "docker_memory": info["MemTotal"],
        "kernel": info["KernelVersion"],
        "storage": info["Driver"],
        "native": platform.system() == "Linux"
        and normalized_arch(platform.machine()) == normalized_arch(info["Architecture"])
        and normalized_arch(platform.machine()) in {"amd64", "arm64"},
    }


def select_screen_units(units: list[dict]) -> list[str]:
    """Pin short/long horizons per family, every batch and the full exemplar."""
    selected = {u["unit"] for u in units if u["batch"] or u["unit"] == EXEMPLAR}
    for family in sorted({u["family"] for u in units}):
        singles = [u for u in units if u["family"] == family and not u["batch"]]
        if not singles:
            continue

        def horizon(unit: dict) -> tuple[int, str]:
            scenario = ROOT / "units" / unit["unit"] / "scenario.json"
            return int(json.loads(scenario.read_text())["horizon_ns"]), unit["unit"]

        ordered = sorted(singles, key=horizon)
        selected.update((ordered[0]["unit"], ordered[-1]["unit"]))
    return [u["unit"] for u in units if u["unit"] in selected]


def execution_roster(plan: dict) -> list[dict]:
    if plan["purpose"] != "screen":
        return plan["roster"]
    return [u for u in plan["roster"] if u["unit"] in plan["screen_units"]]


def freeze(args: argparse.Namespace) -> dict:
    if args.purpose == "confirmation" and not args.baseline:
        raise ValueError("confirmation requires a fixed B0/current-stable image")
    for image in [args.image, args.baseline]:
        if image and (
            "@sha256:" not in image or len(image.rsplit("@sha256:", 1)[1]) != 64
        ):
            raise ValueError("only fixed image digests may enter a plan")
    target = getattr(args, "execution_platform", "linux/amd64")
    disk_gib = getattr(args, "disk_gib", 10)
    plan = {
        "schema": 1,
        "profile": "developer",
        "rankable": False,
        "execution_platform": target,
        "qualification_scope": "local-arm64" if target == "linux/arm64" else "amd64",
        "created_at": now(),
        "round_id": args.round,
        "purpose": args.purpose,
        "history_dir": str(args.history.resolve()),
        "hypothesis": args.hypothesis,
        "identity": identity(),
        "roster": roster(),
        "host": host(),
        "caps": {**CAPS, "disk_bytes": disk_gib * 1024**3},
        "images": {"A": args.baseline or args.image, "B": args.image}
        if args.baseline
        else {"A": args.image},
        "warmups": 1,
        "repeats": 1 if args.purpose == "screen" else REPEATS,
        "bootstrap_samples": BOOTSTRAPS,
        "analysis_seed": ANALYSIS_SEED,
        "resampling": "complete-paired-groups-percentile",
        "timeout_sec": args.timeout,
        "budget_sec": args.budget,
        "stop_conditions": [
            "deadline",
            "host-drift",
            "host-unavailable",
            "unit-failed",
            "three-consecutive-same-failures",
            "completed-once",
        ],
        "holdout": index(args.holdout) if args.holdout else None,
    }
    if args.purpose == "screen":
        plan["screen_units"] = select_screen_units(plan["roster"])
    validate_caps(plan)
    plan["plan_sha256"] = digest(plan)
    save(args.out, plan)
    return plan


def validate_plan(plan: dict, *, current: bool = False) -> None:
    if (
        digest({k: v for k, v in plan.items() if k != "plan_sha256"})
        != plan["plan_sha256"]
    ):
        raise ValueError("frozen plan changed")
    if plan["rankable"] is not False or plan["profile"] != "developer":
        raise ValueError("local plans must be non-rankable")
    repeats = 1 if plan["purpose"] == "screen" else REPEATS
    validate_caps(plan)
    if plan["repeats"] != repeats or plan["warmups"] != 1:
        raise ValueError("unexpected measurement protocol")
    if plan["purpose"] == "screen":
        selected = plan.get("screen_units", [])
        subset = execution_roster(plan)
        mandatory = {
            u["unit"] for u in plan["roster"] if u["batch"] or u["unit"] == EXEMPLAR
        }
        if (
            not selected
            or len(set(selected)) != len(selected)
            or len(subset) != len(selected)
            or not mandatory.issubset(selected)
            or {u["family"] for u in subset} != {u["family"] for u in plan["roster"]}
        ):
            raise ValueError("screen must cover every family, batch and exemplar")
    if (
        plan["bootstrap_samples"] != BOOTSTRAPS
        or plan["analysis_seed"] != ANALYSIS_SEED
    ):
        raise ValueError("analysis protocol changed")
    if current:
        current_identity = identity()
        for key in (
            "code_sha256",
            "toolkit_version",
            "toolkit_source",
            "scorer_sha256",
        ):
            if current_identity[key] != plan["identity"][key]:
                raise ValueError("stale code/toolkit/scorer evidence: " + key)
        if roster() != plan["roster"]:
            raise ValueError("roster changed")


def normalized_arch(arch: str) -> str:
    return {"x86_64": "amd64", "aarch64": "arm64"}.get(arch, arch)


def require_native_host(measured_host: dict, target: str = "linux/amd64") -> None:
    # Card resources are container maxima. Linux MemTotal excludes kernel-reserved
    # RAM, so comparing it to a 16 GiB container limit rejects ordinary 16 GiB hosts.
    # The actual complete run, enforced limits and peak telemetry establish G2.
    arch = target.split("/")[1]
    if (
        not measured_host["native"]
        or measured_host["docker_cpus"] < CAPS["cpus"]
        or any(
            normalized_arch(measured_host.get(key, arch)) != arch
            for key in ("arch", "docker_arch")
        )
    ):
        raise ValueError(
            f"native {target} with four CPUs is required; diagnostics cannot qualify"
        )


def confirmation_reservation(plan_path: Path, plan: dict) -> tuple[str, dict]:
    if plan["purpose"] != "confirmation" or set(plan["images"]) != {"A", "B"}:
        raise ValueError("reservation requires a paired confirmation plan")
    image = plan["images"]["B"]
    if image == plan["images"]["A"]:
        raise ValueError("confirmation requires two different images")
    return digest(image) + ".json", {
        "image": image,
        "plan": index(plan_path),
        "plan_sha256": plan["plan_sha256"],
        "reserved_at": plan["created_at"],
        "rankable": False,
    }


def export_confirmation(plan_path: Path, output: Path) -> dict:
    """Persist a reservation artifact before a disposable worker starts timing."""
    plan = json.loads(plan_path.read_text())
    validate_plan(plan, current=True)
    name, reservation = confirmation_reservation(plan_path, plan)
    if (Path(plan["history_dir"]) / "confirmations" / name).exists():
        raise ValueError("this candidate already reserved its confirmation experiment")
    save(output / name, reservation)
    return reservation


def run_plan(
    plan_path: Path,
    output: Path,
    diagnostic: bool = False,
    scratch_volume: Path | None = None,
) -> dict:
    from throughput.run_unit import retention_byte_limits, run_once

    plan = json.loads(plan_path.read_text())
    validate_plan(plan, current=True)
    if not diagnostic:
        require_native_host(plan["host"], execution_platform(plan))
        for image in set(plan["images"].values()):
            inspected = json.loads(
                subprocess.check_output(["docker", "image", "inspect", image])
            )[0]
            if inspected["Os"] + "/" + inspected["Architecture"] != execution_platform(
                plan
            ):
                raise ValueError(
                    "image architecture differs from the frozen execution platform"
                )
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    save(output / "plan.json", plan)
    if plan["purpose"] == "confirmation":
        name, reservation = confirmation_reservation(plan_path, plan)
        save(Path(plan["history_dir"]) / "confirmations" / name, reservation)
    entries = []
    started = time.monotonic()
    stop_reason = "completed-once"
    stop_error = None
    for group in range(-1, plan["repeats"]):
        for unit in execution_roster(plan):
            arms = list(plan["images"])
            if group % 2:
                arms.reverse()
            for arm in arms:
                if time.monotonic() - started >= plan["budget_sec"]:
                    stop_reason = "deadline"
                    break
                try:
                    measured_host = host()
                except Exception as exc:
                    stop_reason = "host-unavailable"
                    stop_error = {"kind": type(exc).__name__, "message": str(exc)}
                    break
                if measured_host != plan["host"]:
                    stop_reason = "host-drift"
                    break
                run_root = (
                    output
                    / ("warmup" if group == -1 else f"group-{group}")
                    / unit["unit"]
                    / arm
                )
                run_root.mkdir(parents=True)
                raw = {
                    "profile": "developer",
                    "rankable": False,
                    "retention_byte_limits": retention_byte_limits(
                        plan.get("caps", CAPS)["disk_bytes"]
                    ),
                    "plan_sha256": plan["plan_sha256"],
                    "host": plan["host"],
                    "image": plan["images"][arm],
                    "input_sha256": unit["input_sha256"],
                    "unit": unit["unit"],
                    "group": group,
                    "arm": arm,
                    "started_at": now(),
                }
                print(
                    json.dumps({"event": "unit-start", **raw}),
                    file=sys.stderr,
                    flush=True,
                )
                try:
                    result = run_once(
                        raw["image"],
                        ROOT / "units" / unit["unit"],
                        batch=unit["batch"],
                        keep_output=run_root / "output",
                        scratch_root=scratch_volume or run_root,
                        bounded_disk=scratch_volume is not None,
                        disk_limit_bytes=plan.get("caps", CAPS)["disk_bytes"],
                        log_dir=run_root,
                        run_as_host_user=True,
                        timeout_sec=min(
                            plan["timeout_sec"],
                            plan["budget_sec"] - (time.monotonic() - started),
                        ),
                    )
                    raw["measurement"] = asdict(result)
                    save(run_root / "measurement.json", raw["measurement"])
                    raw["output_dir"] = str(run_root / "output")
                    raw["parquet_sha256"] = preparation.output_hashes(
                        run_root / "output"
                    )
                    raw["verification"] = gate_output(
                        ROOT / "units" / unit["unit"],
                        run_root / "output",
                        plan["budget_sec"] - (time.monotonic() - started),
                    )
                    raw["status"] = (
                        "passed" if raw["verification"]["admissible"] else "failed"
                    )
                except Exception as exc:
                    raw["status"] = "failed"
                    if hasattr(exc, "measurement"):
                        raw["measurement"] = asdict(exc.measurement)
                    raw["error"] = {"kind": type(exc).__name__, "message": str(exc)}
                # run_once retains only sanitized output, including partial output
                # from failed invocations. Index it even when timing or gating failed.
                if (run_root / "output").is_dir():
                    raw["output_dir"] = str(run_root / "output")
                    raw["artifacts"] = [
                        index(p)
                        for p in sorted((run_root / "output").rglob("*"))
                        if p.is_file()
                    ]
                if "measurement" in raw:
                    measured = raw["measurement"]
                    # Retained bytes cannot substitute for peak writable disk.
                    raw["resources"] = {
                        "peak_memory_bytes": measured["host_peak_memory_bytes"],
                        "output_bytes": sum(
                            Path(entry["path"]).stat().st_size
                            for entry in raw.get("artifacts", [])
                        ),
                        "peak_disk_bytes": measured["host_peak_disk_bytes"],
                        "disk_capacity_bytes": measured["disk_capacity_bytes"],
                        "disk_status": "bounded"
                        if measured["disk_capacity_bytes"]
                        else "missing",
                    }
                raw["logs"] = [index(p) for p in sorted(run_root.glob("*.log"))]
                raw["checkpoints"] = [
                    index(run_root / name)
                    for name in ("execution.json", "measurement.json")
                    if (run_root / name).exists()
                ]
                raw["finished_at"] = now()
                save(run_root / "record.json", raw)
                entries.append(index(run_root / "record.json"))
                print(
                    json.dumps(
                        {
                            "event": "unit-finished",
                            "record": entries[-1],
                            **{
                                key: raw.get(key)
                                for key in (
                                    "unit",
                                    "group",
                                    "arm",
                                    "status",
                                    "measurement",
                                    "resources",
                                    "error",
                                )
                            },
                        }
                    ),
                    file=sys.stderr,
                    flush=True,
                )
                failure = (
                    raw.get("error", {}).get("kind", "semantic")
                    if raw["status"] == "failed"
                    else None
                )
                consecutive = record_problem(
                    Path(plan["history_dir"]), unit["unit"], failure
                )
                if consecutive >= 3:
                    stop_reason = "three-consecutive-same-failures"
                    break
                if failure:
                    # A failed required invocation already disqualifies this plan.
                    # In particular, a timed-out Docker client does not prove its
                    # container stopped; never launch another unit after failure.
                    stop_reason = "unit-failed"
                    break
            if stop_reason != "completed-once":
                break
        if stop_reason != "completed-once":
            break
    result = {
        "profile": "developer",
        "rankable": False,
        "plan": index(output / "plan.json"),
        "runs": entries,
        "stop_reason": stop_reason,
        "stop_error": stop_error,
        "diagnostic": diagnostic,
        "finished_at": now(),
    }
    save(output / "evidence.json", result)
    return result


def paired_statistics(plan: dict, records: list[dict]) -> dict:
    """Bootstrap whole paired groups, recomputing the defined median-rate score."""
    import numpy as np

    units = plan["roster"]
    arms = list(plan["images"])
    cube = np.zeros((len(arms), len(units), REPEATS))
    lookup = {(r["arm"], r["unit"], r["group"]): r for r in records}
    if len(lookup) != len(records):
        raise ValueError("duplicate run evidence")
    families = {}
    for i, unit in enumerate(units):
        families.setdefault(unit["family"], []).append(i)
        for a, arm in enumerate(arms):
            for group in range(REPEATS):
                record = lookup[(arm, unit["unit"], group)]
                m = record["measurement"]
                wall, count = m["host_wall_clock_sec"], m["n_events"]
                if (
                    not isinstance(count, int)
                    or isinstance(count, bool)
                    or count <= 0
                    or not isinstance(wall, (int, float))
                    or not math.isfinite(wall)
                    or wall <= 0
                    or count != m["reported_n_events"]
                ):
                    raise ValueError("invalid host measurement")
                cube[a, i, group] = count / wall
    for unit in units:
        counts = {
            r["measurement"]["n_events"] for r in records if r["unit"] == unit["unit"]
        }
        if len(counts) != 1:
            raise ValueError("paired candidates must preserve the unit event count")
    rates = np.median(cube, axis=2)
    result = {
        "scores": {arm: float(rates[a].mean()) for a, arm in enumerate(arms)},
        "units": {
            u["unit"]: {arm: float(rates[a, i]) for a, arm in enumerate(arms)}
            for i, u in enumerate(units)
        },
    }
    if len(arms) != 2:
        return result
    rng = np.random.default_rng(plan["analysis_seed"])
    samples = rng.integers(0, REPEATS, (plan["bootstrap_samples"], REPEATS))
    # Each draw selects the SAME group indices for both arms and the entire roster.
    deltas = np.median(cube[1][:, samples], axis=2) - np.median(
        cube[0][:, samples], axis=2
    )

    def interval(x):
        return [float(v) for v in np.quantile(x, [0.025, 0.975])]

    result.update(
        delta_score=float((rates[1] - rates[0]).mean()),
        ci95=interval(deltas.mean(axis=0)),
        family={
            family: {
                "delta": float((rates[1, idx] - rates[0, idx]).mean()),
                "ci95": interval(deltas[idx].mean(axis=0)),
            }
            for family, idx in families.items()
        },
        worst_unit_delta=float((rates[1] - rates[0]).min()),
    )
    return result


def assess(evidence_path: Path) -> dict:
    evidence = json.loads(evidence_path.read_text())
    plan = read_index(evidence["plan"])
    validate_plan(plan, current=True)
    caps = validate_caps(plan)
    records = [read_index(entry) for entry in evidence["runs"]]
    reasons = []
    expected = {
        (a, u["unit"], g)
        for a in plan["images"]
        for u in execution_roster(plan)
        for g in range(-1, plan["repeats"])
    }
    actual = {(r["arm"], r["unit"], r["group"]) for r in records}
    if actual != expected or len(actual) != len(records):
        reasons.append("incomplete-or-duplicate-roster")
    for r in records:
        unit = next(u for u in plan["roster"] if u["unit"] == r["unit"])
        if (
            r["host"] != plan["host"]
            or r["image"] != plan["images"][r["arm"]]
            or r["input_sha256"] != unit["input_sha256"]
            or r["plan_sha256"] != plan["plan_sha256"]
            or r["rankable"] is not False
        ):
            reasons.append("stale-run-identity")
        if r["status"] != "passed" or not r.get("verification", {}).get("admissible"):
            reasons.append("semantic-or-execution-failure")
        for entry in r.get("artifacts", []):
            if file_digest(Path(entry["path"])) != entry["sha256"]:
                reasons.append("changed-output-artifact")
        if r.get("status") == "passed":
            from throughput.run_unit import _host_n_events, _reported_n_events

            output_dir = Path(r.get("output_dir", ""))
            try:
                hashes = preparation.output_hashes(output_dir)
                expected_files = preparation.repeat_artifacts(
                    ROOT / "units" / unit["unit"]
                )
                from qfbench2_track_simulation.limits import stable_paths_for

                allowed_files = set(stable_paths_for(ROOT / "units" / unit["unit"]))
                gates = r["verification"]["gates"]
                checked_gates = gates
                if unit["unit"] == EXEMPLAR:
                    if (
                        set(gates)
                        != {
                            "g0_integrity",
                            "g1_schema",
                            "g2_cutoff_resource",
                            "g3_domain_semantics",
                        }
                        or r["verification"].get("semantic_status") != "unknown"
                        or gates.get("g3_domain_semantics", {}).get("passed")
                        is not None
                        or r["verification"].get("local_trace_checks", {}).get("passed")
                        is not True
                    ):
                        raise ValueError(
                            "exemplar must preserve unknown reference semantics"
                        )
                    checked_gates = {
                        k: v for k, v in gates.items() if k != "g3_domain_semantics"
                    }
                if (
                    not expected_files.issubset(hashes)
                    or not set(hashes).issubset(allowed_files)
                    or hashes != r["parquet_sha256"]
                    or len(gates) != 4
                    or not all(g["passed"] for g in checked_gates.values())
                    or not r.get("artifacts")
                    or not r.get("logs")
                ):
                    raise ValueError("incomplete raw outputs or gates")
                measured = r["measurement"]
                if (
                    unit["unit"] == EXEMPLAR
                    and r["verification"]["local_trace_checks"].get("decoded_rows")
                    != measured["n_events"]
                ):
                    raise ValueError("exemplar decoded count differs from host count")
                if (
                    _host_n_events(output_dir, unit["batch"]) != measured["n_events"]
                    or _reported_n_events(output_dir, unit["batch"])
                    != measured["n_events"]
                ):
                    raise ValueError(
                        "footer/event counts differ from recorded host count"
                    )
            except (KeyError, OSError, ValueError):
                reasons.append("invalid-raw-output")
        for entry in r.get("logs", []) + r.get("checkpoints", []):
            if file_digest(Path(entry["path"])) != entry["sha256"]:
                reasons.append("changed-output-artifact")
        resources = r.get("resources", {})
        capacity = resources.get("disk_capacity_bytes")
        if (
            resources.get("disk_status") != "bounded"
            or not isinstance(capacity, int)
            or not 0 < capacity <= caps["disk_bytes"]
        ):
            reasons.append("missing-disk-bound")
        for key, cap in (
            ("peak_memory_bytes", caps["memory_bytes"]),
            ("peak_disk_bytes", caps["disk_bytes"]),
        ):
            value = resources.get(key)
            if not isinstance(value, int) or value <= 0:
                reasons.append("missing-" + key)
            elif value > cap:
                reasons.append("resource-cap-exceeded")
    for arm in plan["images"]:
        for unit in execution_roster(plan):
            repeats = [
                r.get("parquet_sha256")
                for r in records
                if r["arm"] == arm and r["unit"] == unit["unit"]
            ]
            if not repeats or not repeats[0] or any(h != repeats[0] for h in repeats):
                reasons.append("byte-repeat-failure")
    if not plan["host"]["native"] or evidence.get("diagnostic"):
        reasons.append("not-native-qualification")
    if "docker_arch" in plan["host"]:
        try:
            require_native_host(plan["host"], execution_platform(plan))
        except ValueError:
            reasons.append("wrong-execution-platform")
    if plan["holdout"] is None and plan["purpose"] != "screen":
        reasons.append("missing-independent-validation")
    elif plan["holdout"] is not None:
        from validate_candidate_parameters import validate as validate_holdout

        heldouts = read_index(plan["holdout"])
        for image in set(plan["images"].values()):
            entry = heldouts.get(image)
            if entry is None:
                reasons.append("missing-independent-validation")
                continue
            try:
                result = read_index(entry)
                validate_holdout(result, image, execution_platform(plan))
                for key in ("toolkit_version", "toolkit_source", "scorer_sha256"):
                    if result["identity"][key] != plan["identity"][key]:
                        raise ValueError("heldout verifier changed")
            except ValueError:
                reasons.append("independent-validation-failed")
    score = None
    measurement_errors = {
        "incomplete-or-duplicate-roster",
        "semantic-or-execution-failure",
        "stale-run-identity",
        "changed-output-artifact",
        "byte-repeat-failure",
        "invalid-raw-output",
    }
    if plan["purpose"] != "screen" and not measurement_errors.intersection(reasons):
        score = paired_statistics(plan, [r for r in records if r["group"] >= 0])
    g2 = (
        "pass"
        if not reasons
        else "fail"
        if "semantic-or-execution-failure" in reasons
        or "byte-repeat-failure" in reasons
        or "independent-validation-failed" in reasons
        else "missing"
    )
    g3 = "missing"
    screen_status = None
    if plan["purpose"] == "screen":
        screen_status = "pass" if g2 == "pass" else g2
        g2 = "missing"
        reasons.append("screening-only-not-qualification")
    if (
        score
        and len(plan["images"]) == 2
        and plan["purpose"] == "confirmation"
        and g2 == "pass"
    ):
        if (
            any(f["ci95"][1] < 0 for f in score["family"].values())
            or score["ci95"][1] < 0
        ):
            g3 = "fail"
        elif score["ci95"][0] > 0:
            g3 = "pass"
        else:
            g3 = "inconclusive"
    return {
        "profile": "developer",
        "rankable": False,
        "execution_platform": execution_platform(plan),
        "qualification_scope": "local-arm64"
        if execution_platform(plan) == "linux/arm64"
        else "amd64",
        "caps": caps,
        "G2": g2,
        "G3": g3,
        "reasons": sorted(set(reasons)),
        "statistics": score,
        "evidence": index(evidence_path),
        "plan_sha256": plan["plan_sha256"],
        "images": plan["images"],
        "purpose": plan["purpose"],
        "screen_status": screen_status,
        "executed_units": len({r["unit"] for r in records}),
        "exemplar_semantics": "unknown",
        "next_action": "freeze-independent-confirmation"
        if screen_status == "pass"
        else "repair-screen-failure"
        if screen_status == "fail"
        else "repair-correctness"
        if g2 == "fail"
        else "supply-missing-evidence"
        if g2 != "pass"
        else "promote"
        if g3 == "pass"
        else "choose-new-hypothesis",
    }


@contextmanager
def history_lock(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".lock").open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def next_problem_count(prior: dict | None, failure: str | None) -> int:
    return (
        prior["count"] + 1
        if prior and failure and prior["failure"] == failure
        else 1
        if failure
        else 0
    )


def record_problem(directory: Path, unit: str, failure: str | None) -> int:
    """A different round name cannot reset the same unresolved unit failure."""
    with history_lock(directory):
        path = directory / "problems.jsonl"
        records = (
            [json.loads(line) for line in path.read_text().splitlines()]
            if path.exists()
            else []
        )
        prior = next((r for r in reversed(records) if r["unit"] == unit), None)
        count = next_problem_count(prior, failure)
        with path.open("a") as f:
            f.write(
                json.dumps(
                    {"time": now(), "unit": unit, "failure": failure, "count": count}
                )
                + "\n"
            )
        return count


def history(directory: Path) -> list[dict]:
    events = [json.loads(p.read_text()) for p in sorted(directory.glob("[0-9]*.json"))]
    previous = None
    for e in events:
        if e["previous"] != previous or e["sha256"] != digest(
            {k: v for k, v in e.items() if k != "sha256"}
        ):
            raise ValueError("candidate history modified")
        previous = e["sha256"]
    return events


def decide(
    directory: Path,
    evidence_path: Path,
    g1_paths: list[Path],
    baseline_expected: str | None,
) -> dict:
    result = assess(evidence_path)
    g1 = {
        json.loads(p.read_text())["image"]: (p, json.loads(p.read_text()))
        for p in g1_paths
    }
    with history_lock(directory):
        events = history(directory)
        stable = events[-1]["stable"] if events else None
        if stable != baseline_expected:
            raise ValueError("stable pointer changed; compare again")
        previous_policy = next(
            (e["decision"] for e in reversed(events) if "decision" in e), None
        )
        if previous_policy and (
            execution_platform(previous_policy) != result["execution_platform"]
            or previous_policy.get("caps", CAPS) != result["caps"]
        ):
            raise ValueError(
                "candidate history belongs to another platform/resource policy"
            )
        for image in result["images"].values():
            _, report = g1.get(image, (None, {}))
            if (
                report.get("scope") != "G1"
                or report.get("status") != "passed"
                or report.get("rankable") is not False
            ):
                result["reasons"].append("missing-G1")
                continue
            preparation.validate_delivery(
                report["delivery"], image, result["execution_platform"]
            )
            if file_digest(Path(report["archive"])) != report["archive_sha256"]:
                raise ValueError("submission package changed")
        result["G1"] = "missing" if "missing-G1" in result["reasons"] else "pass"
        attempt_images = list(result["images"].values())
        candidate = attempt_images[-1]
        if any(
            e.get("candidate") == candidate and e.get("purpose") == "confirmation"
            for e in events
        ):
            raise ValueError("this candidate already used its confirmation experiment")
        can_promote = result["G1"] == result["G2"] == "pass" and (
            (
                stable is None
                and result["purpose"] == "baseline"
                and len(attempt_images) == 1
            )
            or (
                candidate != stable
                and stable == result["images"].get("A")
                and result["G3"] == "pass"
            )
        )
        event = {
            "time": now(),
            "action": "promote"
            if can_promote
            else "reject"
            if result["G2"] == "fail" or result["G3"] == "fail"
            else "hold",
            "candidate": candidate,
            "previous_stable": stable,
            "stable": candidate if can_promote else stable,
            "purpose": result["purpose"],
            "decision": result,
            "G1_evidence": [index(p) for p in g1_paths],
            "previous": events[-1]["sha256"] if events else None,
            "rankable": False,
        }
        event["sha256"] = digest(event)
        save(directory / f"{len(events):06d}.json", event)
        return event


def rollback(directory: Path, target: str, reason: str, expected: str) -> dict:
    """Revoke the current candidate and choose a still-qualified prior digest."""
    if not reason.strip():
        raise ValueError("rollback needs a concrete failure reason")
    with history_lock(directory):
        events = history(directory)
        if not events or events[-1]["stable"] != expected or target == expected:
            raise ValueError("stable pointer changed or rollback target is current")
        revoked = {e["previous_stable"] for e in events if e["action"] == "rollback"}
        candidates = [
            e for e in events if e["action"] == "promote" and e["stable"] == target
        ]
        if not candidates or target in revoked:
            raise ValueError(
                "rollback target is not a usable historical stable candidate"
            )
        # Paired confirmation requalifies both immutable images under the current
        # controller. Use that fresh evidence for B0 after a B1 source change;
        # B0's original plan may correctly fail the current-code identity check.
        qualified = [
            e
            for e in events
            if e.get("decision", {}).get("G1") == "pass"
            and e["decision"].get("G2") == "pass"
            and target in e["decision"].get("images", {}).values()
        ]
        if not qualified:
            raise ValueError("rollback target has no complete qualification")
        chosen = qualified[-1]
        proof = chosen["decision"]["evidence"]
        read_index(proof)
        if assess(Path(proof["path"]))["G2"] != "pass":
            raise ValueError("rollback target no longer passes current G2")
        for entry in chosen["G1_evidence"]:
            report = read_index(entry)
            if report["image"] == target:
                if file_digest(Path(report["archive"])) != report["archive_sha256"]:
                    raise ValueError("rollback package changed")
                break
        else:
            raise ValueError("rollback target has no G1 package")
        event = {
            "time": now(),
            "action": "rollback",
            "previous_stable": expected,
            "stable": target,
            "reason": reason,
            "rankable": False,
            "previous": events[-1]["sha256"],
            "qualification": proof,
        }
        event["sha256"] = digest(event)
        save(directory / f"{len(events):06d}.json", event)
        return event


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="action", required=True)
    freeze_p = sub.add_parser("freeze")
    freeze_p.add_argument("--image", required=True)
    freeze_p.add_argument("--baseline")
    freeze_p.add_argument(
        "--purpose", choices=["baseline", "screen", "confirmation"], required=True
    )
    freeze_p.add_argument("--round", required=True)
    freeze_p.add_argument("--hypothesis", required=True)
    freeze_p.add_argument("--timeout", type=float, default=1800)
    freeze_p.add_argument(
        "--execution-platform",
        choices=("linux/amd64", "linux/arm64"),
        default="linux/amd64",
    )
    freeze_p.add_argument("--disk-gib", type=int, choices=(10, 64), default=10)
    freeze_p.add_argument("--budget", type=float, required=True)
    freeze_p.add_argument("--holdout", type=Path)
    freeze_p.add_argument("--history", type=Path, default=ROOT / "out/candidates")
    freeze_p.add_argument("--out", type=Path, required=True)
    run_p = sub.add_parser("run")
    run_p.add_argument("--plan", type=Path, required=True)
    run_p.add_argument("--out", type=Path, required=True)
    run_p.add_argument("--diagnostic", action="store_true")
    run_p.add_argument("--scratch-volume", type=Path)
    reserve_p = sub.add_parser("export-confirmation")
    reserve_p.add_argument("--plan", type=Path, required=True)
    reserve_p.add_argument("--out", type=Path, required=True)
    assess_p = sub.add_parser("assess")
    assess_p.add_argument("--evidence", type=Path, required=True)
    decide_p = sub.add_parser("decide")
    decide_p.add_argument("--evidence", type=Path, required=True)
    decide_p.add_argument("--history", type=Path, required=True)
    decide_p.add_argument("--g1", type=Path, action="append", default=[])
    decide_p.add_argument("--expected-stable")
    back = sub.add_parser("rollback")
    back.add_argument("--history", type=Path, required=True)
    back.add_argument("--to", required=True)
    back.add_argument("--reason", required=True)
    back.add_argument("--expected-stable", required=True)
    args = p.parse_args()
    try:
        if args.action == "freeze":
            result = freeze(args)
        elif args.action == "run":
            result = run_plan(args.plan, args.out, args.diagnostic, args.scratch_volume)
        elif args.action == "export-confirmation":
            result = export_confirmation(args.plan, args.out)
        elif args.action == "assess":
            result = assess(args.evidence)
        elif args.action == "rollback":
            result = rollback(args.history, args.to, args.reason, args.expected_stable)
        else:
            result = decide(args.history, args.evidence, args.g1, args.expected_stable)
        print(json.dumps(result, indent=2, allow_nan=False))
        if args.action == "run":
            return int(result["stop_reason"] != "completed-once")
        if args.action == "assess":
            gate = {"screen": "screen_status", "baseline": "G2", "confirmation": "G3"}[
                result["purpose"]
            ]
            return int(result[gate] != "pass")
        return 0
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
