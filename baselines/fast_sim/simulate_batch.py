"""``simulate-batch`` CLI — parallel multi-scenario verb (family GB).

Sub-scenarios are independent (counters + RNG reset per call). Isolation is
preserved by running each sub in its own process. Aggregate ``wall_clock_sec``
is the host wall clock of the whole batch, so parallel execution raises the
ranked events/sec without changing any per-sub trace.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import time
import multiprocessing as mp
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Any, Optional

from fast_sim.simulate import simulate


def _run_one(args: tuple[str, str]) -> dict[str, Any]:
    sub_path, out_trace = args
    ev = simulate(sub_path, out_trace, require_message_ledger=True)
    return {
        "sub": pathlib.Path(sub_path).stem,
        "n_events": int(ev["n_events"]),
        "trace_sha256": ev["trace_sha256"],
        "peak_memory_bytes": int(ev.get("peak_memory_bytes", 0)),
        "gpu_seconds": float(ev.get("gpu_seconds", 0.0)),
    }


def _worker_count(n_subs: int) -> int:
    # Card cap is 4 CPUs; leave headroom for the parent.
    env = os.environ.get("FAST_SIM_BATCH_WORKERS")
    if env:
        return max(1, min(n_subs, int(env)))
    return max(1, min(n_subs, 4, os.cpu_count() or 1))


def simulate_batch(
    batch_dir: str | pathlib.Path, out_dir: str | pathlib.Path
) -> dict[str, Any]:
    batch_dir = pathlib.Path(batch_dir)
    out_dir = pathlib.Path(out_dir)
    subs = sorted(p for p in batch_dir.glob("*.json"))
    if not subs:
        raise SystemExit(f"simulate-batch: no sub-scenarios (*.json) found in {batch_dir}")

    jobs = [(str(p), str(out_dir / p.stem / "trace.parquet")) for p in subs]
    workers = _worker_count(len(jobs))

    t0 = time.perf_counter()
    results: list[dict[str, Any]] = []
    if workers == 1 or len(jobs) == 1:
        for job in jobs:
            results.append(_run_one(job))
    else:
        # spawn: each child applies patches and resets counters independently.
        ctx_results: dict[str, dict[str, Any]] = {}
        ctx = mp.get_context("spawn")
        with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as pool:
            futs = {pool.submit(_run_one, job): job[0] for job in jobs}
            for fut in as_completed(futs):
                ctx_results[pathlib.Path(futs[fut]).stem] = fut.result()
        results = [ctx_results[p.stem] for p in subs]
    wall_clock_sec = time.perf_counter() - t0

    total_events = sum(int(r["n_events"]) for r in results)
    peak_memory_bytes = max((int(r.get("peak_memory_bytes", 0)) for r in results), default=0)
    gpu_seconds = sum(float(r.get("gpu_seconds", 0.0)) for r in results)
    per_scenario = [
        {"sub": r["sub"], "n_events": int(r["n_events"]), "trace_sha256": r["trace_sha256"]}
        for r in results
    ]
    batch_events = {
        "n_scenarios": len(subs),
        "total_events": total_events,
        "wall_clock_sec": float(wall_clock_sec),
        "events_per_sec": float(total_events / wall_clock_sec) if wall_clock_sec > 0 else 0.0,
        "peak_memory_bytes": peak_memory_bytes,
        "gpu_seconds": gpu_seconds,
        "per_scenario": per_scenario,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "batch_events.json").write_text(json.dumps(batch_events, indent=2) + "\n")
    return batch_events


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="fast_sim.simulate_batch")
    ap.add_argument(
        "verb", nargs="?", default="simulate-batch", choices=["simulate-batch"]
    )
    ap.add_argument("--batch-dir", required=True, help="directory of <sub>.json sub-scenarios")
    ap.add_argument(
        "--out-dir",
        required=True,
        help="output directory for per-sub results + aggregate",
    )
    args = ap.parse_args(argv)
    batch_events = simulate_batch(args.batch_dir, args.out_dir)
    print(json.dumps(batch_events))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
