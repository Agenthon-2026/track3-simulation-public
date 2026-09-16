"""``simulate`` CLI — Track 3 submission verb for the fast_sim participant image.

Usage (Docker harness contract)::

    simulate --config /input/scenario.json --out /output/trace.parquet
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import resource
import time
from typing import Any, Optional

from abides_fork.scenario_io import read_scenario
from fast_sim.engine import run_scenario
from fast_sim.native import write_parquet


def _sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _peak_rss_bytes() -> int:
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024


def simulate(
    config_path: str | pathlib.Path,
    out_path: str | pathlib.Path,
    seed: Optional[int] = None,
    *,
    require_message_ledger: bool = False,
) -> dict[str, Any]:
    """Run the scenario, write ``trace.parquet`` + sidecars, return metadata."""
    scenario = json.loads(read_scenario(config_path))
    # The CLI receives scenario JSON, not the organizer's card. Only the documented
    # throughput-scale single-scenario default permits omission. Unknown families
    # retain the ledger; batch callers always require it independently of family.
    emit_ledger = require_message_ledger or scenario.get("scenario_family") != "throughput-scale"
    if seed is not None:
        scenario = {**scenario, "seed": int(seed)}

    t0 = time.perf_counter()
    out_path = pathlib.Path(out_path)
    msg_out = out_path.parent / "message_trace.parquet"
    trace, message_trace, _end_state = run_scenario(scenario, (out_path, msg_out if emit_ledger else None))
    wall_clock_sec = time.perf_counter() - t0
    peak_memory_bytes = _peak_rss_bytes()

    out_path = pathlib.Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_parquet(trace, out_path)
    if emit_ledger:
        write_parquet(message_trace, msg_out)

    n_events = int(trace.num_rows if hasattr(trace, "num_rows") else len(trace))
    events = {
        "scenario_id": str(scenario["scenario_id"]),
        "seed": int(scenario["seed"]),
        "n_events": n_events,
        "wall_clock_sec": float(wall_clock_sec),
        "events_per_sec": float(n_events / wall_clock_sec) if wall_clock_sec > 0 else 0.0,
        "trace_sha256": _sha256(out_path),
        "n_messages": int(
            message_trace.num_rows if hasattr(message_trace, "num_rows") else len(message_trace)
        ),
        "peak_memory_bytes": peak_memory_bytes,
        "gpu_seconds": 0.0,
    }
    if emit_ledger:
        events["message_trace_sha256"] = _sha256(msg_out)
    (out_path.parent / "events.json").write_text(json.dumps(events, indent=2) + "\n")
    return events


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="fast_sim.simulate")
    ap.add_argument("verb", nargs="?", default="simulate", choices=["simulate"])
    ap.add_argument("--config", required=True, help="path to scenario.json")
    ap.add_argument("--out", required=True, help="output path for trace.parquet")
    ap.add_argument("--seed", type=int, default=None, help="override scenario seed")
    ap.add_argument("--require-message-ledger", action="store_true", help="Emit the optional throughput ledger as well")
    args = ap.parse_args(argv)
    events = simulate(args.config, args.out, args.seed, require_message_ledger=args.require_message_ledger)
    print(json.dumps(events))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
