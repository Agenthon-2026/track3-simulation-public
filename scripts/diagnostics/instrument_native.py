#!/usr/bin/env python3
"""Generate isolated native diagnostics without changing the participant engine.

The generated sibling keeps the production methods intact and adds coarse stage
entrypoints. Stage clocks never run inside the event loop or per-row loops. A
separate profile mode enables Cython call attribution; its timings are explicitly
unsuitable for throughput comparisons. Both modes return unchanged Arrow tables.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE = ROOT / "baselines/fast_sim/_native.pyx"


def replace_once(text: str, old: str, new: str) -> str:
    """Refuse source drift instead of silently instrumenting the wrong boundary."""
    count = text.count(old)
    if count != 1:
        raise ValueError(f"Expected one diagnostic anchor, found {count}: {old!r}")
    return text.replace(old, new, 1)


def boundary(text: str, anchor: str, name: str) -> str:
    return replace_once(
        text,
        anchor,
        f'        metrics["{name}"] = _diagnostic_clock() - phase_start\n'
        "        phase_start = _diagnostic_clock()\n" + anchor,
    )


def methods(source: str, class_name: str, prefix: str) -> str:
    class_start = source.index(f"cdef class {class_name}:\n")
    array_start = source.index("    def to_arrays(self):\n", class_start)
    arrow_start = source.index("    def to_arrow(self):\n", array_start)
    frame_start = source.index("    def to_dataframe(self):\n", arrow_start)
    arrays = source[array_start:arrow_start]
    arrow = source[arrow_start:frame_start]
    arrays = replace_once(
        arrays,
        "    def to_arrays(self):\n",
        "    def to_arrays_diagnostic(self, metrics):\n"
        "        phase_start = _diagnostic_clock()\n",
    )
    if class_name == "CTrace":
        arrays = boundary(arrays, "        n = n_order + n_quote\n", "prepare_columns")
        arrays = boundary(
            arrays, "        idx = _stable_lexsort(t_all, oid_all)\n", "concatenate"
        )
        arrays = boundary(arrays, "        return {\n", "sort")
    else:
        arrays = boundary(arrays, "        for i in range(n):\n", "allocate")
        arrays = boundary(arrays, "        keep = seq >= 0\n", "row_materialize")
        arrays = boundary(
            arrays,
            '        order = np.argsort(seq[keep], kind="stable")\n',
            "keep_mask",
        )
        arrays = boundary(arrays, "        return {\n", "sort")
    arrays = replace_once(arrays, "        return {\n", "        result = {\n")
    arrays = arrays.rstrip() + (
        '\n        metrics["column_gather"] = _diagnostic_clock() - phase_start\n'
        "        return result\n\n"
    )
    # Reuse the original Arrow conversion verbatim, including its imports and
    # empty-table branch. Only the array producer moves to the timed wrapper.
    arrow = replace_once(
        arrow,
        "    def to_arrow(self):\n",
        "    def _diagnostic_arrow_from_arrays(self, a):\n",
    )
    arrow = replace_once(arrow, "        a = self.to_arrays()\n", "")
    wrapper = f'''    def to_arrow_diagnostic(self, metrics):
        """Time {prefix} conversion; nested array phases are not additive to totals."""
        phases = {{}}
        started = _diagnostic_clock()
        a = self.to_arrays_diagnostic(phases)
        metrics["to_arrays_sec"] = _diagnostic_clock() - started
        metrics["array_stages_sec"] = phases
        started = _diagnostic_clock()
        result = self._diagnostic_arrow_from_arrays(a)
        metrics["arrow_materialize_sec"] = _diagnostic_clock() - started
        return result

'''
    return arrays + arrow + wrapper


ENTRYPOINT = '''

def diagnostic_info():
    """Describe instrumentation identity and the interpretation of nested clocks."""
    return {
        "mode": _DIAGNOSTIC_MODE,
        "source_sha256": _DIAGNOSTIC_SOURCE_SHA256,
        "rankable": False,
        "storage_mode": "buffered",
        "empty_array_stages": "an empty mapping means to_arrays returned None before materialization",
        "phase_timings_credible": _DIAGNOSTIC_MODE == "stage",
        "clock_scope": "coarse boundaries only; no per-event or per-row clocks",
        "imports": "array imports are inside to_arrays and its first phase; Arrow imports are inside arrow_materialize; module import is outside this entrypoint",
        "accounting": "seconds are top-level phases plus inclusive total; trace/ledger values break down their respective parent; array_stages_sec is nested inside to_arrays_sec and must not be added again",
        "profile_warning": "profile mode changes every instrumented call; use call counts and attribution only, never compare its wall times with stage or production runs",
    }


def run_native_sim_diagnostic(spec):
    """Return (trace, ledger, metrics); preserve the original tables and RNG order."""
    cdef NativeSim sim
    metrics = diagnostic_info()
    seconds = {}
    metrics["seconds"] = seconds
    metrics["trace"] = {}
    metrics["ledger"] = {}
    overall = _diagnostic_clock()
    started = _diagnostic_clock()
    sim = NativeSim()
    seconds["construct_sec"] = _diagnostic_clock() - started
    started = _diagnostic_clock()
    sim.setup(spec)
    seconds["setup_sec"] = _diagnostic_clock() - started
    started = _diagnostic_clock()
    sim.run_loop()
    seconds["run_loop_sec"] = _diagnostic_clock() - started
    started = _diagnostic_clock()
    trace = sim.trace.to_arrow_diagnostic(metrics["trace"])
    seconds["trace_to_arrow_sec"] = _diagnostic_clock() - started
    started = _diagnostic_clock()
    ledger = sim.ledger.to_arrow_diagnostic(metrics["ledger"])
    seconds["ledger_to_arrow_sec"] = _diagnostic_clock() - started
    seconds["total_sec"] = _diagnostic_clock() - overall
    metrics["buffers"] = {
        "trace_order_rows": sim.trace.n_o,
        "trace_quote_rows": sim.trace.n_q,
        "ledger_stored_rows": sim.ledger.n,
        "queued_events_after_stop": sim.q.n,
        "queue_capacity": sim.q.cap,
    }
    return trace, ledger, metrics
'''


def generate(source: str, mode: str) -> str:
    if mode not in {"stage", "profile"}:
        raise ValueError(f"Unknown diagnostic mode: {mode}")
    source_hash = hashlib.sha256(source.encode()).hexdigest()
    trace = methods(source, "CTrace", "trace")
    ledger = methods(source, "CLedger", "ledger")
    generated = replace_once(
        source, "cdef class CLedger:\n", trace + "\ncdef class CLedger:\n"
    )
    generated = replace_once(
        generated, "cdef class CPriceLevel:\n", ledger + "\ncdef class CPriceLevel:\n"
    )
    generated = replace_once(
        generated,
        "from libc.math cimport exp, floor, log, sqrt\n",
        "from time import perf_counter as _diagnostic_clock\n"
        f'_DIAGNOSTIC_MODE = "{mode}"\n'
        f'_DIAGNOSTIC_SOURCE_SHA256 = "{source_hash}"\n\n'
        "from libc.math cimport exp, floor, log, sqrt\n",
    )
    return (
        f"# cython: profile={'True' if mode == 'profile' else 'False'}\n"
        + generated
        + ENTRYPOINT
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--out", "--output", dest="output", type=Path, required=True)
    parser.add_argument("--mode", choices=["stage", "profile"], default="stage")
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()
    if args.source.resolve() == args.output.resolve():
        parser.error(
            "Diagnostic output must be a sibling; never overwrite production source"
        )
    manifest_path = args.manifest or args.output.with_suffix(".json")
    if manifest_path.resolve() in {args.source.resolve(), args.output.resolve()}:
        parser.error("Manifest must not overwrite production source or generated code")
    source_bytes = args.source.read_bytes()
    source = source_bytes.decode("utf-8")
    generated = generate(source, args.mode)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(generated)
    manifest = {
        "kind": "isolated-native-diagnostic",
        "mode": args.mode,
        "rankable": False,
        "source": str(args.source),
        "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "output": str(args.output),
        "output_sha256": hashlib.sha256(generated.encode()).hexdigest(),
        "generator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "entrypoint": "run_native_sim_diagnostic(spec) -> (trace, ledger, metrics)",
        "profile_timings_comparable": False,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
