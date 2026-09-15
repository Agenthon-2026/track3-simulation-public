"""Build an ABIDES config dict from a Track 3 ``scenario.json``.

ABIDES supplies the kernel, exchange, order book, and matching engine. This maps the
scenario's ``oracle_config``, ``latency_config``, and ``agent_configs`` onto a config that
``abides_core.abides.run`` consumes; agent random states are drawn in a fixed order from a
seeded global RNG, so a run is deterministic for a given scenario seed (the structure
mirrors the upstream ``rmsc04`` config).

Two mapping conventions are worth noting:
  - ``oracle_config.params.kappa`` is interpreted as a per-SECOND OU mean-reversion rate and
    converted to ABIDES's per-nanosecond rate (see ``_oracle_kappa_per_ns``); ``sigma`` maps
    to the OU ``fund_vol``. ``dt_ns`` / ``noise_type`` are not consumed by ABIDES's OU oracle.
  - ``latency_config`` is realized per message by ``ScenarioLatencyModel`` (uniform or
    log-normal, clipped to ``[min_ns, max_ns]``), replacing ABIDES's default line-distance
    latency. ``exchange_config`` is read only for the (cosmetic) symbol.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from abides_core.latency_model import LatencyModel
from abides_core.utils import str_to_ns
from abides_markets.agents import ExchangeAgent
from abides_markets.oracles import SparseMeanRevertingOracle
from abides_markets.utils import generate_latency_model

from abides_fork.agents import AGENT_REGISTRY

_DATE = "20210205"
_STARTING_CASH = 10_000_000  # cents


def _draw_random_state() -> np.random.RandomState:
    """One RandomState seeded from the global RNG (fixed draw order = determinism)."""
    return np.random.RandomState(
        seed=np.random.randint(low=0, high=2**32, dtype="uint64")
    )


def _oracle_kappa_per_ns(oracle_params: dict[str, Any]) -> float:
    """Convert the scenario's per-second OU mean-reversion rate to ABIDES's per-ns rate.

    ABIDES's ``SparseMeanRevertingOracle`` applies ``exp(-kappa * d)`` with ``d`` in
    nanoseconds, so a scenario ``kappa`` of e.g. 0.05 fed in directly would underflow and
    pin the fundamental to a constant. We read it as per-second and divide by 1e9. Absent
    kappa falls back to the gentle upstream rmsc04 default (already per-nanosecond).
    """
    kappa_per_s = float(oracle_params.get("kappa", 0.0))
    return kappa_per_s / 1e9 if kappa_per_s > 0 else 1.67e-16


def _oracle_megashock_rate_per_ns(oracle_params: dict[str, Any]) -> float:
    """Convert the scenario's per-second jump (megashock) intensity to ABIDES's per-ns rate.

    ABIDES draws megashock inter-arrivals at rate ``megashock_lambda_a`` per nanosecond, so a
    scenario ``jump_intensity`` (e.g. 2.0) fed in directly fires megashocks every ~0.5ns -- a
    runaway price process. We read it as a per-SECOND jump rate and divide by 1e9. Absent or
    zero jumps fall back to the gentle upstream rmsc04 default (effectively no megashocks).
    """
    rate_per_s = float(oracle_params.get("jump_intensity", 0.0))
    return rate_per_s / 1e9 if rate_per_s > 0 else 2.77778e-18


class ScenarioLatencyModel(LatencyModel):  # type: ignore[misc]  # untyped ABIDES base
    """Per-message latency drawn from the scenario's ``latency_config`` distribution.

    ABIDES's built-in LatencyModel offers only fixed ("deterministic") latency or a
    cubic-jitter model; neither matches a scenario's requested distribution. This draws
    each message's latency from the configured model, clipped to ``[min_ns, max_ns]``:
      - ``log_normal``: ``lognormal(mu=ln(mean_ns), sigma)`` so the median is ``mean_ns``;
      - ``uniform``:    ``uniform(min_ns, max_ns)`` (``mean_ns`` is advisory for this model);
      - ``pareto``:     ``min_ns * (1 + Pareto(alpha))`` (heavy-tailed; default ``alpha=1.5``,
                        producing occasional extreme delays for latency-edge-case scenarios);
      - otherwise:      a constant ``mean_ns``.
    Draws use a seeded RandomState and the kernel's deterministic message order, so the run
    stays reproducible for a given scenario seed.
    """

    def __init__(
        self,
        n_agents: int,
        latency_config: dict[str, Any],
        random_state: np.random.RandomState,
    ) -> None:
        params = latency_config.get("params", {})
        self._model = str(latency_config.get("model", "deterministic"))
        self._mean_ns = float(params.get("mean_ns", 0.0))
        self._sigma = float(params.get("sigma", 0.0))
        self._min_ns = float(params.get("min_ns", 0.0))
        self._max_ns = float(params.get("max_ns", 1e12))
        self._alpha = float(params.get("alpha", 1.5))
        self._mu = float(np.log(self._mean_ns)) if self._mean_ns > 0 else 0.0
        super().__init__(
            random_state=random_state,
            min_latency=np.zeros((n_agents, n_agents)),
            latency_model="deterministic",
        )

    def get_latency(self, sender_id: int, recipient_id: int) -> int:
        if sender_id == recipient_id:
            return 0
        if self._model == "log_normal":
            value = self.random_state.lognormal(mean=self._mu, sigma=self._sigma)
        elif self._model == "uniform":
            value = self.random_state.uniform(self._min_ns, self._max_ns)
        elif self._model == "pareto":
            base = self._min_ns if self._min_ns > 0 else 1.0
            value = base * (1.0 + self.random_state.pareto(self._alpha))
        else:
            value = self._mean_ns
        return int(round(float(np.clip(value, self._min_ns, self._max_ns))))


def _interval_ns(params: dict[str, Any]) -> int:
    """Per-wakeup interval: explicit rebalance interval, else 1 / arrival_rate_hz."""
    if "rebalance_interval_ns" in params:
        return int(params["rebalance_interval_ns"])
    hz = float(params.get("arrival_rate_hz", 1.0))
    return int(1e9 / hz) if hz > 0 else int(1e9)


def _agent_kwargs(
    agent_type: str, params: dict[str, Any], reference_price: int
) -> dict[str, Any]:
    """Translate scenario params into the custom agent's constructor kwargs."""
    common = {"interval_ns": _interval_ns(params)}
    if agent_type == "NoiseTrader":
        return {
            **common,
            "order_size_mean": params.get("order_size_mean", 10),
            "order_size_std": params.get("order_size_std", 2),
            "price_offset_ticks": params.get("price_offset_ticks", 5),
            "reference_price": reference_price,
        }
    if agent_type == "MarketMaker":
        return {
            **common,
            "spread_ticks": params.get("spread_ticks", 2),
            "depth_levels": params.get("depth_levels", 3),
            "size_per_level": params.get("size_per_level", 10),
            "reference_price": reference_price,
        }
    if agent_type == "ValueTrader":
        # fundamental_value_source is always the oracle in this baseline.
        return {
            **common,
            "order_size_mean": params.get("order_size_mean", 25),
            "threshold_ticks": params.get("threshold_ticks", 2),
        }
    if agent_type == "MomentumTrader":
        return {
            **common,
            "order_size_mean": params.get("order_size_mean", 15),
            "threshold_ticks": params.get("threshold_ticks", 2),
            "lookback": params.get("lookback", 5),
        }
    raise KeyError(f"unsupported agent_type: {agent_type!r}")


def build_config(scenario: dict[str, Any], seed: int | None = None) -> dict[str, Any]:
    """Return an ABIDES config dict for ``scenario`` (validated elsewhere)."""
    seed = int(scenario["seed"] if seed is None else seed)
    np.random.seed(seed)

    exchange_cfg = scenario["exchange_config"]
    # ABIDES's ExchangeAgent.kernel_terminating hardcodes the symbol "ABM", so the
    # baseline uses it. The scenario's symbol is cosmetic (it is not a trace.parquet
    # column). A one-line ABIDES patch could honor exchange_config.symbol if a future
    # multi-symbol scenario ever needs it.
    _scenario_symbol = str(exchange_cfg.get("symbol", "ABM"))  # noqa: F841 (documented)
    ticker = "ABM"

    # Exchange-protocol family (MP), OPT-IN. Only when ``exchange_config.protocol_enforcement`` is
    # true do we honor the exchange's self-trade policy and exec-report/ack timing; otherwise the
    # exchange stays in its legacy inert mode (stp_policy=None, zero response delay), so every
    # existing reference trace is byte-for-byte unchanged. See notes/PHASE3_TAIL_MP_PLAN.md §4c.
    _proto = bool(exchange_cfg.get("protocol_enforcement", False))
    _stp_policy = (
        str(exchange_cfg["stp_policy"])
        if _proto and exchange_cfg.get("stp_policy")
        else None
    )
    _ack_delay = int(exchange_cfg.get("ack_delay_ns", 0)) if _proto else 0
    _compute_delay = int(exchange_cfg.get("compute_delay_ns", 0)) if _proto else 0
    oracle_params = scenario["oracle_config"].get("params", {})
    reference_price = int(oracle_params.get("initial_price", 100_000))
    horizon_ns = int(scenario["horizon_ns"])

    date_ns = int(pd.to_datetime(_DATE).to_datetime64())
    mkt_open = date_ns + str_to_ns("09:30:00")
    mkt_close = mkt_open + horizon_ns
    oracle_close = date_ns + str_to_ns("16:00:00")

    # --- oracle (mean-reverting); map scenario params onto the ABIDES symbol dict
    symbols = {
        ticker: {
            "r_bar": reference_price,
            "kappa": _oracle_kappa_per_ns(oracle_params),
            "sigma_s": 0,
            "fund_vol": float(oracle_params.get("sigma", 5e-5)),
            "megashock_lambda_a": _oracle_megashock_rate_per_ns(oracle_params),
            "megashock_mean": float(oracle_params.get("jump_sigma", 0.0)) or 1000.0,
            "megashock_var": 50_000,
            "random_state": _draw_random_state(),
            # Reactive-agent (RA) intervention: optional deterministic fundamental shock. The
            # scenario gives ``scheduled_jump.time_ns`` RELATIVE to market open (intuitive) and a
            # signed ``magnitude``; we convert to the absolute kernel clock. RNG-neutral, so a
            # baseline (no jump) and an intervention run share identical pre-jump draws. Absent ->
            # [] -> legacy behavior; existing references unchanged.
            "scheduled_jumps": (
                [
                    {
                        "time_ns": mkt_open
                        + int(oracle_params["scheduled_jump"]["time_ns"]),
                        "magnitude": int(oracle_params["scheduled_jump"]["magnitude"]),
                        "_consumed": False,
                    }
                ]
                if oracle_params.get("scheduled_jump")
                else []
            ),
        }
    }
    oracle = SparseMeanRevertingOracle(mkt_open, oracle_close, symbols)

    # --- agents: exchange first, then the scenario's agents (fixed order)
    agents: list[Any] = [
        ExchangeAgent(
            id=0,
            name="EXCHANGE_AGENT",
            type="ExchangeAgent",
            mkt_open=mkt_open,
            mkt_close=mkt_close,
            symbols=[ticker],
            book_logging=True,
            book_log_depth=10,
            log_orders=None,
            pipeline_delay=_ack_delay,
            computation_delay=_compute_delay,
            stream_history=500,
            random_state=_draw_random_state(),
            stp_policy=_stp_policy,
        )
    ]
    next_id = 1
    for agent_cfg in scenario["agent_configs"]:
        agent_type = str(agent_cfg["agent_type"])
        agent_cls = AGENT_REGISTRY[agent_type]
        params = agent_cfg.get("params", {})
        kwargs = _agent_kwargs(agent_type, params, reference_price)
        for _ in range(int(agent_cfg["count"])):
            agents.append(
                agent_cls(
                    id=next_id,
                    name=f"{agent_type}_{next_id}",
                    type=agent_type,
                    symbol=ticker,
                    starting_cash=_STARTING_CASH,
                    random_state=_draw_random_state(),
                    **kwargs,
                )
            )
            next_id += 1

    # Latency: realize the scenario's latency_config per message; fall back to ABIDES's
    # default line-distance model only when no latency_config is present. Either branch
    # consumes exactly one global-RNG draw, preserving the deterministic draw order.
    latency_cfg = scenario.get("latency_config")
    if latency_cfg:
        latency_model = ScenarioLatencyModel(
            len(agents), latency_cfg, _draw_random_state()
        )
    else:
        latency_model = generate_latency_model(len(agents))
    random_state_kernel = _draw_random_state()

    return {
        "seed": seed,
        "start_time": date_ns,
        "stop_time": mkt_close + str_to_ns("1s"),
        "agents": agents,
        "agent_latency_model": latency_model,
        "default_computation_delay": 50,
        "custom_properties": {"oracle": oracle},
        "random_state_kernel": random_state_kernel,
        "stdout_log_level": "WARNING",
    }
