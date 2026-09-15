"""``simulate`` CLI — the Track 3 submission verb for the abides_fork baseline.

Usage (also the Docker entrypoint contract):

    python -m abides_fork.simulate --config /input/scenario.json --out /output/trace.parquet [--seed N]
    # or, matching the verb form the harness uses:
    simulate --config /input/scenario.json --out /output/trace.parquet

Runs the scenario through ABIDES (driven by the ``abides_fork`` agents), then writes the
canonical ``trace.parquet`` (7 columns, Snappy) and ``events.json`` next to it. ABIDES's
global id counters are reset first so the run is deterministic regardless of process reuse.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import resource
import time
from typing import Any, Optional

from abides_core import abides
from abides_core.message import Message
from abides_markets.orders import Order

from abides_fork.config import build_config
from abides_fork.scenario_io import read_scenario
from abides_fork.trace import extract_message_trace, extract_trace


def reset_abides_counters() -> None:
    """Reset ABIDES's class-level id counters so a run is deterministic in-process."""
    Order._order_id_counter = 0
    # Name-mangled private class var on Message.
    setattr(Message, "_Message__message_id_counter", 1)


def _sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _peak_rss_bytes() -> int:
    """Peak resident set size of this process, in bytes. The official image is Linux, where
    ``ru_maxrss`` is reported in KiB (macOS reports bytes, irrelevant for the scored path)."""
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024


def simulate(
    config_path: str | pathlib.Path,
    out_path: str | pathlib.Path,
    seed: Optional[int] = None,
) -> dict[str, Any]:
    """Run the scenario, write ``trace.parquet`` + ``events.json``, return the metadata."""
    scenario = json.loads(read_scenario(config_path))
    if seed is not None:
        scenario = {**scenario, "seed": int(seed)}

    reset_abides_counters()
    config = build_config(scenario)
    t0 = time.perf_counter()
    end_state = abides.run(config)
    wall_clock_sec = time.perf_counter() - t0
    peak_memory_bytes = _peak_rss_bytes()

    trace = extract_trace(end_state)
    message_trace = extract_message_trace(end_state)

    out_path = pathlib.Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    trace.to_parquet(out_path, compression="snappy", index=False)
    # v2 companion: the message-level enriched trace for the latency/event-order/wakeup/
    # reactive/protocol gates. Written next to trace.parquet as message_trace.parquet.
    msg_out = out_path.parent / "message_trace.parquet"
    message_trace.to_parquet(msg_out, compression="snappy", index=False)

    n_events = int(len(trace))
    events = {
        "scenario_id": str(scenario["scenario_id"]),
        "seed": int(scenario["seed"]),
        "n_events": n_events,
        "wall_clock_sec": float(wall_clock_sec),
        "events_per_sec": float(n_events / wall_clock_sec)
        if wall_clock_sec > 0
        else 0.0,
        "trace_sha256": _sha256(out_path),
        "n_messages": int(len(message_trace)),
        "message_trace_sha256": _sha256(msg_out),
        # Phase-4 secondary-diagnostic telemetry (memory-efficiency + efficiency). Self-reported like
        # wall_clock_sec, and the ranked primary metric is untouched. The awards pipeline does not
        # trust these: throughput/run_unit.py measures GPU seconds via NVML and peak memory from the
        # container cgroup, and a unit without those host measurements is flagged self-reported so it
        # cannot win a telemetry-dependent award. The baseline is CPU-only, so gpu_seconds is 0.0.
        "peak_memory_bytes": peak_memory_bytes,
        "gpu_seconds": 0.0,
    }
    (out_path.parent / "events.json").write_text(json.dumps(events, indent=2) + "\n")
    return events


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="abides_fork.simulate")
    # Accept an optional leading "simulate" verb so the Docker `<img> simulate ...`
    # form and the `python -m abides_fork.simulate ...` form both work.
    ap.add_argument("verb", nargs="?", default="simulate", choices=["simulate"])
    ap.add_argument("--config", required=True, help="path to scenario.json")
    ap.add_argument("--out", required=True, help="output path for trace.parquet")
    ap.add_argument("--seed", type=int, default=None, help="override scenario seed")
    args = ap.parse_args(argv)
    events = simulate(args.config, args.out, args.seed)
    print(json.dumps(events))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
