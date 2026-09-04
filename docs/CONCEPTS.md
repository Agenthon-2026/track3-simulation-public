# CONCEPTS.md — What Track 3 is testing, in plain English

## Executive summary (read this first)

Track 3 asks one question: **can you build a market simulator that is faster than ABIDES
(the reference) and still behaves like a real stock exchange?**

This document is a self-contained glossary of every concept you need to understand before
you write a single line of code. Read it top to bottom once. After that, use it as a
reference. Every term is defined the first time it appears. When a term is also in the
project-wide glossary, we point you there: the competition-wide GLOSSARY is published with the
shared toolkit, at `Agenthon-2026/Agenthon2026-public`, file `docs/GLOSSARY.md` — the same public
repository this track installs `qfbench2-common` from. It is not in this repository; this file is
the public glossary for Track 3.

The main concepts, in the order they appear in this file:

1. What a market simulator is and why it matters
2. ABIDES — the reference simulator
3. The limit order book (LOB) — the heart of the exchange
4. The matching engine — the rules for filling orders
5. Price-time priority, partial fills, cancel/replace, self-trade prevention
6. The event trace — what your simulator must output
7. Seeds and determinism — why reproducibility is non-negotiable
8. The semantic regression check — correctness gate
9. Stylized facts — the realism gate
10. The metrics we measure: KS distance, ACF, Hill estimator, Jensen-Shannon divergence
11. Events per second — the speed metric
12. The speed-realism frontier — what the competition optimizes

---

## 1. What a market simulator is and why it matters

A **market simulator** is a computer program that imitates a real stock exchange. In a real
exchange, many buyers and sellers send in orders — instructions like "buy 100 shares of
stock X at $50" or "sell 200 shares at any price." The exchange collects these orders,
matches buyers with sellers, and publishes the resulting trades.

Researchers use simulators to study how markets behave — for example, what happens when
algorithmic traders chase momentum, or when market makers withdraw during a crisis — without
risking real money or waiting years for data.

The challenge: real exchanges process millions of orders per day. A naive Python simulator
is far too slow for large-scale experiments. Track 3 asks participants to build a
**faster** version that still matches the rules of the real exchange.

---

## 2. ABIDES — the reference simulator

**ABIDES** (Agent-Based Interactive Discrete Event Simulator) is an open-source market
simulator originally built by J.P. Morgan. Source:
`https://github.com/jpmorganchase/abides-jpmc-public`. It is the **reference baseline**
for Track 3 — your simulator must be faster than ABIDES while producing the same results.

ABIDES models a market as a set of **agents** (software programs that send buy/sell orders)
and an **exchange** (a program that collects orders, matches them, and sends back fills).
Each agent runs on its own simulated clock, and the simulator steps through time
event-by-event.

The Track 3 evaluation harness runs your simulator the same way it runs ABIDES: it sends
in a config file (`scenario.json`) and expects two output files (`trace.parquet` and
`events.json`). If your output matches ABIDES's output within the declared tolerances,
you pass the correctness gate. Then you are ranked by speed.

**Scope note.** Track 3 only concerns ABIDES's *market simulation* — the `abides-core` event kernel and
the `abides-markets` exchange, order book, and trading agents. ABIDES also ships `abides-gym`, a
reinforcement-learning wrapper; that is **out of scope** for this track. You do not need it, and its
legacy `gym`/`ray` dependencies are unnecessary and do not install on modern Python.

---

## 3. The limit order book (LOB)

The **limit order book** (LOB) is the data structure at the heart of every exchange. It
is a sorted list of open buy orders and open sell orders waiting to be matched.

### A simple picture

Imagine a stock currently trading around $100. The LOB might look like this:

```
SELL SIDE (asks, sorted lowest first):
  $100.03 — 500 shares (3 orders: 200, 200, 100)
  $100.02 — 300 shares (2 orders: 200, 100)
  $100.01 — 200 shares (1 order: 200)

    ↑ spread (gap between best bid and best ask)

BUY SIDE (bids, sorted highest first):
  $99.99 — 400 shares (2 orders: 300, 100)
  $99.98 — 600 shares (3 orders: 200, 200, 200)
  $99.97 — 100 shares (1 order: 100)
```

The **best ask** (lowest sell price) is $100.01. The **best bid** (highest buy price) is
$99.99. The **spread** (the gap) is $100.01 - $99.99 = $0.02.

A new buy order at $100.01 or higher would immediately match ("cross") against the best
ask. A new sell order at $99.99 or lower would match against the best bid.

Orders that do not immediately match wait in the book until a matching counterpart arrives
or the order is cancelled. These are called **resting orders**. An incoming order that
immediately matches is called an **aggressor**.

---

## 4. The matching engine

The **matching engine** is the part of the exchange that decides whether an incoming order
matches any resting order and, if so, records a **fill** (a completed trade). The matching
engine is the most correctness-critical component. Even one wrong fill invalidates the
entire simulation.

The matching engine must handle:

- **Limit orders** — "buy up to N shares at price P or lower" (for a buy) or "sell at P
  or higher" (for a sell). A limit order that does not immediately cross rests in the book.
- **Market orders** — "buy N shares at whatever the current best price is." These fill
  immediately against the best resting orders, walking through the book until they are
  fully filled or the book is exhausted.
- **Partial fills** — when a large incoming order matches against several smaller resting
  orders in sequence (see Section 5).
- **Cancel and replace** — when an agent wants to modify an existing order (see Section 5).
- **Self-trade prevention** — rules to prevent a single firm from trading against itself
  (see Section 5).

---

## 5. Price-time priority, partial fills, cancel/replace, self-trade prevention

### Price-time priority

When two resting orders are at the same price on the same side, which one gets filled
first? The answer is **the one that arrived first** — this is called **price-time priority**
(also known as FIFO — first-in, first-out).

Example: if Agent A posts a sell order at $100.01 at time 9:00:00.000 and Agent B posts a
sell order at $100.01 at time 9:00:00.001, a buy order hitting $100.01 must fill Agent A's
order first. If Agent B's order fills before Agent A's, that is a **price-time priority
violation** and is treated as an immediate failure in the regression check.

### Partial fills

A **partial fill** happens when a large aggressor order matches against several smaller
resting orders in sequence.

Example: a buy market order for 500 shares hits the book shown above:
1. Fills the 200-share order at $100.01 first (price priority, then time priority).
2. Still needs 300 shares. Moves to $100.02. Fills the 200-share order first, then the
   100-share order.
3. Total: 500 shares filled across three separate fill events.

A resting order that is **partially** filled (say, 150 of 200 shares fill) stays in the
book with its remaining quantity (50 shares) and **keeps its original time priority**. It
does not go to the back of the queue.

### Cancel and replace

An agent can cancel a resting order (remove it from the book) or **cancel-and-replace**
it (cancel the old order and submit a new one at a different price or size). The two
operations must happen atomically from the exchange's perspective: another agent's order
cannot fill against the old version after the cancel half is processed but before the new
version is posted. If the new order is at a **different price**, it gets a **new time
priority** (it goes to the back of that price level's queue).

### Self-trade prevention (STP)

**Self-trade prevention** (STP) stops a single agent from trading against itself. Real
exchanges require this to prevent wash trading (fake volume). The reference baseline
implements two STP policies, selected per scenario by the `stp_policy` field:

- `cancel_newest` — when an incoming order would match against a resting order from the
  same agent, the *newer* (incoming) order is cancelled and the resting order stays.
- `cancel_oldest` — the *older* (resting) order is cancelled instead and the incoming
  order proceeds. Used by the exchange-protocol family.

Your simulator must apply the scenario's declared policy consistently, every time —
cancelling the wrong side, or letting the self-trade execute, diverges from the reference
fill sequence.

---

## 6. The event trace — what your simulator must output

Every time something happens in the exchange, the simulator emits an **event**. The
sequence of all events is called the **trace**. Your simulator must write the trace to a
file called `trace.parquet` (a columnar data format). Each row in the file is one event.

The most important event types:

| Event type | What it means |
|---|---|
| `ORDER_SUBMITTED` | An agent sent an order to the exchange |
| `ORDER_ACCEPTED` | The exchange acknowledged the order |
| `ORDER_FILLED` | The order (or part of it) was matched and a trade occurred |
| `PARTIAL_FILL` | Same as ORDER_FILLED but the full order quantity was not consumed |
| `ORDER_CANCELLED` | The order was removed from the book |
| `ORDER_REPLACED` | The order was cancelled and resubmitted at a new price/size |
| `QUOTE_UPDATE` | The best bid or best ask changed |

Each event has a **timestamp** in nanoseconds (`t_ns`), an order identifier (`order_id`),
the price (`price`), the quantity (`size`), and the side (`BID` or `ASK`).

Alongside the trace, your simulator writes a small JSON file (`events.json`) with summary
statistics: total event count, wall-clock runtime, the most important number for
ranking — `events_per_sec` (how many events your simulator processed per second of real
wall-clock time) — and resource telemetry (`peak_memory_bytes`, `gpu_seconds`).

Some families also require a **message-level kernel ledger**, `message_trace.parquet`,
recording each message the exchange sends and receives with its ack/pipeline-delay timing.
It is required by the exchange-protocol, reactive-agent, and batch units, and feeds the
latency/causality and message-ledger (g3.5) checks.

---

## 7. Seeds and determinism

A **seed** is a starting number given to a random-number generator (RNG). If you give the
same seed to the same RNG, you always get the same sequence of random numbers. This means
the same simulation with the same seed will always produce exactly the same trace —
**byte for byte**. This is called **determinism**.

Determinism is mandatory in Track 3. Why? Because the correctness check compares your
output trace to a **reference trace** that was pre-computed by ABIDES. If your simulator
is non-deterministic (i.e., it gives slightly different results each run even with the same
seed), the comparison is meaningless.

Common causes of accidental non-determinism to avoid: using Python's `time.time()` for
anything that affects order timing; using `dict` ordering (which changed across Python
versions); using threads that run in unpredictable order.

**The part that catches people out: determinism is a property of an implementation, not of a
seed.** Read the first sentence above carefully — it says the same seed given to *the same RNG*
produces the same sequence. Your simulator being perfectly deterministic does not mean it produces
the *reference's* trace. If you draw random numbers from a different generator, or draw a
different number of them, or draw them in a different order, you get a different sequence from the
same seed — and on a Tier-A scenario that is a failure, however correct your market logic is.

This is not a corner case here: 65 of the 66 public scenarios draw their message latencies from a
distribution, and those draws share one seeded NumPy stream with the agents, consumed in a fixed
order. The Tier-A note in the top-level `README.md` gives the exact call order and what you may
change without breaking it. Tier B scenarios do not have this constraint, and `docs/CATEGORIES.md`
explains why.

---

## 8. The semantic regression check

The **semantic regression check** is the correctness gate. It asks: "Does your simulator
produce the same sequence of trades as ABIDES would, given the same scenario?"

The check runs your Docker image on the 72 public units (65 single-scenario, 6 batch, and one
worked exemplar that is documentation rather than a graded scenario) and on a sealed scenario set
whose size and labels are not published. For each scenario, it loads your `trace.parquet` and the
pre-computed reference `trace.parquet`, then applies two tiers of comparison:

### Tier A — exact match (Families 1, 3, 6, 7, 8)

Used for scenarios that test matching-engine rules. Your fill events must appear in
**exactly the same order** as the reference, with **exactly the same prices and sizes**.
The only tolerance is a ±1 microsecond window on timestamps (to allow for
implementation-level timing differences). One wrong fill = immediate failure.

### Tier B — statistical match (Families 2, 4, 5)

Used for scenarios that test market dynamics. Your mid-price series must be
**statistically close** to the reference. Specifically:
- The two-sample Kolmogorov-Smirnov (KS) distance between your return distribution and the
  reference distribution must be ≤ 0.08 — the same calibrated return-distribution KS check
  used by the stylized-fact gate (§9).
- Your spread (the gap between best bid and best ask, averaged over time) must be within
  ±10 basis points (bps) of the reference. One basis point = 0.01%.

**Every Tier B scenario must pass.** There is no majority rule and no allowance for a marginal
miss: each scenario is graded on its own, exactly as in Tier A. Earlier revisions of this page
described an 80%-per-family threshold — nothing implements it, so do not plan around it.

### How the tier sets a unit's difficulty label

Each unit's card carries a `difficulty` field, and it is **derived from the tier, not
assigned by hand**. It reflects how hard it is to *accelerate the unit while still passing
this gate*:

- **Tier A → `hard`.** Bit-exact reproduction (fill sequence + latency-causal message
  ledger) forbids exactly the moves that buy GPU speed — reordering events, batching agents,
  approximating arithmetic, collapsing the discrete-event queue. A naive/fast port is
  *rejected* by the g3 gate (verified: non-reactive, leaky-batch, and fills-only ports all
  fail), so a submission that is both faster and faithful is genuinely hard.
- **Tier B → `medium`.** The statistical tolerance (KS / spread / stylized-fact ceilings)
  admits an approximation band, so there is real room to accelerate as long as the market's
  distributional behavior is preserved.

No unit is labeled `easy`: by design the track has no scenario that a trivial port can both
reproduce *and* speed up — that is the anti-cheat premise of the benchmark. (The label is a
property of the tier; it is not calibrated by having a model attempt each unit.)

---

## 9. Stylized facts — the realism gate

**Stylized facts** are statistical properties that almost every financial market shares,
regardless of the country, asset class, or time period. They were catalogued by Rama Cont
in a famous 2001 paper.

A simulator that passes the semantic regression check might still produce a price series
that looks completely unrealistic — for example, one with perfectly Gaussian returns and no
clustering of big moves. The stylized-fact check catches this.

Before a submission is ranked by speed, it must pass all four gated stylized-fact checks
(KS distance, ACF of |r_t|, Hill tail exponent, and depth-distribution JS — the first three
are also computed by the local `regression_suite` pre-check; the depth check runs at sealed
scoring, where order-book depth histograms are reconstructed). The intraday
U-shape below is described for context but is not gated by a ceiling:

### Fat tails (heavy tails)

Real returns are not normally distributed. Extreme moves happen far more often than a
Gaussian bell curve would predict. We measure this with the **Hill tail index** (see
Section 10). The Hill index of real equity returns typically falls between 2 and 5; a
value above 10 suggests your return distribution is too thin-tailed (too Gaussian).

The ceiling: your Hill index must be within ±1.5 of the reference Hill index.

### Volatility clustering

Big price moves tend to cluster together. A day with a large move is likely to be followed
by another large move — not necessarily in the same direction, but of similar magnitude.
Technically, absolute returns `|r_t|` are positively autocorrelated at many lags. We
measure this with the **autocorrelation function of |r_t|** (ACF of |r_t|).

At the same time, the raw returns `r_t` themselves should NOT be autocorrelated (i.e.,
knowing that the price went up today should not tell you much about tomorrow's direction).

The ceiling: the L2 difference between your ACF-of-|r_t| curve and the reference curve
(over lags 1 to 20) must be ≤ 0.12.

### Intraday seasonality (U-shape)

Trading volume and volatility are not constant throughout the trading day. They are
highest at the open (9:30 AM) and close (4:00 PM) and lower in the middle of the day —
forming a rough "U" shape when you plot activity vs. time.

A simulator that spreads agent arrivals uniformly over the session will not reproduce this
pattern. Note: the intraday U-shape is described here for context but is **not** one of
the gated admissibility checks — there is no intraday ceiling. Only four stylized-fact
metrics are gated (KS, ACF of |r_t|, Hill tail exponent, and depth-distribution JS).

### Depth distribution

The quantity available at each price level in the book decreases as you move away from
the mid-price. The distribution of quantities across price levels follows a roughly
exponential decay. If your simulator produces a flat or random depth distribution, it fails
this check.

The ceiling: the Jensen-Shannon divergence between your depth histogram and the reference
must be ≤ 0.10.

### Return distribution shape (KS test)

The overall shape of the return distribution — not just the tails — must match the
reference. We use the two-sample **Kolmogorov-Smirnov (KS) test** (see Section 10).

The ceiling: KS distance ≤ 0.08, calibrated from the natural run-to-run variation of correct
simulations. (This is the same return-distribution KS the Tier-B statistical check uses.)

---

## 10. The metrics we measure

### KS distance (Kolmogorov-Smirnov)

The **KS statistic** measures the maximum vertical distance between two cumulative
distribution functions (CDFs). You can think of it as: "what is the worst-case fraction of
the time that these two distributions disagree?"

A KS distance of 0 means the two distributions are identical. A KS distance of 1 means
they are completely non-overlapping. For Track 3, the stylized-fact return-distribution
ceiling is 0.08 — your distribution can deviate from the reference by at most 8% at any point.

```
Reference CDF:  ▁▂▄▆▇████  (how ABIDES returns accumulate)
Candidate CDF:  ▁▂▃▅▇████  (how your returns accumulate)
KS distance:    ↕ (the biggest gap anywhere)
```

### ACF of |r_t| (autocorrelation function of absolute returns)

The **autocorrelation function** (ACF) at lag `k` measures: "knowing today's value, how
predictable is the value `k` steps later?" Specifically, ACF(1) is the correlation between
`|r_t|` and `|r_{t-1}|`, ACF(2) is the correlation between `|r_t|` and `|r_{t-2}|`, etc.

For volatility clustering, we expect ACF(k) to be positive and slowly decaying — a big
move today predicts slightly elevated volatility for the next several days. We measure the
**L2 norm** (root-mean-square) of the difference between your ACF curve and the reference
ACF curve over lags 1 through 20.

### Hill tail index

The **Hill estimator** measures how "heavy" the tails of a distribution are. Technically
it estimates the **tail exponent** α: for large values x, the probability of exceeding x
falls off like 1/x^α. Smaller α means heavier tails (more extreme events).

For equity returns, α is typically between 2 and 5. We compare your estimated α to the
reference α; the absolute difference must be ≤ 1.5.

### Jensen-Shannon divergence (JSD)

**Jensen-Shannon divergence** measures how different two probability distributions are. It
is always between 0 (identical) and log(2) ≈ 0.693 (completely non-overlapping). Unlike
KL divergence (a related measure), JSD is symmetric — it does not matter which
distribution you call "reference" and which you call "candidate." The gated ceiling for
the depth distribution is JSD ≤ 0.10.

---

## 11. Events per second — the speed metric

Your ranking score is `events_per_sec`: the number of exchange events your simulator
processed per second of real wall-clock time.

Your simulator must write this number to `events.json` after each run:

```json
{
  "scenario_id": "...",
  "seed": 42,
  "n_events": 1482931,
  "wall_clock_sec": 12.4,
  "events_per_sec": 119591.2,
  "trace_sha256": "e3b0c...",
  "peak_memory_bytes": 5368709120,
  "gpu_seconds": 8.1
}
```

The harness runs your simulator **five times** on the sealed benchmark scenario with five
different seeds. It discards the first run (which is typically slower because the Python
JIT needs to warm up), then takes the **median** of the remaining four runs. The median is
robust to one outlier run caused by the operating system scheduler temporarily preempting
your process.

You cannot fake `events_per_sec` — the harness checks that it is consistent with
`n_events` divided by `wall_clock_sec`, within ±5%. Submissions that report an inflated
number are disqualified.

---

## 12. The speed-realism frontier

Track 3 is fundamentally about a trade-off: **speed vs. realism.** A trivially fast
simulator that just returns an empty trace in 1 millisecond would win on speed but fails
every correctness check. A correct but unmodified ABIDES simulation is the floor — your
submission must be faster.

The competition is designed so that the two checks are independent. You must pass
**both** before you receive a leaderboard rank:

```
Submission pipeline:
    → Semantic regression check (gate 1: correctness)
        - Fail: inadmissible, no rank
        - Pass: proceed to gate 2
    → Stylized-fact check (gate 2: realism)
        - Fail: inadmissible, no rank
        - Pass: proceed to ranking
    → Throughput ranking
        - Score = median events/sec on sealed benchmark
        - Higher is better
```

This means you cannot trade realism for speed. A simulator that is 10x faster but cuts
corners on price-time priority will fail gate 1. A simulator that is 10x faster but batches
order processing into large time steps — destroying volatility clustering — will fail gate 2.

Beyond the primary raw-`events_per_sec` rank, the frontier itself is materialized by
`throughput/frontier.py` (the speed-realism Pareto frontier), reported alongside the
`secondary_diagnostics` on the final score (median speedup / efficiency / memory-efficiency)
and four special awards in `throughput/awards.py` (Best GPU Acceleration, Best Speed-Realism
Frontier, Best Latency-Semantics Preservation, Best Systems Diagnosis); the last is fed by
the `throughput/simprofile.py` SimProfile verifier, which is diagnostic only and never an
admissibility gate.

The fastest correct and realistic simulator wins.
