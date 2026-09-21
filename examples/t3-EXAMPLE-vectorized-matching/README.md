# Vectorized Matching Engine — Price-Time Priority Benchmark

Unit ID: `t3-EXAMPLE-vectorized-matching` (the value of `[task].id` in `card.toml`)  
Track: simulation | Scenario family: throughput-scale  
Verifier: `t3.semantic_stylized` | Metric: `events_per_sec` ↑

---

## 1. Task Summary

Vectorize the ABIDES limit-order-book (LOB) matching engine to achieve at least a **5× throughput
improvement** over the unmodified ABIDES Python stack while keeping matching-engine semantics and
return-distribution statistics within published tolerance ceilings.

**Baseline.** The reference implementation is the unmodified ABIDES Python stack
(`jpmorganchase/abides-jpmc-public`, pinned commit recorded in `card.toml`).

**No absolute events/sec target is published here, because none has been measured on the
evaluation hardware.** Earlier revisions of this file named a ~50 000 events/sec baseline and a
≥ 250 000 events/sec target; neither figure was ever reproduced on the fleet your submission runs
on, and the second was a 5× multiple of the first rather than an independent measurement. Both are
removed rather than restated. For the numbers that do exist, and their provenance, see
`../../baselines/README.md` §1 and §3 — including the reproducible figure: the `events_per_sec`
recorded in the 65 shipped public `units/*/events.json`, geometric mean 13,793 (range
3,471–18,046), on hardware that is not recorded.

**What "5×" means here.** It is the scientific ambition of the unit — a substantial
constant-factor speedup over pure-Python ABIDES — not a threshold anything checks. There is no
hard floor: any admissible submission is ranked, and ranking is by raw median `events_per_sec`
against other submissions, not against a fixed number.

**Ordering must be preserved.** The LOB implements a continuous double auction with
price-time-priority (ITCH/OUCH ordering). The verifier replays fill events from the candidate
`trace.parquet` and checks that every fill respects price-time priority against the sealed
reference trace. No reordering of fills is allowed even under parallel or vectorized execution.

**Stylized facts must stay within ceilings.** Log-return distributions, volatility-clustering ACF,
and tail-index estimates from the candidate simulation must not diverge from the reference beyond
the ceilings defined in Section 5.

---

## 2. What Must Be Preserved

The semantic regression suite (**65 public scenarios**, plus a sealed set whose size is not
published — described in `../../regression_suite/README.md`) defines the correctness contract. A
submission must reproduce every property below on every scenario in that suite.

| Property | Requirement |
|---|---|
| **Fill sequence** | Same order IDs receive fills in the same sequence as the reference (price-time priority). Exact match on filled price and size. |
| **Cancel atomicity** | Cancel acknowledgements appear before any subsequent fill on the same order. A cancel that arrives while the order is partially filled must atomically cancel the residual — no residual fills may appear after the cancel ack. |
| **STP policy** | No self-crossing fills under the configured STP mode (`cancel_newest` in this scenario). The scenario's `stp_policy` field governs; do not change it. |
| **Partial-fill semantics** | Partially filled orders stay live in the book at their original price with the correct remaining quantity until cancelled or fully filled. The `size` field in `trace.parquet` records the *remaining* quantity after each partial fill event. |
| **Timestamp tolerance** | Fill timestamps must be within ±1 µs (1 000 ns) of the reference fill timestamps. Clock-jitter from latency sampling is absorbed by the tolerance window. |
| **Trace schema** | `trace.parquet` must contain exactly the columns listed in Section 4 with the correct types. Extra columns are ignored; missing columns are a schema failure. |

Additionally, the candidate simulation must stay within the **stylized-fact ceilings** listed in
Section 5. These ceilings are checked on long-horizon calibration scenarios (Family 5 sealed
suite), not on the throughput scenario itself.

---

## 3. Suggested Approaches

These are starting points. Participants are free to use any technique that satisfies the interface
and semantic contracts.

**NumPy sorted-array price levels.**  
Replace the Python `SortedDict` price-level structure with a pre-allocated NumPy array of price
buckets, keeping an integer pointer to the best bid/ask. Insert and cancel are O(1) amortized;
matching is a vectorized scan from the best price inward.

**Numba JIT-compiled order queue.**  
Annotate the inner fill loop with `@numba.njit`. The queue data structure (price → deque of
`(order_id, size, timestamp)`) can be represented as a structured NumPy array to remain
Numba-compatible. First-run JIT compilation cost is amortized over the benchmark (first run is
discarded as warm-up).

**Vectorized fill loop.**  
Pre-compute all incoming orders for a time slice, sort by price then arrival time, then fill
against the book in a single vectorized pass. Requires that oracle and agent message generation
be factored into a batch API.

**Batch order processing.**  
Group agent wakeup events into mini-batches keyed by simulation nanosecond. Process each batch
as a NumPy array of (price, size, side, agent_id) tuples rather than dispatching events one at a
time through Python method calls. This reduces Python-level dispatch overhead.

**What NOT to change.**  
- Do not alter the oracle model (mean-reverting process) or its parameters.
- Do not change the agent decision logic (noise trader, market maker, value trader) in ways that
  alter their stochastic behaviour — only their internal data structures and dispatch paths.
- Do not change the inter-agent message protocol or the ITCH/OUCH event types.
- Do not pre-seed the random number generator in a way that differs from the scenario `seed`
  field.

---

## 4. Submission Interface

Submissions are Docker images. The image must implement the following CLI entry point:

```
simulate --config /input/scenario.json --out /output/trace.parquet
```

The container is run with `--network=none`. All inputs are bind-mounted read-only at `/input/`;
outputs are written to `/output/`.

**Required output files:**

`/output/trace.parquet` — Event trace with the following columns (all others are ignored):

| Column | Type | Description |
|---|---|---|
| `t_ns` | int64 | Event timestamp, nanoseconds since simulation epoch |
| `agent_id` | int32 | Originating agent identifier |
| `msg_type` | string | `ORDER_SUBMITTED`, `ORDER_ACCEPTED`, `ORDER_FILLED`, `PARTIAL_FILL`, `ORDER_CANCELLED`, `ORDER_REPLACED`, `QUOTE_UPDATE` |
| `side` | string | `BID`, `ASK`, or null for non-directional messages |
| `price` | int64 | Price in integer ticks (null for market orders at submission) |
| `size` | int64 | Order size in shares (remaining quantity for partial-fill events) |
| `order_id` | int64 | Unique order identifier, stable across fill/cancel/replace events |

`/output/events.json` — Metadata:

```json
{
  "scenario_id": "<UUID matching the scenario config>",
  "seed": <int>,
  "n_events": <int>,
  "wall_clock_sec": <float>,
  "events_per_sec": <float>,
  "trace_sha256": "<hex SHA-256 of /output/trace.parquet>"
}
```

All six keys are required; the g1 schema gate marks a submission `SCHEMA_INVALID_OUTPUT`
(inadmissible) if any is missing.

`events_per_sec` is measured as total events in `trace.parquet` divided by wall-clock seconds
from simulation start to simulation end (not including container startup). The harness cross-checks
this against its own external wall-clock measurement.

---

## 5. Scoring

### Admissibility Gate 1 — Semantic Regression Suite

The submission runs against the full regression suite — the 65 public scenarios plus the sealed
set. Each scenario produces a `trace.parquet` that is compared against the reference. The
submission must pass every check in every scenario to be admissible; there is no majority rule in
either tier.

Failure label: `t3.semantic_regression_fail`

### Admissibility Gate 2 — Stylized-Fact Ceilings

Log-returns are extracted from the mid-price time series of calibration-family scenarios. All four
divergence metrics below must be under their ceilings for the submission to be admissible.

| Metric | Ceiling | Description |
|---|---|---|
| KS distance | ≤ 0.08 | Two-sample KS statistic on log-return distributions, candidate vs reference |
| ACF \|r_t\| L2 | ≤ 0.12 | RMS difference in ACF of absolute returns at lags (1, 5, 10, 20, 50) |
| Hill abs error | ≤ 1.5 | Absolute error in Hill tail-index estimator (top-100 order statistics of \|r\|) |
| Depth JS | ≤ 0.10 | Jensen-Shannon divergence on bid-ask depth distribution |

A breach of any single ceiling disqualifies the submission regardless of throughput.

Failure label: `t3.stylized_fact_breach`

### Ranking Metric

Admissible submissions are ranked by **median `events_per_sec` across the measured throughput
repeats**, with the first run discarded as JIT warm-up. The repeat count and the warm-up discard
are committed in the evaluation plan rather than fixed in this document. The rate is
host-measured — the runner's own event count over the runner's own wall clock — not read from your
`events.json`. Higher is better (`leaderboard_sort = "desc"`).

---

## 6. Files in This Unit

| File | Role | Description |
|---|---|---|
| `card.toml` | card | Machine-readable unit specification |
| `scenario.json` | scenario_config | Throughput benchmark scenario (200 noise traders, 10 MMs, 20 value traders, 1-hour horizon) |
| `manifest.json` | manifest | File inventory with checksums |
| `README.md` | documentation | This file |

---

## 7. Example Run

```bash
docker run --rm --network=none \
  -v $(pwd)/input:/input:ro \
  -v $(pwd)/output:/output \
  --cpus=4 --memory=16g \
  my-simulator:latest \
  simulate --config /input/scenario.json --out /output/trace.parquet
```

After the run completes:

```bash
# Inspect trace
python -c "import pandas as pd; df = pd.read_parquet('output/trace.parquet'); print(df.dtypes); print(df.shape)"

# Check events.json
cat output/events.json

# Run the public regression harness. It drives your IMAGE over every public scenario --
# there is no flag that scores a single trace file you already produced.
# Run these from the repository root, and build the reference cache once first.
python regression_suite/build_reference_cache.py
python regression_suite/run_regression.py \
  --candidate-image my-simulator:latest \
  --scenarios-dir regression_suite/scenarios/ \
  --reference-dir regression_suite/reference_traces/ \
  --output-dir run_outputs/ --workers 4

# Read the stylized facts. The same run computed them -- all four, including depth-JS --
# and wrote one report per scenario. There is no separate checker script or directory.
cat run_outputs/<scenario_id>/stylized_fact_report.json
```
