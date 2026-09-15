"""``simulate-batch`` CLI — the batched multi-scenario submission verb (BatchMarketSim / family GB).

Runs N independent single-market scenarios in one process invocation and writes one output
subdirectory per sub-scenario plus a batch-level aggregate. The organizer baseline runs them
SERIALLY (a loop over the single-scenario ``simulate``); a GPU submission is expected to batch them
in parallel for a higher *aggregate* events/sec. Correctness is per-sub-scenario — each output must
reproduce that scenario's ISOLATED reference — so a batch that leaks book / agent / RNG state between
markets produces a divergent sub-trace and fails the isolation gate.

Why the serial baseline reproduces each isolated reference exactly: ``simulate`` resets ABIDES's
global id counters and re-seeds ``np.random`` from the scenario seed on every call, so each sub-run is
independent of batch order and byte-identical to running that scenario alone.

Contract (Docker entrypoint form; track-owned, no shared-infra change):

    simulate-batch --batch-dir /input/scenarios --out-dir /output

    /input/scenarios/<sub>.json   ->  /output/<sub>/trace.parquet
                                      /output/<sub>/message_trace.parquet
                                      /output/<sub>/events.json
    /output/batch_events.json     aggregate: {n_scenarios, total_events, wall_clock_sec,
                                  events_per_sec (= total_events / batch wall_clock), per_scenario[]}
"""

from __future__ import annotations

import argparse
import json
import pathlib
import time
from typing import Any, Optional

from abides_fork.simulate import simulate


def simulate_batch(
    batch_dir: str | pathlib.Path, out_dir: str | pathlib.Path
) -> dict[str, Any]:
    """Run every ``*.json`` sub-scenario in ``batch_dir`` and write per-sub outputs + an aggregate.

    Returns the batch aggregate dict. Sub-scenarios are processed in sorted filename order; each
    ``simulate`` call is self-contained (counter reset + RNG re-seed), so the per-sub outputs do not
    depend on batch order — the property the isolation gate checks.
    """
    batch_dir = pathlib.Path(batch_dir)
    out_dir = pathlib.Path(out_dir)
    subs = sorted(p for p in batch_dir.glob("*.json"))
    if not subs:
        raise SystemExit(
            f"simulate-batch: no sub-scenarios (*.json) found in {batch_dir}"
        )

    per_scenario: list[dict[str, Any]] = []
    total_events = 0
    peak_memory_bytes = 0
    gpu_seconds = 0.0
    t0 = time.perf_counter()
    for sub_path in subs:
        sub = sub_path.stem
        ev = simulate(sub_path, out_dir / sub / "trace.parquet")
        total_events += int(ev["n_events"])
        peak_memory_bytes = max(peak_memory_bytes, int(ev.get("peak_memory_bytes", 0)))
        gpu_seconds += float(ev.get("gpu_seconds", 0.0))
        per_scenario.append(
            {
                "sub": sub,
                "n_events": int(ev["n_events"]),
                "trace_sha256": ev["trace_sha256"],
            }
        )
    wall_clock_sec = time.perf_counter() - t0

    batch_events = {
        "n_scenarios": len(subs),
        "total_events": total_events,
        "wall_clock_sec": float(wall_clock_sec),
        "events_per_sec": float(total_events / wall_clock_sec)
        if wall_clock_sec > 0
        else 0.0,
        # Batch telemetry: peak memory is the max across the serial sub-runs (a batched GPU submission
        # self-reports its own concurrent peak); gpu_seconds sums the per-sub GPU time.
        "peak_memory_bytes": peak_memory_bytes,
        "gpu_seconds": gpu_seconds,
        "per_scenario": per_scenario,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "batch_events.json").write_text(
        json.dumps(batch_events, indent=2) + "\n"
    )
    return batch_events


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="abides_fork.simulate_batch")
    # Accept an optional leading "simulate-batch" verb so the Docker `<img> simulate-batch ...`
    # form and the `python -m abides_fork.simulate_batch ...` form both work.
    ap.add_argument(
        "verb", nargs="?", default="simulate-batch", choices=["simulate-batch"]
    )
    ap.add_argument(
        "--batch-dir", required=True, help="directory of <sub>.json sub-scenarios"
    )
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
