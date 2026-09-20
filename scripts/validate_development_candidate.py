"""Validate exact public outputs and pair candidate throughput on native amd64.

This bounded developer check preserves all 71 runnable public inputs. It checks
semantic gates once, then measures fresh container launches in alternating order.
The original exemplar is excluded; this is not official timing or promotion.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import stat
import statistics
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]
from scripts.diagnostics import run_diagnosis as runtime  # noqa: E402

EXEMPLAR = "t3-EXAMPLE-vectorized-matching"
OUTPUT_CAP = 64 * 1024**2


def save(path, value):
    Path(path).write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def sha(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def roster(names):
    units = sorted(
        p for p in (ROOT / "units").iterdir() if p.is_dir() and p.name != EXEMPLAR
    )
    if len(units) != 71:
        raise ValueError("Expected canonical 71-unit runnable roster")
    if names:
        if len(names) != len(set(names)) or set(names) - {u.name for u in units}:
            raise ValueError("Duplicate or unknown unit selection")
        units = [u for u in units if u.name in names]
    return units


def input_hashes(unit):
    paths = (
        sorted((unit / "scenarios").glob("*.json"))
        if (unit / "batch.json").exists()
        else [unit / "scenario.json"]
    )
    # Bind gate policy and batch membership as well as mounted scenario inputs.
    paths += [p for p in (unit / "card.toml", unit / "batch.json") if p.exists()]
    return {str(p.relative_to(unit)): sha(p) for p in paths}


def prefixes(unit):
    if (unit / "batch.json").exists():
        from qfbench2_track_simulation.batch import load_subs

        return [entry["sub"] for entry in load_subs(unit)]
    return [""]


def inventory(unit, output):
    from qfbench2_track_simulation.limits import allowed_paths_for
    from scripts.prepare_submission import repeat_artifacts

    allowed = set(allowed_paths_for(unit))
    files = {}
    for directory, dirs, names in os.walk(output, followlinks=False):
        for name in dirs:
            if not stat.S_ISDIR((Path(directory) / name).lstat().st_mode):
                raise ValueError("Non-directory output node")
        for name in names:
            path = Path(directory) / name
            info = path.lstat()
            relative = path.relative_to(output).as_posix()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or relative not in allowed
            ):
                raise ValueError(f"Disallowed output: {relative}")
            if info.st_size > OUTPUT_CAP:
                raise ValueError("Output file exceeds 64 MiB")
            files[relative] = {"bytes": info.st_size, "sha256": sha(path)}
    if sum(f["bytes"] for f in files.values()) > OUTPUT_CAP:
        raise ValueError("Output tree exceeds 64 MiB")
    required = repeat_artifacts(unit)
    required |= {str(Path(p) / "events.json") for p in prefixes(unit)}
    if (unit / "batch.json").exists():
        required.add("batch_events.json")
    if required - set(files):
        raise ValueError(f"Missing output: {sorted(required - set(files))}")
    return files


def positive_time(value):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError("Invalid measured time")
    return value


def measured_output(unit, output, elapsed):
    files = inventory(unit, output)
    batch = (unit / "batch.json").exists()
    measured = runtime.output_metadata(output, batch)
    counts = {}
    for prefix in prefixes(unit):
        events = json.loads((output / prefix / "events.json").read_text())
        row = {}
        for stem, key in (("trace", "n_events"), ("message_trace", "n_messages")):
            count = events[key]
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError(f"Invalid {key}")
            relative = str(Path(prefix) / (stem + ".parquet"))
            if (
                relative in measured["parquet"]
                and count != measured["parquet"][relative]["rows"]
            ):
                raise ValueError(f"Footer count differs: {relative}")
            row[key] = count
        reference = unit / "checks/reference_data" / prefix if batch else unit
        public_ledger = reference / "message_trace.parquet"
        if public_ledger.exists():
            import pyarrow.parquet as pq

            if row["n_messages"] != pq.ParquetFile(public_ledger).metadata.num_rows:
                raise ValueError(
                    "Message count differs from actual public reference ledger"
                )
        counts[prefix or "single"] = row
    events = measured["events"]
    wall = positive_time(events["wall_clock_sec"])
    host = positive_time(elapsed)
    rate = events["events_per_sec"]
    if (
        isinstance(rate, bool)
        or not isinstance(rate, (int, float))
        or not math.isfinite(rate)
        or not math.isclose(
            rate, measured["actual_events"] / wall, rel_tol=1e-10, abs_tol=1e-10
        )
    ):
        raise ValueError("Reported rate differs from real rows / reported time")
    return {
        "inventory": files,
        "parquet": measured["parquet"],
        "counts": counts,
        "actual_events": measured["actual_events"],
        "self_reported_wall_clock_sec": wall,
        "self_reported_events_per_sec": rate,
        "host_wall_clock_sec": host,
        "host_events_per_sec": measured["actual_events"] / host,
    }


def exact_worker(unit, output, report, reference=None):
    from verify_candidate_output import limit_memory

    limit_memory()
    from validate_candidate_parameters import compare
    from qfbench2_track_simulation.scoring import _CardPolicy

    batch = (unit / "batch.json").exists()
    checks = {}
    for prefix in prefixes(unit):
        ref = (
            reference / prefix
            if reference is not None
            else (unit / "checks/reference_data" / prefix if batch else unit)
        )
        checks[prefix or "single"] = compare(
            output / prefix,
            ref,
            require_ledger=batch or _CardPolicy(unit).requires_message_ledger,
        )
    save(
        report,
        {"passed": all(c["passed"] for c in checks.values()), "comparisons": checks},
    )


def exact(unit, output, report, reference=None):
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "exact-worker",
        str(unit),
        str(output),
        str(report),
    ]
    if reference is not None:
        command.append(str(reference))
    with report.with_suffix(".stderr.log").open("wb") as log:
        subprocess.run(
            command,
            check=True,
            timeout=300,
            env={**os.environ, "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"},
            stdout=subprocess.DEVNULL,
            stderr=log,
        )
    result = json.loads(report.read_text())
    if not result["passed"]:
        raise ValueError(f"Exact comparison failed: {report}")
    return result


def identity_matches(actual, expected):
    # Byte identity is checked within an arm, after exact decoded comparison
    # established equivalence across images during the warmup pass.
    if (
        actual["parquet"] != expected["parquet"]
        or actual["counts"] != expected["counts"]
    ):
        raise ValueError(
            "Output bytes or real trace/message counts changed across repetitions"
        )


def complete(records, units, repeats):
    expected = {
        (u, arm, group)
        for u in units
        for arm in ("baseline", "candidate")
        for group in range(-1, repeats)
    }
    actual = [(r["unit"], r["arm"], r["group"]) for r in records]
    return (
        len(actual) == len(expected)
        and set(actual) == expected
        and all(r.get("passed") is True for r in records)
    )


def aggregate(records, units, repeats):
    output = {}
    for unit in units:
        measured = [r for r in records if r["unit"] == unit and r["group"] >= 0]
        pairs = []
        medians = {}
        for arm in ("baseline", "candidate"):
            arm_rows = [r for r in measured if r["arm"] == arm]
            medians[arm] = {
                metric: statistics.median(r[metric] for r in arm_rows)
                for metric in ("self_reported_events_per_sec", "host_events_per_sec")
            }
        for group in range(repeats):
            pair = {r["arm"]: r for r in measured if r["group"] == group}
            pairs.append(
                {
                    "group": group,
                    **{
                        metric + "_speedup": pair["baseline"][
                            metric + "_wall_clock_sec"
                        ]
                        / pair["candidate"][metric + "_wall_clock_sec"]
                        for metric in ("host", "self_reported")
                    },
                }
            )
        output[unit] = {
            "medians": medians,
            "pairs": pairs,
            **{
                "median_" + metric + "_speedup": statistics.median(
                    p[metric + "_speedup"] for p in pairs
                )
                for metric in ("host", "self_reported")
            },
        }
    return output


def controller(args):
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "AMD64"):
        raise ValueError("Measurements require native Linux amd64")
    if args.repeats < 1:
        raise ValueError("At least one measured pair required")
    units = roster(args.unit)
    images = {}
    for arm in ("baseline", "candidate"):
        requested = getattr(args, arm)
        info = json.loads(
            subprocess.check_output(
                ["docker", "image", "inspect", requested], timeout=30
            )
        )[0]
        if (info["Os"], info["Architecture"]) != ("linux", "amd64"):
            raise ValueError("Image must be linux/amd64")
        images[arm] = {
            "requested": requested,
            "id": info["Id"],
            "repo_digests": info.get("RepoDigests", []),
        }
    args.out.mkdir(parents=True, exist_ok=False)
    summary = {
        "kind": "exact-public-development-and-paired-cli",
        "rankable": False,
        "qualification": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "images": images,
        "source_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        "controller_sha256": sha(__file__),
        "runtime_sha256": sha(runtime.__file__),
        "machine": {
            "uname": list(platform.uname()),
            "cpus": os.cpu_count(),
            "cpuinfo": Path("/proc/cpuinfo").read_text(),
        },
        "limitations": [
            "GitHub runner, not organizer hardware or official timing",
            "GPU omitted for CPU implementation",
            "Original one-hour exemplar excluded; no promotion claim",
        ],
        "unit_roster": [u.name for u in units],
        "full_runnable_roster": len(units) == 71,
        "warmups_per_unit_per_arm": 1,
        "measured_pairs_per_unit": args.repeats,
        "records": [],
        "measurements_complete": False,
        "cleanup_complete": False,
        "passed": False,
    }
    destination = args.out / "summary.json"
    save(destination, summary)
    anchors = {}
    before = {u.name: input_hashes(u) for u in units}
    from verify_candidate_output import bounded_verify

    try:
        # Gate every canonical unit before beginning the measured paired pass.
        for group in range(-1, args.repeats):
            for unit in units:
                with tempfile.TemporaryDirectory(
                    prefix="t3-development-"
                ) as scratch_name:
                    scratch = Path(scratch_name)
                    scratch.chmod(0o755)
                    order = (
                        ("baseline", "candidate")
                        if group == -1 or group % 2 == 0
                        else ("candidate", "baseline")
                    )
                    warmup_outputs = {}
                    for arm in order:
                        label = (
                            ("warmup" if group == -1 else f"pair-{group}") + "-" + arm
                        )
                        record = {
                            "unit": unit.name,
                            "arm": arm,
                            "group": group,
                            "input_sha256": before[unit.name],
                            "passed": False,
                        }
                        summary["records"].append(record)
                        print("START", unit.name, label, flush=True)
                        try:
                            batch = (unit / "batch.json").exists()
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
                            output, elapsed = runtime.container_run(
                                images[arm]["id"],
                                unit,
                                scratch / label,
                                verb,
                                log_root=args.out / "logs",
                                workers=4 if batch else None,
                            )
                            record.update(measured_output(unit, output, elapsed))
                            if group == -1:
                                warmup_outputs[arm] = output
                                anchors[unit.name, arm] = record
                                if arm == "candidate":
                                    report_dir = args.out / "verification" / unit.name
                                    report_dir.mkdir(parents=True)
                                    record["developer_gates"] = bounded_verify(
                                        unit, output, timeout=300
                                    )
                                    if not record["developer_gates"]["admissible"]:
                                        raise ValueError(
                                            "Developer semantic gates failed"
                                        )
                                    record["exact_public_reference"] = exact(
                                        unit, output, report_dir / "public.json"
                                    )
                                    record["exact_baseline"] = exact(
                                        unit,
                                        output,
                                        report_dir / "baseline.json",
                                        warmup_outputs["baseline"],
                                    )
                                    if (
                                        record["counts"]
                                        != anchors[unit.name, "baseline"]["counts"]
                                    ):
                                        raise ValueError(
                                            "Candidate trace/message counts differ from baseline"
                                        )
                            else:
                                identity_matches(record, anchors[unit.name, arm])
                            if before[unit.name] != input_hashes(unit):
                                raise ValueError("Canonical inputs or policy changed")
                            record["passed"] = True
                        except Exception as exc:
                            record["error"] = {
                                "type": type(exc).__name__,
                                "message": str(exc),
                            }
                            raise
                        finally:
                            save(destination, summary)
                        print("PASS", unit.name, label, flush=True)
        summary["measurements_complete"] = complete(
            summary["records"], summary["unit_roster"], args.repeats
        )
        summary["cleanup_complete"] = True
        if not summary["measurements_complete"]:
            raise ValueError("Incomplete or duplicated measurement roster")
        summary["per_unit"] = aggregate(
            summary["records"], summary["unit_roster"], args.repeats
        )
        summary["summary"] = {
            metric: {
                "geometric_mean_paired_median_speedup": math.exp(
                    statistics.mean(
                        math.log(v["median_" + metric + "_speedup"])
                        for v in summary["per_unit"].values()
                    )
                ),
                "baseline_mean_unit_median_rate": statistics.mean(
                    v["medians"]["baseline"][metric + "_events_per_sec"]
                    for v in summary["per_unit"].values()
                ),
                "candidate_mean_unit_median_rate": statistics.mean(
                    v["medians"]["candidate"][metric + "_events_per_sec"]
                    for v in summary["per_unit"].values()
                ),
            }
            for metric in ("host", "self_reported")
        }
        summary["passed"] = True
    except Exception as exc:
        summary["error"] = {"type": type(exc).__name__, "message": str(exc)}
        raise
    finally:
        summary["finished_at"] = datetime.now(timezone.utc).isoformat()
        save(destination, summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--unit", action="append", default=[])
    args = parser.parse_args()
    args.out = args.out.resolve()
    controller(args)


if __name__ == "__main__":
    if len(sys.argv) in (5, 6) and sys.argv[1] == "exact-worker":
        exact_worker(*(Path(p) for p in sys.argv[2:]))
    else:
        main()
