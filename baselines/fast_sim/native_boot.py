"""Build a native C-kernel spec from scenario JSON without ABIDES agent objects.

``build_config`` pulls pandas + ExchangeAgent construction (~0.25s+). The native
path only needs RNG streams drawn in the same order and an ``oracle_spec``
snapshot matching ``SparseMeanRevertingOracle`` after ``__init__``. This module
replicates that draw order with numpy alone so cold ``simulate`` stays fast on
the default native roster.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from fast_sim.native import _copy_random_state

_DATE_NS = 1_612_483_200_000_000_000  # 2021-02-05 UTC, matches pd.to_datetime("20210205").value
_NS_0930 = 34_200_000_000_000
_NS_1600 = 57_600_000_000_000
_NS_1S = 1_000_000_000
_STARTING_CASH = 10_000_000  # unused by native roster fields; kept for draw parity docs

_KIND = {
    "NoiseTrader": 1,
    "MarketMaker": 2,
    "ValueTrader": 3,
    "MomentumTrader": 4,
}


def scenario_supports_native(scenario: dict[str, Any]) -> bool:
    for agent_cfg in scenario.get("agent_configs") or []:
        if str(agent_cfg.get("agent_type")) not in _KIND:
            return False
    return True


def _draw_random_state() -> np.random.RandomState:
    return np.random.RandomState(
        seed=np.random.randint(low=0, high=2**32, dtype="uint64")
    )


def _oracle_kappa_per_ns(oracle_params: dict[str, Any]) -> float:
    kappa_per_s = float(oracle_params.get("kappa", 0.0))
    return kappa_per_s / 1e9 if kappa_per_s > 0 else 1.67e-16


def _oracle_megashock_rate_per_ns(oracle_params: dict[str, Any]) -> float:
    rate_per_s = float(oracle_params.get("jump_intensity", 0.0))
    return rate_per_s / 1e9 if rate_per_s > 0 else 2.77778e-18


def _interval_ns(params: dict[str, Any]) -> int:
    if "rebalance_interval_ns" in params:
        return int(max(1, int(params["rebalance_interval_ns"])))
    hz = float(params.get("arrival_rate_hz", 1.0))
    return int(max(1, int(1e9 / hz))) if hz > 0 else int(1e9)


def _stp_code(exchange_cfg: dict[str, Any]) -> int:
    if not bool(exchange_cfg.get("protocol_enforcement", False)):
        return 0
    policy = exchange_cfg.get("stp_policy")
    if policy == "cancel_oldest":
        return 2
    if policy:
        return 1
    return 0


def _latency_dict(latency_cfg: dict[str, Any] | None, random_state: np.random.RandomState) -> dict[str, Any]:
    if not latency_cfg:
        # generate_latency_model consumes one draw; native only needs a bound RS.
        return {
            "model": "deterministic",
            "min_ns": 0.0,
            "max_ns": 1e12,
            "mean_ns": 0.0,
            "sigma": 0.0,
            "mu": 0.0,
            "alpha": 1.5,
            "random_state": random_state,
        }
    params = latency_cfg.get("params", {})
    model = str(latency_cfg.get("model", "deterministic"))
    mean_ns = float(params.get("mean_ns", 0.0))
    return {
        "model": model,
        "min_ns": float(params.get("min_ns", 0.0)),
        "max_ns": float(params.get("max_ns", 1e12)),
        "mean_ns": mean_ns,
        "sigma": float(params.get("sigma", 0.0)),
        "mu": float(np.log(mean_ns)) if mean_ns > 0 else 0.0,
        "alpha": float(params.get("alpha", 1.5)),
        "random_state": random_state,
    }


def _agent_row(
    *,
    agent_id: int,
    agent_type: str,
    params: dict[str, Any],
    reference_price: int,
    random_state: np.random.RandomState,
) -> dict[str, Any]:
    kind = _KIND[agent_type]
    row: dict[str, Any] = {
        "id": agent_id,
        "kind": kind,
        "interval_ns": _interval_ns(params),
        "log_orders": True,
        "random_state": random_state,
        "order_size_mean": float(params.get("order_size_mean", 0.0) or 0.0),
        "order_size_std": float(params.get("order_size_std", 0.0) or 0.0),
        "price_offset_ticks": int(params.get("price_offset_ticks", 0) or 0),
        "reference_price": int(params.get("reference_price", reference_price) or reference_price),
        "spread_ticks": int(params.get("spread_ticks", 2) or 2),
        "depth_levels": int(params.get("depth_levels", 1) or 1),
        "size_per_level": int(params.get("size_per_level", 1) or 1),
        "threshold_ticks": int(params.get("threshold_ticks", 0) or 0),
        "sigma_n": float(params.get("sigma_n", 0.0) or 0.0),
        "lookback": int(params.get("lookback", 1) or 1),
    }
    if agent_type == "NoiseTrader":
        row["order_size_mean"] = float(params.get("order_size_mean", 10))
        row["order_size_std"] = float(params.get("order_size_std", 2))
        row["price_offset_ticks"] = int(params.get("price_offset_ticks", 5))
        row["reference_price"] = reference_price
    elif agent_type == "MarketMaker":
        # MarketMaker.__init__ clamps spread_ticks >= 2, depth/size >= 1.
        row["spread_ticks"] = int(max(2, int(params.get("spread_ticks", 2))))
        row["depth_levels"] = int(max(1, int(params.get("depth_levels", 3))))
        row["size_per_level"] = int(max(1, int(params.get("size_per_level", 10))))
        row["reference_price"] = reference_price
    elif agent_type == "ValueTrader":
        row["order_size_mean"] = float(params.get("order_size_mean", 25))
        row["threshold_ticks"] = int(params.get("threshold_ticks", 2))
        row["sigma_n"] = float(params.get("sigma_n", 1000.0))
    elif agent_type == "MomentumTrader":
        row["order_size_mean"] = float(params.get("order_size_mean", 15))
        row["threshold_ticks"] = int(params.get("threshold_ticks", 2))
        row["lookback"] = int(max(1, int(params.get("lookback", 5))))
    return row


def spec_from_scenario(scenario: dict[str, Any], seed: int | None = None) -> dict[str, Any]:
    """Return a ``run_native`` / ``stream_native`` spec; numpy-only side effects."""
    if not scenario_supports_native(scenario):
        raise ValueError("scenario roster is not native-supported")

    seed = int(scenario["seed"] if seed is None else seed)
    np.random.seed(seed)

    exchange_cfg = scenario["exchange_config"]
    oracle_params = scenario["oracle_config"].get("params", {})
    reference_price = int(oracle_params.get("initial_price", 100_000))
    horizon_ns = int(scenario["horizon_ns"])
    mkt_open = _DATE_NS + _NS_0930
    mkt_close = mkt_open + horizon_ns
    oracle_close = _DATE_NS + _NS_1600

    # --- oracle symbol RS, then SparseMeanRevertingOracle.__init__ megashock draws
    sym_rs = _draw_random_state()
    kappa = _oracle_kappa_per_ns(oracle_params)
    ms_lambda = _oracle_megashock_rate_per_ns(oracle_params)
    ms_mean = float(oracle_params.get("jump_sigma", 0.0)) or 1000.0
    ms_var = 50_000.0
    scheduled_jumps = []
    if oracle_params.get("scheduled_jump"):
        scheduled_jumps.append(
            {
                "time_ns": mkt_open + int(oracle_params["scheduled_jump"]["time_ns"]),
                "magnitude": int(oracle_params["scheduled_jump"]["magnitude"]),
                "consumed": False,
            }
        )

    # Matches abides SparseMeanRevertingOracle.__init__ megashock seeding.
    ms_time_delta = np.random.exponential(scale=1.0 / ms_lambda)
    mst = mkt_open + ms_time_delta
    msv = sym_rs.normal(loc=ms_mean, scale=math.sqrt(ms_var))
    msv = msv if sym_rs.randint(2) == 0 else -msv
    # Symbol RS is not advanced again during agent construction; snapshot it now.
    sym_rs_snap = _copy_random_state(sym_rs)

    _proto = bool(exchange_cfg.get("protocol_enforcement", False))
    _ack_delay = int(exchange_cfg.get("ack_delay_ns", 0)) if _proto else 0
    _compute_delay = int(exchange_cfg.get("compute_delay_ns", 0)) if _proto else 0

    # ExchangeAgent is constructed with log_orders=None → bool(None) is False in snapshot_native.
    roster: list[dict[str, Any]] = [
        {
            "id": 0,
            "kind": 0,
            "interval_ns": 0,
            "log_orders": False,
            "random_state": _draw_random_state(),
            "order_size_mean": 0.0,
            "order_size_std": 0.0,
            "price_offset_ticks": 0,
            "reference_price": reference_price,
            "spread_ticks": 2,
            "depth_levels": 1,
            "size_per_level": 1,
            "threshold_ticks": 0,
            "sigma_n": 0.0,
            "lookback": 1,
        }
    ]

    next_id = 1
    for agent_cfg in scenario["agent_configs"]:
        agent_type = str(agent_cfg["agent_type"])
        params = agent_cfg.get("params", {})
        for _ in range(int(agent_cfg["count"])):
            roster.append(
                _agent_row(
                    agent_id=next_id,
                    agent_type=agent_type,
                    params=params,
                    reference_price=reference_price,
                    random_state=_draw_random_state(),
                )
            )
            next_id += 1

    latency_cfg = scenario.get("latency_config")
    latency_rs = _draw_random_state()
    _ = _draw_random_state()  # random_state_kernel — consumed for draw-order parity

    # snapshot_native copies the global stream AFTER build_config finishes all
    # agent/latency/kernel draws — match that moment, not post-oracle-init.
    oracle_spec = {
        "mkt_close": int(oracle_close),
        "pt": 0.0,
        "pt_ns": int(mkt_open),
        "pt_is_float": False,
        "pv": float(reference_price),
        "r_bar": float(reference_price),
        "kappa": float(kappa),
        "fund_vol": float(oracle_params.get("sigma", 5e-5)),
        "ms_lambda": float(ms_lambda),
        "ms_mean": float(ms_mean),
        "ms_scale": float(math.sqrt(ms_var)),
        "mst": float(mst),
        "msv": float(msv),
        "jumps": [
            {
                "time_ns": int(j["time_ns"]),
                "magnitude": int(j["magnitude"]),
                "consumed": bool(j["consumed"]),
            }
            for j in scheduled_jumps
        ],
        "random_state": sym_rs_snap,
        "global_rs": _copy_random_state(np.random.mtrand._rand),
    }

    return {
        "start_time": int(_DATE_NS),
        "stop_time": int(mkt_close + _NS_1S),
        "mkt_open": int(mkt_open),
        "mkt_close": int(mkt_close),
        "default_computation_delay": 50,
        "exchange_computation_delay": int(_compute_delay),
        "pipeline_delay": int(_ack_delay),
        "stp": _stp_code(exchange_cfg),
        "last_trade": int(reference_price),
        "n_agents": len(roster),
        "agents": roster,
        "latency": _latency_dict(latency_cfg, latency_rs),
        "oracle": None,
        "oracle_spec": oracle_spec,
    }
