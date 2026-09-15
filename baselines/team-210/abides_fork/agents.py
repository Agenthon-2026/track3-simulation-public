"""Lightweight Track 3 agents driven directly by ``scenario.json`` params.

ABIDES provides the kernel, exchange, order book, and matching engine; these
agents provide the order flow the scenario describes. Each is a thin
``TradingAgent`` subclass that wakes on a fixed schedule (derived from the
scenario's ``arrival_rate_hz`` / ``rebalance_interval_ns``), queries the current
spread, and places orders on the response. All randomness draws from
``self.random_state`` (seeded), so a run is deterministic for a given seed.

The stock ABIDES agents are not used because their parameters do not match the
scenario abstraction (e.g. the stock ``NoiseAgent`` wakes exactly once); these
honor the scenario format defined in ``templates/sim_scenario.schema.json``.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from abides_core import Message, NanosecondTime
from abides_markets.agents.trading_agent import TradingAgent
from abides_markets.messages.query import QuerySpreadResponseMsg
from abides_markets.orders import Side


class ScheduledAgent(TradingAgent):  # type: ignore[misc]  # untyped ABIDES base
    """Base agent: wake every ``interval_ns``, query the spread, then ``act()``.

    The kernel/parent delivers the first wakeup at market open; from then on the
    agent re-arms its own wakeup each cycle to produce a steady order stream.
    """

    def __init__(
        self,
        *,
        id: int,
        name: str,
        type: str,
        symbol: str,
        starting_cash: int,
        random_state: np.random.RandomState,
        interval_ns: int,
        log_orders: bool = True,
    ) -> None:
        super().__init__(id, name, type, random_state, starting_cash, log_orders)
        self.symbol = symbol
        self.interval_ns = int(max(1, interval_ns))
        self.state = "AWAITING_WAKEUP"

    def kernel_starting(self, start_time: NanosecondTime) -> None:
        super().kernel_starting(start_time)
        self.oracle = self.kernel.oracle

    def wakeup(self, current_time: NanosecondTime) -> None:
        super().wakeup(current_time)
        if not self.mkt_open or not self.mkt_close or self.mkt_closed:
            return
        # Re-arm the next wakeup, then query the book and act on the response.
        self.set_wakeup(current_time + self.interval_ns)
        self.get_current_spread(self.symbol)
        self.state = "AWAITING_SPREAD"

    def receive_message(
        self, current_time: NanosecondTime, sender_id: int, message: Message
    ) -> None:
        super().receive_message(current_time, sender_id, message)
        if self.state == "AWAITING_SPREAD" and isinstance(
            message, QuerySpreadResponseMsg
        ):
            if not self.mkt_closed:
                self.act()
            self.state = "AWAITING_WAKEUP"

    def get_wake_frequency(self) -> NanosecondTime:
        """Offset (ns) for the parent-scheduled first wakeup; 0 = at market open."""
        return 0

    def act(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError


class NoiseTrader(ScheduledAgent):
    """Each wakeup, place a random buy/sell limit order anchored to the touch.

    When the relevant side of the book is empty (e.g. a scenario with no market
    maker), the order anchors to ``reference_price`` instead so the trader can
    bootstrap a two-sided market on its own.
    """

    def __init__(
        self,
        *,
        order_size_mean: float = 10.0,
        order_size_std: float = 2.0,
        price_offset_ticks: int = 5,
        reference_price: int = 100_000,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.order_size_mean = float(order_size_mean)
        self.order_size_std = float(order_size_std)
        self.price_offset_ticks = int(price_offset_ticks)
        self.reference_price = int(reference_price)

    def act(self) -> None:
        bid, _, ask, _ = self.get_known_bid_ask(self.symbol)
        size = int(
            max(
                1,
                round(
                    self.random_state.normal(self.order_size_mean, self.order_size_std)
                ),
            )
        )
        buy = bool(self.random_state.randint(0, 2))
        offset = int(self.random_state.randint(0, self.price_offset_ticks + 1))
        if buy:
            # Cross/join the ask (a marketable-to-aggressive buy); seed off the
            # best available price, falling back to the reference if the book is empty.
            anchor = int(ask) if ask else (int(bid) if bid else self.reference_price)
            self.place_limit_order(self.symbol, size, Side.BID, anchor + offset)
        else:
            anchor = int(bid) if bid else (int(ask) if ask else self.reference_price)
            self.place_limit_order(self.symbol, size, Side.ASK, anchor - offset)


class MarketMaker(ScheduledAgent):
    """Each wakeup, cancel and repost a symmetric ladder of quotes around the mid."""

    def __init__(
        self,
        *,
        spread_ticks: int = 2,
        depth_levels: int = 3,
        size_per_level: int = 10,
        reference_price: int = 100_000,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.spread_ticks = int(max(2, spread_ticks))
        self.depth_levels = int(max(1, depth_levels))
        self.size_per_level = int(max(1, size_per_level))
        self.reference_price = int(reference_price)

    def act(self) -> None:
        bid, _, ask, _ = self.get_known_bid_ask(self.symbol)
        mid = int((bid + ask) // 2) if (bid and ask) else self.reference_price
        self.cancel_all_orders()
        half = self.spread_ticks // 2
        for lvl in range(self.depth_levels):
            self.place_limit_order(
                self.symbol, self.size_per_level, Side.BID, mid - half - lvl
            )
            self.place_limit_order(
                self.symbol, self.size_per_level, Side.ASK, mid + half + lvl
            )


class ValueTrader(ScheduledAgent):
    """Trade toward an oracle-observed fundamental when the market mid deviates.

    Each wakeup, observe a noisy fundamental value from the oracle and compare it
    to the market mid: if the asset looks underpriced by more than ``threshold_ticks``
    place a buy that crosses the ask, if overpriced place a sell that crosses the bid.
    This produces the marketable order flow that drives fills / partial fills.
    """

    def __init__(
        self,
        *,
        order_size_mean: float = 25.0,
        threshold_ticks: int = 2,
        sigma_n: float = 1000.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.order_size_mean = float(order_size_mean)
        self.threshold_ticks = int(threshold_ticks)
        self.sigma_n = float(sigma_n)

    def act(self) -> None:
        bid, _, ask, _ = self.get_known_bid_ask(self.symbol)
        if bid and ask:
            mid = (int(bid) + int(ask)) / 2.0
        elif bid:
            mid = float(bid)
        elif ask:
            mid = float(ask)
        else:
            return  # no market yet; the market maker / noise traders seed it
        fundamental = int(
            self.oracle.observe_price(
                self.symbol, self.current_time, self.random_state, sigma_n=self.sigma_n
            )
        )
        size = int(max(1, round(self.order_size_mean)))
        if mid < fundamental - self.threshold_ticks and ask:
            self.place_limit_order(self.symbol, size, Side.BID, int(ask))
        elif mid > fundamental + self.threshold_ticks and bid:
            self.place_limit_order(self.symbol, size, Side.ASK, int(bid))


class MomentumTrader(ScheduledAgent):
    """Trade in the direction of recent price movement (trend-following).

    Each wakeup, compare the current mid to the mid observed ``lookback`` wakeups ago:
    if the price has risen by more than ``threshold_ticks`` buy (cross the ask), if it
    has fallen sell (cross the bid). This adds trend-following flow distinct from the
    mean-reverting value trader. Deterministic given the price path (uses no randomness),
    so it does not perturb the seeded RNG stream.
    """

    def __init__(
        self,
        *,
        order_size_mean: float = 15.0,
        threshold_ticks: int = 2,
        lookback: int = 5,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.order_size_mean = float(order_size_mean)
        self.threshold_ticks = int(threshold_ticks)
        self.lookback = int(max(1, lookback))
        self._mid_history: list[float] = []

    def act(self) -> None:
        bid, _, ask, _ = self.get_known_bid_ask(self.symbol)
        if bid and ask:
            mid = (int(bid) + int(ask)) / 2.0
        elif bid:
            mid = float(bid)
        elif ask:
            mid = float(ask)
        else:
            return  # no market yet; other agents seed it
        self._mid_history.append(mid)
        if len(self._mid_history) > self.lookback + 1:
            self._mid_history.pop(0)
        if len(self._mid_history) <= self.lookback:
            return  # not enough history yet
        past = self._mid_history[0]  # mid `lookback` wakeups ago
        size = int(max(1, round(self.order_size_mean)))
        if mid > past + self.threshold_ticks and ask:
            self.place_limit_order(self.symbol, size, Side.BID, int(ask))
        elif mid < past - self.threshold_ticks and bid:
            self.place_limit_order(self.symbol, size, Side.ASK, int(bid))


# Scenario agent_type name -> agent class.
AGENT_REGISTRY: dict[str, type[ScheduledAgent]] = {
    "NoiseTrader": NoiseTrader,
    "MarketMaker": MarketMaker,
    "ValueTrader": ValueTrader,
    "MomentumTrader": MomentumTrader,
}
