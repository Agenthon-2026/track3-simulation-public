"""Measure where the v2 simulator spends time, without changing its submission.

Stage timings, instrumented call attribution and unchanged CLI timings are kept
separate. This is diagnostic evidence, never an official score or qualification.
The controller runs on native Linux amd64; workers also support local smoke tests.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent.parent
V2 = "ghcr.io/youxuanxue/track3-simulation-public@sha256:7312745e1c9a09cb898402e74a5d18b83cbfb6b5372a3739464a9c01e6e84259"
PROFILE_UNITS = [
    "t3-s001-price-time-priority",
    "t3-eq-deterministic-baseline",
    "t3-eq-lognormal-highsigma",
    "t3-mp05-cancel-churn-newest",
    "t3-ca-baseline-calm",
    "t3-as06-throughput-fast",
    "t3-gb-mega-throughput",
    "t3-mr-deep-book-state-size",
]


def save(path, value):
    Path(path).write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def table_identity(table):
    import pyarrow as pa

    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return {
        "rows": table.num_rows,
        "arrow_sha256": hashlib.sha256(sink.getvalue()).hexdigest(),
    }


def stage_worker(args):
    # Match the participant's pre-timer imports. Loading the sibling diagnostic
    # extension is diagnostic setup, not a claim about cold CLI initialization.
    import fast_sim.simulate  # noqa: F401

    native = importlib.import_module("fast_sim._native_diagnostic")
    scenario = json.loads(args.scenario.read_text())
    records = []
    expected = None
    for repeat in range(args.repeats + 1):
        start = time.perf_counter()
        from fast_sim.native_boot import spec_from_scenario

        spec = spec_from_scenario(deepcopy(scenario))
        bootstrap_sec = time.perf_counter() - start
        placements = sum(
            max(0, spec["mkt_close"] - spec["mkt_open"])
            // max(1, a["interval_ns"])
            * (2 * a["depth_levels"] if a["kind"] == 2 else 1)
            for a in spec["agents"]
            if a["kind"]
        )
        assert (
            placements <= 1_000_000
        ), "stage diagnosis only supports the published buffered path"
        trace, ledger, phases = native.run_native_sim_diagnostic(spec)
        elapsed = bootstrap_sec + phases["seconds"]["total_sec"]
        if expected is None:
            from fast_sim._native import run_native_sim

            expected = run_native_sim(spec_from_scenario(deepcopy(scenario)))
        assert trace.equals(
            expected[0], check_metadata=True
        ), "trace differs from published v2"
        assert ledger.equals(
            expected[1], check_metadata=True
        ), "ledger differs from published v2"
        records.append(
            {
                "first_call": repeat == 0,
                "bootstrap_sec": bootstrap_sec,
                "total_sec": elapsed,
                "phases": phases,
                "exact_v2": True,
            }
        )
    save(
        args.out,
        {
            "rankable": False,
            "scope": "buffered-native-stage-diagnostic",
            "first_call_note": "Pre-timer imports and sibling extension already loaded; warm results do not include cold CLI imports",
            "scenario_sha256": sha(args.scenario),
            "tables": [table_identity(t) for t in expected],
            "records": records,
        },
    )


def profile_worker(args):
    import cProfile
    import pstats
    from fast_sim.native_boot import spec_from_scenario

    native = importlib.import_module("fast_sim._native_profile")
    scenario = json.loads(args.scenario.read_text())
    # Warm dependency imports outside function-level attribution. The published
    # CLI's cold costs are measured separately by actual fresh container launches.
    native.run_native_sim(spec_from_scenario(deepcopy(scenario)))
    profile = cProfile.Profile()
    profile.enable()
    trace, ledger = native.run_native_sim(spec_from_scenario(deepcopy(scenario)))
    profile.disable()
    stats = pstats.Stats(profile)
    rows = []
    for (filename, line, name), (cc, nc, own, cumulative, _) in stats.stats.items():
        rows.append(
            {
                "file": filename,
                "line": line,
                "name": name,
                "primitive_calls": cc,
                "calls": nc,
                "self_sec": own,
                "cumulative_sec": cumulative,
            }
        )
    from fast_sim._native import run_native_sim

    expected = run_native_sim(spec_from_scenario(deepcopy(scenario)))
    assert all(
        a.equals(b, check_metadata=True) for a, b in zip((trace, ledger), expected)
    )
    save(
        args.out,
        {
            "rankable": False,
            "scope": "instrumented-call-attribution-not-speed",
            "warning": "Per-call profiler overhead biases hot function timing; do not combine with unprofiled stage seconds",
            "exact_v2": True,
            "total_instrumented_sec": stats.total_tt,
            "tables": [table_identity(t) for t in expected],
            "functions": sorted(rows, key=lambda r: r["self_sec"], reverse=True),
        },
    )


def container_run(image, unit, dest, verb, *, log_root, scripts=False, workers=None):
    sys.path.insert(0, str(ROOT))
    from throughput.timer import bounded_container_run

    dest.mkdir(parents=True)
    output = dest / "output"
    output.mkdir()
    output.chmod(0o777)
    batch = (unit / "batch.json").exists()
    mount = unit / "scenarios" if batch else unit / "scenario.json"
    target = "/input/scenarios" if batch else "/input/scenario.json"
    cid = dest / "container.cid"
    cmd = [
        "docker",
        "run",
        "--rm",
        "--platform=linux/amd64",
        "--network=none",
        "--cpus=4",
        "--memory=16g",
        "--memory-swap=16g",
        "--read-only",
        "--user=65534:65534",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m",
        "--pids-limit=256",
        "--ulimit=nofile=1024:1024",
        "--ulimit=nproc=256:256",
        "--ulimit=fsize=67108864:67108864",
        "--cidfile",
        str(cid),
        "-v",
        f"{mount}:{target}:ro",
        "-v",
        f"{output}:/output",
        "-e",
        "QFBENCH_SEED=0",
        "-e",
        "QFBENCH_NETWORK=none",
    ]
    if scripts:
        cmd += ["-v", f"{Path(__file__).parent}:/diagnostics:ro"]
    if workers is not None:
        cmd += ["-e", f"FAST_SIM_BATCH_WORKERS={workers}"]
    start = time.perf_counter()
    proc = bounded_container_run(cmd + [image, *verb], cidfile=cid, timeout_sec=300)
    elapsed = time.perf_counter() - start
    logs = log_root / unit.name / dest.name
    logs.mkdir(parents=True)
    (logs / "stdout.log").write_bytes(proc.stdout)
    (logs / "stderr.log").write_bytes(proc.stderr)
    proc.check_returncode()
    if sum(p.stat().st_size for p in output.rglob("*") if p.is_file()) > 64 * 1024**2:
        raise ValueError("output exceeds published aggregate size cap")
    return output, elapsed


def output_metadata(output, batch):
    import pyarrow.parquet as pq

    name = "batch_events.json" if batch else "events.json"
    events = json.loads((output / name).read_text())
    parquet = {
        str(p.relative_to(output)): {
            "rows": pq.ParquetFile(p).metadata.num_rows,
            "sha256": sha(p),
        }
        for p in output.rglob("*.parquet")
    }
    actual = sum(
        v["rows"] for k, v in parquet.items() if Path(k).name == "trace.parquet"
    )
    assert actual == events.get("total_events", events.get("n_events"))
    for path in output.rglob("events.json"):
        sub = json.loads(path.read_text())
        for stem in ("trace", "message_trace"):
            table = path.parent / (stem + ".parquet")
            if table.exists():
                assert sha(table) == sub[stem + "_sha256"]
    return {"events": events, "actual_events": actual, "parquet": parquet}


def controller(args):
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "AMD64"):
        raise ValueError("Credible diagnosis controller requires native Linux amd64")
    units = sorted(
        p for p in (ROOT / "units").iterdir() if p.is_dir() and "EXAMPLE" not in p.name
    )
    assert len(units) == 71
    if args.unit:
        units = [p for p in units if p.name in args.unit]
        assert len(units) == len(set(args.unit))
    args.out.mkdir(parents=True, exist_ok=False)
    image_info = {}
    for label, image in (("v2", V2), ("diagnostic", args.image)):
        inspected = json.loads(
            subprocess.check_output(["docker", "image", "inspect", image])
        )[0]
        assert inspected["Architecture"] == "amd64" and inspected["Os"] == "linux"
        image_info[label] = {
            "requested": image,
            "id": inspected["Id"],
            "repo_digests": inspected.get("RepoDigests", []),
        }
    result = {
        "rankable": False,
        "qualification": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "images": image_info,
        "source_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        "machine": {
            "uname": list(platform.uname()),
            "cpus": os.cpu_count(),
            "cpuinfo": Path("/proc/cpuinfo").read_text(),
            "meminfo": Path("/proc/meminfo").read_text(),
        },
        "limitations": [
            "GitHub runner, not organizer hardware",
            "No GPU",
            "Per-call profile timing is perturbed",
            "Original exemplar excluded per organizer guidance",
        ],
        "repeats": args.repeats,
        "units": {},
    }
    with tempfile.TemporaryDirectory(prefix="t3-diagnose-") as scratch:
        scratch = Path(scratch)
        scratch.chmod(0o755)
        for unit in units:
            print("START", unit.name, flush=True)
            batch = (unit / "batch.json").exists()
            row = {
                "batch": batch,
                "batch_workers": min(4, len(list((unit / "scenarios").glob("*.json"))))
                if batch
                else None,
                "cold_cli": [],
                "input_sha256": {
                    str(p.relative_to(unit)): sha(p)
                    for p in (
                        sorted((unit / "scenarios").glob("*.json"))
                        if batch
                        else [unit / "scenario.json"]
                    )
                },
            }
            canonical = None
            # The first invocation is a separately reported OS-cache warmup;
            # every measured invocation still starts a fresh Python interpreter.
            for repeat in range(args.repeats + 1):
                verb = (
                    [
                        "simulate-batch",
                        "--batch-dir",
                        "/input/scenarios",
                        "--out-dir",
                        "/output",
                    ]
                    if batch
                    else [
                        "simulate",
                        "--config",
                        "/input/scenario.json",
                        "--out",
                        "/output/trace.parquet",
                    ]
                )
                output, elapsed = container_run(
                    image_info["v2"]["id"],
                    unit,
                    scratch / unit.name / f"cold-{repeat}",
                    verb,
                    log_root=args.out / "logs",
                    workers=4 if batch else None,
                )
                metrics = output_metadata(output, batch)
                if canonical is None:
                    canonical = metrics["parquet"]
                assert metrics["parquet"] == canonical, "unchanged v2 repeat differs"
                row["cold_cli"].append(
                    {"warmup": repeat == 0, "host_sec": elapsed, **metrics}
                )
            if not batch:
                verb = [
                    "python",
                    "/diagnostics/run_diagnosis.py",
                    "stage-worker",
                    "--scenario",
                    "/input/scenario.json",
                    "--out",
                    "/output/stages.json",
                    "--repeats",
                    str(args.stage_repeats),
                ]
                output, _ = container_run(
                    image_info["diagnostic"]["id"],
                    unit,
                    scratch / unit.name / "stages",
                    verb,
                    log_root=args.out / "logs",
                    scripts=True,
                )
                row["stages"] = json.loads((output / "stages.json").read_text())
                assert (
                    row["stages"]["tables"][0]["rows"]
                    == row["cold_cli"][0]["actual_events"]
                )
                if unit.name in PROFILE_UNITS:
                    verb = [
                        "python",
                        "/diagnostics/run_diagnosis.py",
                        "profile-worker",
                        "--scenario",
                        "/input/scenario.json",
                        "--out",
                        "/output/profile.json",
                    ]
                    output, _ = container_run(
                        image_info["diagnostic"]["id"],
                        unit,
                        scratch / unit.name / "profile",
                        verb,
                        log_root=args.out / "logs",
                        scripts=True,
                    )
                    row["profile"] = json.loads((output / "profile.json").read_text())
                    assert row["profile"]["tables"] == row["stages"]["tables"]
            else:
                row["serial_batch"] = []
                for repeat in range(args.repeats):
                    output, elapsed = container_run(
                        image_info["v2"]["id"],
                        unit,
                        scratch / unit.name / f"serial-{repeat}",
                        verb,
                        log_root=args.out / "logs",
                        workers=1,
                    )
                    metrics = output_metadata(output, True)
                    assert (
                        metrics["parquet"] == canonical
                    ), "serial/parallel batch values differ"
                    row["serial_batch"].append({"host_sec": elapsed, **metrics})
            result["units"][unit.name] = row
            save(args.out / "results.json", result)
            print(
                "PASS",
                unit.name,
                "cold_sim_median",
                statistics.median(
                    r["events"]["wall_clock_sec"]
                    for r in row["cold_cli"]
                    if not r["warmup"]
                ),
                flush=True,
            )
    result["passed"] = True
    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    save(args.out / "results.json", result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["run", "stage-worker", "profile-worker"])
    parser.add_argument("--image")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--scenario", type=Path)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--stage-repeats", type=int, default=5)
    parser.add_argument("--unit", action="append", default=[])
    args = parser.parse_args()
    if args.repeats < 1 or args.stage_repeats < 1:
        parser.error("repeats must be positive")
    if args.action == "run":
        if not args.image:
            parser.error("run requires --image")
        controller(args)
    else:
        if not args.scenario:
            parser.error("worker requires --scenario")
        {"stage-worker": stage_worker, "profile-worker": profile_worker}[args.action](
            args
        )


if __name__ == "__main__":
    main()
