"""From-scratch C kernel entry. Hybrid ``Kernel.run`` stays the fallback.

``native_supported`` is true when the roster is Exchange + the four Track-3
scheduled agents. ``run_native`` snapshots params / RNG streams from the
ABIDES objects ``build_config`` already constructed (so seed order matches
the references) and then runs a C event loop that never calls ``Kernel.run``.
"""

from __future__ import annotations

import os
from typing import Any

# Wired as default after s001 120/84/74/0/604/664 matched bit-exact.
NATIVE_IS_DEFAULT = True

_SUPPORTED = {"ExchangeAgent", "NoiseTrader", "MarketMaker", "ValueTrader", "MomentumTrader"}


def native_flag() -> str:
    return os.environ.get("FAST_SIM_NATIVE", "auto").strip().lower()


def native_supported(agents: list[Any]) -> bool:
    if not agents:
        return False
    if type(agents[0]).__name__ != "ExchangeAgent":
        return False
    for agent in agents[1:]:
        if type(agent).__name__ not in _SUPPORTED:
            return False
    return True


def should_use_native(agents: list[Any]) -> bool:
    flag = native_flag()
    if flag in ("0", "false", "hybrid", "off"):
        return False
    if not native_supported(agents):
        return False
    if flag in ("1", "true", "native", "on"):
        return True
    return NATIVE_IS_DEFAULT


def _kind(agent: Any) -> int:
    name = type(agent).__name__
    if name == "ExchangeAgent":
        return 0
    if name == "NoiseTrader":
        return 1
    if name == "MarketMaker":
        return 2
    if name == "ValueTrader":
        return 3
    if name == "MomentumTrader":
        return 4
    raise TypeError(f"unsupported agent {name}")


def _stp_code(policy: Any) -> int:
    if policy == "cancel_oldest":
        return 2
    if policy:
        return 1
    return 0


def _latency_spec(model: Any) -> dict[str, Any]:
    name = getattr(model, "_model", "deterministic")
    return {
        "model": str(name),
        "min_ns": float(getattr(model, "_min_ns", 0.0)),
        "max_ns": float(getattr(model, "_max_ns", 1e12)),
        "mean_ns": float(getattr(model, "_mean_ns", 0.0)),
        "sigma": float(getattr(model, "_sigma", 0.0)),
        "mu": float(getattr(model, "_mu", 0.0)),
        "alpha": float(getattr(model, "_alpha", 1.5)),
        "random_state": model.random_state,
    }


def _copy_random_state(rs: Any) -> Any:
    """Clone a RandomState (or the global stream) so C and Python do not share it."""
    import numpy as np

    clone = np.random.RandomState()
    clone.set_state(rs.get_state() if hasattr(rs, "get_state") else np.random.get_state())
    return clone


def _oracle_spec(oracle: Any) -> dict[str, Any] | None:
    """Snapshot SparseMeanRevertingOracle after ``__init__`` (first megashock done).

    Copies both RNG streams. Tags ``pt`` as int vs float64 so C can do
    ``(t1 - t2)`` in integer nanoseconds when Python stored ints — float64
    around 2021-ns (~1.6e18) cannot represent a 1 ns step.
    """
    if oracle is None:
        return None
    import math

    import numpy as np

    symbols = getattr(oracle, "symbols", {})
    if not symbols:
        return None
    sym_key = "ABM" if "ABM" in symbols else next(iter(symbols.keys()))
    sym = symbols[sym_key]
    pt, pv = oracle.r.get(sym_key, (0, 0))
    ms_list = oracle.megashocks.get(sym_key)
    ms = ms_list[-1] if ms_list else {"MegashockTime": 0.0, "MegashockValue": 0.0}
    jumps = []
    for jump in sym.get("scheduled_jumps") or []:
        jumps.append(
            {
                "time_ns": int(jump["time_ns"]),
                "magnitude": int(jump["magnitude"]),
                "consumed": bool(jump.get("_consumed")),
            }
        )
    pt_is_float = isinstance(pt, (float, np.floating))
    return {
        "mkt_close": int(oracle.mkt_close),
        "pt": float(pt) if pt_is_float else 0.0,
        "pt_ns": 0 if pt_is_float else int(pt),
        "pt_is_float": bool(pt_is_float),
        "pv": float(pv),
        "r_bar": float(sym["r_bar"]),
        "kappa": float(sym["kappa"]),
        "fund_vol": float(sym["fund_vol"]),
        "ms_lambda": float(sym["megashock_lambda_a"]),
        "ms_mean": float(sym["megashock_mean"]),
        "ms_scale": float(math.sqrt(sym["megashock_var"])),
        "mst": float(ms["MegashockTime"]),
        "msv": float(ms["MegashockValue"]),
        "jumps": jumps,
        "random_state": _copy_random_state(sym["random_state"]),
        "global_rs": _copy_random_state(np.random.mtrand._rand),
    }


def snapshot_native(config: dict[str, Any]) -> dict[str, Any]:
    """Pull scalar fields + RNG objects off the ABIDES config (no Kernel)."""
    agents = config["agents"]
    exchange = agents[0]
    oracle = config.get("custom_properties", {}).get("oracle") or getattr(
        exchange, "oracle", None
    )
    mkt_open = int(exchange.mkt_open)
    mkt_close = int(exchange.mkt_close)
    sym_key = "ABM"
    if oracle is not None and getattr(oracle, "symbols", None):
        sym_key = "ABM" if "ABM" in oracle.symbols else next(iter(oracle.symbols.keys()))
    last_trade = int(oracle.get_daily_open_price(sym_key, mkt_open)) if oracle is not None else 100000
    roster = []
    for agent in agents:
        k = _kind(agent)
        row: dict[str, Any] = {
            "id": int(agent.id),
            "kind": k,
            "interval_ns": int(getattr(agent, "interval_ns", 0) or 0),
            "log_orders": bool(getattr(agent, "log_orders", True)),
            "random_state": getattr(agent, "random_state", None),
            "order_size_mean": float(getattr(agent, "order_size_mean", 0.0) or 0.0),
            "order_size_std": float(getattr(agent, "order_size_std", 0.0) or 0.0),
            "price_offset_ticks": int(getattr(agent, "price_offset_ticks", 0) or 0),
            "reference_price": int(getattr(agent, "reference_price", last_trade) or last_trade),
            "spread_ticks": int(getattr(agent, "spread_ticks", 2) or 2),
            "depth_levels": int(getattr(agent, "depth_levels", 1) or 1),
            "size_per_level": int(getattr(agent, "size_per_level", 1) or 1),
            "threshold_ticks": int(getattr(agent, "threshold_ticks", 0) or 0),
            "sigma_n": float(getattr(agent, "sigma_n", 0.0) or 0.0),
            "lookback": int(getattr(agent, "lookback", 1) or 1),
        }
        roster.append(row)
    return {
        "start_time": int(config["start_time"]),
        "stop_time": int(config["stop_time"]),
        "mkt_open": mkt_open,
        "mkt_close": mkt_close,
        "default_computation_delay": int(config.get("default_computation_delay", 50)),
        "exchange_computation_delay": int(getattr(exchange, "computation_delay", 0) or 0),
        "pipeline_delay": int(getattr(exchange, "pipeline_delay", 0) or 0),
        "stp": _stp_code(getattr(exchange, "stp_policy", None)),
        "last_trade": last_trade,
        "n_agents": len(agents),
        "agents": roster,
        "latency": _latency_spec(config["agent_latency_model"]),
        "oracle": oracle,
        "oracle_spec": _oracle_spec(oracle),
    }


def as_pandas(obj: Any) -> Any:
    """Validate / compare path only — timed ``simulate`` writes Arrow tables.

    Default ``Table.to_pandas()`` promotes nullable int64 to float64 and
    rounds nanosecond timestamps. Map those columns to pandas ``Int64``.
    """
    if obj is None:
        return obj
    try:
        import pyarrow as pa
        import pandas as pd
    except ImportError:
        pa = None
        pd = None
    if pa is not None and isinstance(obj, pa.Table):
        def _mapper(typ):
            if pa.types.is_int64(typ):
                return pd.Int64Dtype()
            if pa.types.is_int32(typ):
                return pd.Int32Dtype()
            if pa.types.is_string(typ) or pa.types.is_large_string(typ):
                return pd.StringDtype()
            return None

        return obj.to_pandas(types_mapper=_mapper)
    to_pd = getattr(obj, "to_pandas", None)
    if callable(to_pd) and not hasattr(obj, "iloc"):
        return to_pd()
    return obj


def write_parquet(obj: Any, path: Any) -> None:
    """Write a pyarrow Table (or pandas DataFrame).

    Event traces stay a raw Arrow write. Message ledgers have nullable
    ``t_send_ns`` / ``order_id`` / ``causal_parent``: ``pq.write_table``
    emits no pandas metadata, so the official scorer's ``pd.read_parquet``
    (no ``dtype_backend``) promotes those columns to float64 and rounds
    19-digit ns timestamps. That breaks ``t_recv - t_send == latency_ns``
    and causal order on every batch sub even though the in-memory ledger
    is self-consistent. The shared sink attaches nullable pandas metadata
    from an empty table, preserving integers without converting every row.
    """
    from pathlib import Path

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    from fast_sim.streaming import ParquetSink, StoredTable

    if isinstance(obj, StoredTable):
        if obj.path.resolve() != path.resolve():
            raise ValueError("streamed output must be written at its final destination")
        return
    schema = getattr(obj, "schema", None)
    if schema is not None and hasattr(obj, "to_batches"):
        sink = ParquetSink(path, obj.slice(0, 0))
        try:
            sink.write(obj)
        finally:
            sink.close()
        return
    obj.to_parquet(path, compression="snappy", index=False)


def run_native_from_spec(
    spec: dict[str, Any], output_paths=None, *, agents: Any = None
) -> tuple[Any, Any, dict[str, Any]]:
    """Run from a pre-built native spec (light boot or ``snapshot_native``)."""
    from fast_sim._native import run_native_sim

    # Dispatch by workload parameters, never public scenario IDs. This estimate
    # chooses storage only; it does not change the scenario or its event count.
    placements = sum(
        max(0, spec["mkt_close"] - spec["mkt_open"]) // max(1, a["interval_ns"])
        * (2 * a["depth_levels"] if a["kind"] == 2 else 1)
        for a in spec["agents"]
        if a["kind"]
    )
    if output_paths is not None and placements > 1_000_000:
        trace, msg = stream_native(spec, output_paths)
    else:
        trace, msg = run_native_sim(spec)
    return trace, msg, {"col_trace": None, "col_ledger": None, "agents": agents or []}


def run_native(config: dict[str, Any], output_paths=None) -> tuple[Any, Any, dict[str, Any]]:
    return run_native_from_spec(
        snapshot_native(config), output_paths, agents=config["agents"]
    )


def stream_native(spec, output_paths, chunk_rows=524288):
    """Classify final executions, then replay into bounded Parquet row groups.

    Classification binds MT19937 copies from the Python RandomStates and does not
    advance them, so the replay pass can reuse ``spec`` without ``deepcopy``.
    """
    from fast_sim._native import (
        CTrace, CLedger, classify_native_executions, stream_native_sim,
    )
    from fast_sim.streaming import ParquetSink, UnstoredLedger

    partial, executions = classify_native_executions(spec)
    trace = ParquetSink(output_paths[0], CTrace().to_arrow())
    try:
        ledger = (
            ParquetSink(output_paths[1], CLedger().to_arrow())
            if output_paths[1] is not None else UnstoredLedger()
        )
        try:
            stream_native_sim(spec, partial, executions, trace, ledger, chunk_rows)
        finally:
            ledger_table = ledger.close()
    finally:
        trace_table = trace.close()
    return trace_table, ledger_table
