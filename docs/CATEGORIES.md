# CATEGORIES.md — The eight scenario families in Track 3

## Executive summary (read this first)

Track 3 uses **eight scenario families** to test two things: (1) that your simulator follows
the exact rules of a stock exchange, and (2) that it still produces realistic-looking
markets. Families 1 through 4 test correctness; Family 5 tests realism; Family 6 tests
correctness *at scale* and is used for the final speed ranking; Family 7 tests exchange
*response* fidelity (execution reports, STP, ack/pipeline timing); Family 8 tests that
background agents react correctly to a scheduled fundamental shock.

That is the *purpose* of each family. Separately, *how* a family is checked is its **tier**
(see `docs/CONCEPTS.md`): **Tier A** = exact match for Families **1, 3, 6, 7, 8**; **Tier B** =
statistical match for Families **2, 4, 5**. Purpose and tier are different axes — e.g.
Family 2 is a correctness family checked statistically.

Before reading this file, read `docs/CONCEPTS.md` — it defines every term used here
(limit order book, price-time priority, partial fill, etc.). Terms introduced here for the
first time are defined in parentheses. Further definitions are in the competition-wide glossary
published with the shared toolkit, at `Agenthon-2026/Agenthon2026-public`, file
`docs/GLOSSARY.md`; it is not in this repository.

This is the most detailed file in the track. For each family you will find:
- What it tests, in plain English with a concrete example
- The exact property it pins down
- How strict the tolerance is and why
- In general terms, how the sealed variants stress the family harder than the public examples
- Common mistakes participants make

> **Every "exact property pinned" table below describes a check that actually runs.** Where an
> earlier revision of this page described a property that no scorer evaluates, the row has been
> removed and the omission is stated in place. Engineer against the tables here and against
> `qfbench2_track_simulation/semantics.py`, which is the implementation they document.
>
> **The sealed material is the scenarios, not the logic.** Configs, seeds and reference traces for
> the sealed scenarios stay sealed until after the competition. The admissibility gates, the
> tolerance ceilings and the ranked-score function are all published — in this file, in
> `regression_suite/README.md`, and as executable code in `qfbench2_track_simulation/`. Nothing
> about how you are graded is withheld. Counts, labels and parameters of the sealed scenarios are.

---

## Family 1 — Matching-Engine Semantics

### What this tests (plain English)

Family 1 checks that your exchange does the right thing when orders arrive in specific
sequences. These are the core rules of a continuous double auction (the type of market
used by most major stock exchanges).

**The key rule: price-time priority.** When two buy orders both want to pay the same price,
the one that arrived first gets to trade first. No exceptions. Example:

```
9:00:00.000  Agent A posts: SELL 100 shares at $50.01
9:00:00.001  Agent B posts: SELL 100 shares at $50.01   ← same price, arrived later
9:00:00.002  Agent C posts: BUY  150 shares at $50.01   ← aggressor (crosses the book)
```

Correct behavior: Agent C's buy first fills Agent A's 100 shares (A arrived first), then
fills 50 shares of Agent B's order. Agent B keeps the remaining 50 shares resting in the
book.

Wrong behavior (a regression failure): filling Agent B first, or filling all of Agent A
and all of Agent B in one step, or emitting the fill events in the wrong order.

This family also exercises:
- **Partial fills** — a single order that consumes multiple resting orders generates one
  fill event per resting order consumed, in strict price-then-time order.
- **Cancel/replace atomicity** — an agent cancels and resubmits an order; the cancel
  must be fully processed before any other agent can fill the old order.
- **Self-trade prevention (STP)** — when an agent's order would trade against its own
  resting order, the exchange cancels the newer order (the `cancel_newest` policy). Family 7
  (exchange-protocol) formalizes both the `cancel_newest` and `cancel_oldest` STP baselines.
- **Market orders** — a "buy at market" order walks the book level by level until fully
  filled; any unfilled residual is cancelled (FAK — Fill And Kill semantics — unless the
  scenario says otherwise).

A dense, high-churn matching/replay variant (the **MR** units, `t3-mr-*`) stresses these
same rules under heavy order churn. It is scored under Family 1 (Tier A).

### Exact property pinned

**Bit-exact fill ordering.** For each scenario, a reference trace is pre-computed by
ABIDES. Your trace must contain exactly the same fill events (same `order_id`, same price,
same size) in the same sequence. The verifier aligns the fill sub-sequences by position
and checks element-by-element.

### Tolerance: why so strict?

**This is a Tier A check: zero tolerance on sequence.** One transposed fill = immediate
failure. The only flexibility is a ±1 microsecond window on event timestamps. Why so
strict? Because price-time priority is a legal rule in most jurisdictions. An exchange that
does not follow it to the letter would be sued. Track 3 is checking whether your simulator
correctly models that legal contract.

| What is checked | Tolerance |
|---|---|
| Fill sequence order (same `order_id` at each position) | Exact — 0 deviations allowed |
| Fill price (integer ticks) | Exact — must match the reference |
| Fill size (integer shares) | Exact — must match the reference |
| Event timestamp | ±1 µs (1,000 nanoseconds) |
| Cancel ordering (cancel must precede fills on subsequent orders) | Exact |

### How the sealed variants are harder

The public examples use shallow books and small populations to make debugging easy. The sealed
Family 1 scenarios stress the same rules harder along these axes:

- **Deeper books** — a large aggressor order must walk through many more price levels than
  in any public example, generating long fill sequences that must match exactly.
- **Larger populations with simultaneous arrivals** — when two orders arrive at the same
  nanosecond, the tie-break rule (ascending order ID) must be applied. More agents = more
  ties = more tie-breaking pressure.
- **Denser order lifecycles** — heavier cancel/replace activity than the public examples,
  exercising order-lifecycle bookkeeping.

The number of sealed scenarios in this family, and their specific parameters, are not disclosed
before the competition ends. Nothing about the *checks* differs: the sealed scenarios run through
the same Tier-A comparison, at the same tolerances, as the public ones.

### Common mistakes

1. **Wrong time priority for partial fills.** If Order A rests at $50 with 200 shares,
   then an aggressor fills 100 shares, Order A must keep its original arrival-time priority
   on the remaining 100 shares. Implementations that reset the priority after a partial
   fill will fail when a third order arrives at $50 and incorrectly fills behind Order A
   instead of in front of it.

2. **Wrong self-trade-prevention policy.** The reference cancels the *newer* order when an
   agent would trade against itself (`cancel_newest`). Cancelling the resting order instead —
   or letting the self-trade execute — diverges from the reference fill sequence.

3. **Processing cancel and replace as two separate visible events.** Another agent's order
   must not fill against the old price between the cancel half and the replace half. The
   atomicity check detects this.

4. **Discarding the unfilled residual of a market order silently.** A market order that
   exhausts the book must emit an explicit `ORDER_CANCELLED` event for the unfilled
   residual. Silently dropping the quantity is a fill-omission failure.

---

## Family 2 — Agent-Mix / Market-Regime Scenarios

### What this tests (plain English)

Family 2 checks that the overall behavior of the market is correct when the population of
traders is configured in a specific way. Unlike Family 1, this is not about bit-exact fill
sequences — it is about whether the **aggregate** price dynamics make economic sense.

ABIDES uses four types of agents:

- **Noise traders** — submit random limit orders at prices scattered around the current
  mid-price. They have no information about where the price "should" be. They generate
  raw order flow but no price direction.
- **Value traders** — believe the stock has a "fundamental value" set by an oracle. They
  buy when the price drops below fundamental value and sell when it rises above. They pull
  the price back toward fair value — a **mean-reversion** force.
- **Momentum traders** — chase recent price moves. They buy after a run-up and sell after
  a drop. They amplify trends.
- **Market makers** — post two-sided limit quotes (one buy order and one sell order at
  the same time). They profit from the spread (difference between bid and ask) and keep
  the market liquid. When they are absent or withdraw, the spread widens dramatically.

Two regime configurations are tested:

- **Calm regime** — low noise trader arrival rate, moderate value traders, tight market
  maker spread. The price should oscillate narrowly around fundamental value. The spread
  should be narrow and stable.
- **Stressed regime** — high noise trader arrival rate, elevated momentum traders, market
  makers temporarily withdrawn. The price should exhibit occasional large swings and wide
  spreads.

### Exact property pinned

Two aggregate statistical properties of the mid-price and spread time series — these are the
whole Tier-B check, implemented in `semantics.check_tier_b`:

| Property | What it measures | Ceiling |
|---|---|---|
| Mid-price return distribution | Two-sample KS distance between your mid-price log-returns and the reference's | ≤ 0.08 (the same calibrated return-distribution KS as the stylized-fact gate) |
| Spread proximity | Time-averaged spread in basis points, versus the reference's | within ±10 bps (`[scoring.params].spread_bps_tolerance`) |

Numeric sanity runs first, on both traces: a non-finite `t_ns`, `price` or `size`, or a
non-positive price or size, fails the unit before any statistic is computed.

> **Not gated, despite what an earlier revision of this page said.** There is no mean-reversion
> check (no signed ACF of the oracle deviation at lag 1) and no market-maker-presence check (no
> "best bid and best ask exist ≥ 95% of the time"). Neither is implemented in the public scorer or
> in the private one, so neither can pass or fail you. They remain good properties for a correct
> simulator to have — a market whose value traders do not mean-revert, or whose book is empty half
> the time, will generally miss the KS and spread ceilings above — but engineer against the two
> rows in the table, which are what actually execute.

### Tolerance: why statistical?

**This is a Tier B check.** Bit-exact comparison is not meaningful here — the market
dynamics have statistical noise from the random agent decisions, and a slightly different
internal RNG sequence will produce slightly different prices even with the same seed. What
matters is that the statistical properties are the same, not the exact prices.

**There is no majority rule.** Every scenario is graded on its own and every scenario must pass;
one Tier-B failure is one inadmissible unit. Earlier revisions of this page, and of
`regression_suite/README.md`, described an "80% of the family must pass" rule — no scorer
implements it, and `run_regression.py` reports `all_pass` only when `failed == 0 and errored == 0`.
Assume no slack.

### How the sealed variants are harder

Public examples use balanced agent mixes. The sealed Family 2 scenarios push the population
mix towards extremes in both directions:

- **Momentum-dominated mixes** — the market trends strongly. A simulator that does not
  correctly amplify momentum will produce a calmer market than the reference.
- **Noise-dominated mixes with the market makers withdrawn** — the book is thin and the spread
  erratic. Implementations that assume a market maker is always present will miss the spread
  tolerance.
- **Value-dominated mixes** — rapid, tight mean reversion; the price deviates minimally from
  the oracle.

Read every population parameter from `scenario.json` at run time. The specific ratios, and the
number of sealed scenarios in this family, are not disclosed before the competition ends.

> **There is no `regime_switch` scenario flag.** An earlier revision of this page described a
> sealed scenario that "starts calm and switches to stressed", marked in its config by
> `"regime_switch": true`. No such key exists — not in the public scenario schema, not in the
> baseline adapter, and not in any sealed config. Do not write code that looks for it.

### Common mistakes

1. **Ignoring agent type counts.** Some implementations hardcode the number of agents or
   their types instead of reading from `scenario.json`. Sealed scenarios use very different
   ratios.

2. **Treating the oracle update cadence as constant.** The oracle (fundamental value)
   updates at a configurable frequency. Value traders hold stale oracle values between
   updates; they must not use the latest value mid-step if it has not yet been "published."

3. **Not modeling market maker withdrawal.** In the stressed regime, market makers stop
   posting quotes at a certain point. Implementations that keep market makers active
   unconditionally will have a narrower spread than the reference, failing the spread check.

---

## Family 3 — Latency-Profile Scenarios

### What this tests (plain English)

In a real market, messages between agents and the exchange travel over a network. Some
agents have faster connections than others. A message sent at time T by an agent with
1-millisecond latency arrives at the exchange at time T + 1 ms. An agent with 0.1 ms
latency sending the same message at the same simulated time arrives first.

Family 3 checks that your simulator correctly models this network delay and processes
messages in the order they **arrive at the exchange**, not the order they were sent.

Example:

```
Agent A (latency: 0.5 ms) sends order at t=10.000 ms → arrives at exchange at t=10.500 ms
Agent B (latency: 0.2 ms) sends order at t=10.300 ms → arrives at exchange at t=10.500 ms
```

Both arrive at the same nanosecond (a "tie"). The tie-break rule: ascending `order_id`.
So if Agent B's order ID is lower, it is processed first, even though Agent A sent its
order 300 microseconds earlier.

Latency models, named by the `latency_config.model` string the scenario actually carries. All
four appear in the public scenarios, so you can develop against every one of them:

| `model` | Behaviour | Public scenarios using it |
|---|---|---|
| `deterministic` | Every message from this agent takes exactly the configured delay | 1 |
| `log_normal` | Latency drawn from a log-normal distribution | 58 |
| `uniform` | Latency drawn uniformly between two bounds | 3 |
| `pareto` | Heavy-tailed; occasional extreme delays (`t3-eq-pareto-heavytail`, `t3-eq001-pareto-latency-tail`, `t3-st05-latency-spike-pareto`) | 3 |

Support all four, and read the model and its parameters from `latency_config` at run time —
never hardcode one.

### Exact property pinned

**Kendall-τ rank correlation** between your event sequence and the reference sequence must be
≥ 0.999 (`[scoring.params].kendall_tau_floor`). Kendall-τ measures how similarly two lists are
ordered. A value of 1.000 means identical ordering; 0.999 means at most 0.1% of pairs are
inverted.

Family 3 is a Tier-A family, so it also gets the full Tier-A check — exact row count, exact fill
sequence, exact bidirectional event coverage. The τ statistic is computed over **every** matched
event, not a prefix or a sample.

| What is checked | Tolerance |
|---|---|
| Event count (candidate vs reference rows) | Exact |
| Fill `order_id` / `price` / `size` sequence | Exact |
| Fill timestamps | ±1 µs (`timestamp_tolerance_ns`) |
| Event coverage, both directions (missing *and* extra events) | 0 of each |
| Kendall-τ of event sequence vs reference | ≥ 0.999 |

Tie-breaking is not a separate check: an incorrectly resolved tie shows up as a coverage or
ordering breach in the rows above. The tie-break rule (ascending `order_id`) is deterministic —
there is no room for variation.

> **Kendall-τ is not Family-3-only.** `check_tier_a` runs the same coverage-and-τ comparison for
> every Tier-A family (1, 3, 6, 7, 8). `regression_suite/README.md` used to present it as a
> Family-3 extra; it is not.

### Tolerance philosophy

The Kendall-τ ceiling of 0.999 is tight but allows for a small number of reorderings due
to floating-point rounding when sampling from a `lognormal` distribution. It is not
permissive enough to hide systematic errors in the arrival-queue logic.

### How the sealed variants are harder

The sealed Family 3 scenarios stress the arrival-queue logic along two axes:

- **Heavier-tailed latency** — occasional extreme delays create pathological reordering edge
  cases for messages that were sent nearly simultaneously. The public `pareto` units in the
  table above are the same mechanism at gentler settings.
- **Higher tie density** — more messages arriving at the same nanosecond, so the tie-break rule
  is applied far more often. Implementations that treat tie-breaking as a rare special case
  produce wrong orderings under it.

The sealed latency parameters, the tie density, and the number of sealed scenarios in this family
are not disclosed before the competition ends.

> **There are no "network zones" and no correlated latency draws.** An earlier revision of this
> page described sealed scenarios in which agents sharing a network zone drew a common latency
> component per timestep. No such feature exists — not in the scenario schema, not in the baseline
> latency sampler, and not in any sealed config. Every agent's latency is drawn independently from
> its own `latency_config`. Do not build a correlated sampler for it.

### Common mistakes

1. **Using `time.time()` to add network delay.** Any latency calculation that uses real
   wall-clock time is non-deterministic across runs. Use only the seeded RNG.

2. **Sorting by submission time instead of arrival time.** The exchange must sort the
   message queue by arrival time (submission time + latency). Sorting by submission time
   ignores latency differences between agents.

3. **Ignoring the tie-break rule.** When two messages arrive at the same nanosecond,
   order ID ascending must be the tie-breaker. Many implementations fall back to Python's
   dict ordering or insertion order, which is non-deterministic with respect to the
   reference.

---

## Family 4 — Oracle-Noise Robustness

### What this tests (plain English)

The **oracle** is the source of "fundamental value" that value traders use to decide
whether the stock is cheap or expensive. In a real market, this would be something like the
discounted cash flow value of the company. In the simulator, it is a configurable random
process.

Family 4 tests that your simulator correctly handles oracle values even when the oracle is
noisy, jumpy, or temporarily unavailable.

Three oracle types are tested:

- **Ornstein-Uhlenbeck (mean-reverting) oracle** — the fundamental value wanders around a
  long-run mean, always being pulled back. This is the standard oracle in the public
  examples.
- **Jump-diffusion oracle** — the fundamental value follows a random walk but occasionally
  jumps by a large amount (like a surprise earnings announcement). Value traders must react
  to the jump.
- **Scheduled-jump oracle** — a deterministic fundamental shock at a scenario-declared time.
  This is the intervention Family 8 is built on; the baseline implements it via
  `baselines/patches/oracle_scheduled_jump.patch`.

### Exact property pinned

**Family 4 is graded by the same Tier-B check as Family 2** — there is no separate oracle
comparison. `semantics.check_tier_b` runs on every Tier-B unit regardless of family:

| Property | What it measures | Ceiling |
|---|---|---|
| Mid-price return distribution | Two-sample KS on mid-price log-returns, candidate vs reference | ≤ 0.08 |
| Spread proximity | Time-averaged spread in basis points, versus the reference's | within ±10 bps |

> **The oracle-tracking checks this page used to publish do not run.** An earlier revision listed
> an "oracle-tracking RMSE within ±20% of the reference RMSE" ceiling and a "mid-price within ±2σ
> of the oracle at ≥ 90% of time steps" coverage ceiling. Neither is evaluated. The public scorer
> does not implement either — the oracle's fundamental path is not present in `trace.parquet`, so
> a mid-price-versus-oracle comparison cannot be computed from a submission's output at all. The
> private scorer contains an oracle-RMSE branch, but it is unreachable: it needs an
> `oracle_rmse_reference` and an `oracle_prices` series that nothing supplies. **Do not tune your
> value traders against a ±20% RMSE budget or a 90% coverage target; tune them against the KS and
> spread ceilings above.**

### Tolerance philosophy

**This is a Tier B check.** Bit-exact comparison is not meaningful when the oracle path itself is
stochastic; what the ceilings pin is that your market's return distribution and spread level match
the reference's, which they will only if your value traders react to the oracle correctly.

### How the sealed variants differ

The sealed Family 4 scenarios move along the same axes as the public examples rather than beyond
them on every one:

- **Slow mean reversion** — the sealed set reaches slower oracle mean-reversion than any public
  unit. This is the axis least covered by what you can test locally, so it is the one worth
  stressing yourself.
- **Jump magnitude and rate** — these stay inside the range the public units already span.
  `t3-sf-02-heavy-jump-tails` is more extreme than any sealed Family 4 scenario on both, so it is
  a valid upper bound to develop against.

This family ships **no public scenarios** — every Family 4 scenario is sealed. Develop against the
Tier-B units that exercise the jump path: `t3-sf-01` through `t3-sf-07` and
`t3-ca-fat-tail-jumps`. The Family 2 units share the agent-mix machinery but set
`jump_intensity = 0.0`, so no jump ever occurs in them; use them for population effects, not for
oracle response. Specific sealed
jump rates, parameters and scenario counts are not disclosed before the competition ends.

### Common mistakes

1. **Treating jump-diffusion as the same as Gaussian noise.** A Gaussian noise oracle
   produces small perturbations every step; a jump-diffusion oracle is usually quiet but
   occasionally produces a large instantaneous change. Value traders must respond to the
   instantaneous change; they cannot average it out.

2. **Smoothing the jump away.** Applying a low-pass filter or interpolation to the oracle
   series turns an instantaneous jump into a gradual drift; the mid-price then lags the
   fundamental value, which distorts the mid-price return distribution and fails the KS ceiling.

3. **Miscalibrated value-trader thresholds.** If the value trader's buy/sell threshold is
   too wide, the market stays wider than the reference and the time-averaged spread misses the
   tolerance.

---

## Family 5 — Calibration / Stylized-Fact Scenarios

### What this tests (plain English)

Family 5 is the realism check. It runs your simulator for a long time (8+ simulated hours)
and asks: "Does the resulting price series look like a real financial market?"

The five **stylized facts** (properties true of almost every market in the world, documented
by Cont 2001) are:

1. **Fat tails** — extreme price moves happen more often than a normal distribution
   predicts. Example: in a Gaussian world, a 5-standard-deviation daily move should happen
   once every 14,000 years. In reality, they happen every few years.
2. **Volatility clustering** — if the market was volatile today, it is likely to be
   volatile tomorrow. Big moves come in clusters.
3. **Intraday U-shape** — volume is high at the open (9:30 AM) and close (4:00 PM), and
   lower in the middle of the day.
4. **Depth distribution** — more shares are available closer to the mid-price; the
   available quantity falls off (roughly exponentially) as you move away.
5. **Return distribution shape** — the full return distribution, not just the tails, should
   look like the reference.

These are the stylized facts the admissibility gate cares about. **Four of them are
gated by hard ceilings** (KS, ACF of |r_t|, Hill tail exponent, and depth-distribution
JS). The intraday U-shape is described here for completeness but is **not** part of the
admissibility gate — there is no intraday ceiling. If your simulator breaches any one of
the four gated ceilings it is **inadmissible** — it receives no rank, regardless of how
fast it is. Family 5 is the only check that can disqualify an otherwise correct simulator.

### Exact properties pinned

The admissibility gate applies exactly these four ceilings (matching
`common/qfbench2_common/scoring/stylized_facts.py`):

| Metric | Ceiling | Estimator (as implemented) |
|---|---|---|
| Return distribution KS distance | ≤ 0.08 | Two-sample KS on mid-price log-returns |
| ACF of \|r_t\| error | ≤ 0.12 | **RMS** difference between your ACF(\|r\|) and the reference's, over lags **(1, 5, 10, 20, 50)** |
| Hill tail exponent absolute error | ≤ 1.5 | \|α_cand − α_ref\|, Hill estimator on the **top-100 order statistics** of \|r\| |
| Bid-ask depth distribution JS divergence | ≤ 0.10 | Jensen-Shannon divergence between 20-bin `QUOTE_UPDATE` size histograms |

> Two of these estimators were described incorrectly here before. The ACF metric is a **root-mean-
> square** difference over the five lags `(1, 5, 10, 20, 50)`, not an L2 norm over lags 1–20; and
> the Hill estimator uses a fixed **top-100 order statistics** cut, not an "upper 5%" quantile. The
> ceilings are unchanged; the estimators are what `acf_abs_l2` and `hill_error` in
> `qfbench2_common.scoring.stylized_facts` compute.

The return-distribution KS here (≤ 0.08) is calibrated from the natural run-to-run variation of
correct simulations. It is the same KS check the Tier-B statistical gate applies — one
statistic, one calibrated ceiling.

### Tolerance philosophy

These ceilings were calibrated from the seed-to-seed self-divergence of the reference
baseline: the baseline was run on a calibration scenario under 8 different seeds and every one of
the 28 resulting pairs was compared, giving a distribution of "how far apart two runs of a
*correct* simulator land" for each metric. Each ceiling is set at roughly 5× the 95th percentile
of that distribution, with per-metric judgement where the baseline's natural variation was
unrepresentative — loose enough that any correct implementation passes, tight enough to catch
implementations with structural defects. The four values are frozen; they are the same numbers in
the local harness, the public gate and the sealed scorer.

The most common structural defects that fail this check:
- **Capped prices** — a simulator that clips prices at some maximum will have a truncated
  return distribution with thin tails.
- **Batched order processing** — a simulator that processes orders in large time-step
  batches instead of event-by-event will destroy the volatility clustering structure.
- **Uniform intraday agent arrival** — real agents arrive more frequently at open/close.
  A simulator that spreads arrivals uniformly over the session produces a flat intraday
  profile instead of a U-shape.

### How the sealed variants are harder

Public examples use shorter simulated sessions. The sealed calibration scenarios:

- **Longer sessions** — a longer simulated horizon than any public example, so transient
  start-up artefacts stop being a meaningful fraction of the sample.
- **Larger agent populations** — reduces Monte Carlo variance in the stylized-fact
  statistics, making it harder for a marginal implementation to pass by luck.

The gated ceilings are exactly the four published above — there are no undisclosed
secondary ceilings. The sealed session horizons, populations and scenario counts are not
disclosed before the competition ends. Nothing else about the check differs: the same four
statistics, at the same four ceilings, computed by the same shared code you can read.

### Common mistakes

1. **Batching events for speed.** A common optimization is to process all orders that
   arrive within a 1-millisecond window as a batch. This speeds up the simulator but
   destroys the fine-grained volatility clustering that makes the ACF of |r_t| positive at
   short lags.

2. **Ignoring the intraday schedule.** ABIDES draws agent arrival times from a U-shaped
   intensity function over the trading day. An accelerated simulator that replaces this
   with a uniform Poisson process will produce a flat intraday profile.

3. **Price discretization artifacts.** If tick size is too coarse relative to the oracle
   volatility, returns cluster at tick boundaries. This distorts the tail of the return
   distribution.

---

## Family 6 — Throughput / Scale Scenarios

### What this tests (plain English)

Family 6 is the speed test. It runs your simulator on the largest, most demanding scenarios
and measures how fast it goes — while still checking that the output is correct.

The sealed Family 6 benchmark scenario (`SS-BENCH`) is the largest and most demanding scenario in
the suite. Its parameters — horizon, agent population, book depth and event count — are withheld
until after the competition. What is publicly stated is only its role: it is a throughput-scale
scenario of the same shape as the public Family 6 units, run through the same `simulate` interface
and the same Tier-A checks.

**`SS-BENCH` is not the only scenario that decides your rank.** An earlier revision of this page
said it was. Your leaderboard score is the **mean of your throughput over every unit in the
roster**, and a unit you fail contributes **zero** to that mean rather than dropping out of it. So
one failed unit costs you a proportion of your rank, and there is no unit you can afford to ignore
in favour of the benchmark.

The public Family 6 units exercise that interface and those checks at smaller scale. Use them to
develop and profile your implementation. They are not a good predictor of your absolute throughput
on the sealed benchmark, which is more demanding along parameters you cannot see.

A **BatchMarketSim** variant (the `t3-gbatch-*` units, invoked via the `simulate-batch`
verb) runs N independent sub-scenarios in one pass under this family. Each sub is checked by
a per-sub **isolation gate** — its output must reproduce its isolated reference — alongside
the aggregate throughput reported across all sub-scenarios.

### Exact properties pinned

Family 6 applies all of the Family 1 correctness checks **at scale**, plus the schema
cross-checks in the g1 gate:

| Property | Tolerance | Where it runs |
|---|---|---|
| Event count (candidate vs reference rows) | Exact | `check_tier_a` |
| Fill `order_id` / `price` / `size` sequence | Exact | `check_tier_a` |
| Fill timestamps | ±1 µs | `check_tier_a` |
| Event coverage, both directions (missing *and* extra events) | 0 of each | `check_tier_a` |
| Kendall-τ of event sequence | ≥ 0.999 | `check_tier_a` |
| `events_per_sec` consistency | within ±5% of `n_events ÷ wall_clock_sec` | g1 schema gate |
| `n_events` versus the real `trace.parquet` row count | Exact | g3 |
| For batch (`t3-gbatch-*`) units: each sub reproduces its **isolated** reference | Exact | `batch.score_isolation` |

> **There is no separate order-book reconstruction gate.** An earlier revision of this page listed
> "fill omissions = 0", "phantom orders = 0" and "crossed-book violations = 0" as three additional
> invariants with their own checks. No such checks are implemented — nothing in the public scorer,
> the private scorer or the shared toolkit reconstructs a book from your trace and tests it for
> crossing or for dangling order ids.
>
> Those properties are still *enforced*, but as consequences of exactness rather than as gates of
> their own: a trace that omits a fill, invents an order, or crosses the book cannot match the
> reference's event multiset and sequence, so it fails the coverage and fill-sequence rows above.
> The practical difference is the error you get — a coverage or sequence breach, not a named
> book-consistency breach — so do not expect a diagnostic that points straight at a crossed book.

### Tolerance philosophy

**This is a Tier A check.** Correctness rules do not relax because the volume is higher.
Exactness is what catches the failures that only manifest under heavy load — a simulator that
drops fill events when its event queue is full, or that maintains a stale order book that ends up
crossed during a burst of activity, diverges from the reference event stream and is refused.

The `events_per_sec` consistency check prevents misreporting. A simulator that claims
1,000,000 events/sec but only emitted 100,000 events and ran for 10 seconds is reporting
correctly (100,000 / 10 = 10,000 events/sec). The harness checks the arithmetic.

### How the sealed variants are harder

The public throughput units are moderate-difficulty warm-ups; the sealed throughput-scale
scenarios and the leaderboard benchmark run at materially larger event counts. Their sizes are
not disclosed before the competition ends.

The absolute `events_per_sec` you measure locally on a public unit is not a reliable
predictor of your rank on the sealed benchmark. The sealed benchmark may have a very
different memory access pattern (deep books = more cache misses) or a different bottleneck
(many agents = more message overhead). Profile across the whole public Family 6 set rather than
tuning to one unit.

### Common mistakes

1. **Reporting `events_per_sec` that includes initialization time.** The clock starts when
   the simulation processes the first nanosecond of simulated time. Initialization (loading
   the scenario config, constructing agents, warming up data structures) does not count.
   Including it lowers your reported throughput.

2. **Not handling a full book correctly.** Some accelerated order books use fixed-size
   arrays indexed by price level. When the price level count exceeds the array size, they
   silently discard orders or corrupt the book state. This produces a crossed-book
   violation.

3. **Using a dict for the order book in Python.** Python dicts are fast for random access
   but slow for the sorted insertion and deletion the matching engine requires. The
   reference ABIDES already uses a sorted data structure; you probably need something
   lower-level (C extension, Rust, or vectorized NumPy arrays).

4. **Multi-threading without determinism.** Parallelizing agent stepping is a powerful
   optimization, but all sources of non-determinism must be eliminated. Using a thread-safe
   queue that processes messages in arrival order is necessary but not sufficient — the
   tie-break rule (ascending `order_id`) must still be applied when timestamps collide.

---

## Family 7 — Exchange-Protocol Scenarios

Family 7 checks exchange *response* fidelity: self-trade prevention (`stp_policy`
`cancel_newest` vs `cancel_oldest`), execution-report counts (accept / execute / cancel),
and ack / pipeline-delay timing. These are verified by a gate on the message-level kernel
ledger (`message_trace.parquet`). This is a **Tier A** check — exact fill sequence plus the
ledger gate.

---

## Family 8 — Reactive-Agent Scenarios

Family 8 checks that background agents react correctly to an oracle scheduled-jump
fundamental shock; the reactive cascade they produce is the answer. This is a **Tier A**
check (exact fill sequence) and requires the message-level kernel ledger
(`message_trace.parquet`).
