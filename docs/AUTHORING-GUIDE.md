# AUTHORING-GUIDE.md — How to design and seal a Track 3 scenario

## Executive summary (read this first)

A **scenario** is a single test case for your simulator. Each scenario is a config file
(`scenario.json`) that tells the simulator what kind of market to run: how many agents,
what types, how long, with what network latency model. The simulator runs the scenario and
writes a **reference trace** — a record of every exchange event. That reference trace is
the answer key. When participants submit a simulator, the harness runs it on the same
scenario and checks whether the output matches the reference.

This guide walks through every step of creating one scenario. Follow the steps in order —
skipping any step produces a scenario that either fails validation or is non-deterministic,
which breaks the entire grading system. Terms are defined in `docs/CONCEPTS.md`; further
definitions are in the competition-wide glossary published with the shared toolkit, at
`Agenthon-2026/Agenthon2026-public`, file `docs/GLOSSARY.md` (not in this repository).

**Six steps, in order:**

1. Pick a scenario family
2. Write `scenario.json`
3. Generate the reference trace
4. Verify determinism
5. Set semantic-equality tolerances in `card.toml`
6. Confirm stylized-fact ceilings and throughput measurement

---

## Step 1: Pick a scenario family

There are eight families. Read `docs/CATEGORIES.md` for the full description of each.
Pick the one that matches what you want to test:

| Family | Name | What it tests | Tier |
|---|---|---|---|
| 1 | Matching-Engine Semantics | Price-time priority, fills, STP, cancel/replace | A (exact) |
| 2 | Agent-Mix / Market-Regime | Aggregate price dynamics with different agent populations | B (statistical) |
| 3 | Latency Profile | Message arrival ordering under heterogeneous network delay | A (exact) |
| 4 | Oracle-Noise Robustness | How well the market tracks a noisy or intermittent oracle | B (statistical) |
| 5 | Calibration / Stylized Facts | Long-horizon realism: fat tails, clustering, U-shape, depth | B (statistical) |
| 6 | Throughput / Scale | Correctness at high volume and speed measurement | A (exact) |
| 7 | Exchange-Protocol | Exchange response fidelity: STP, execution-report counts, ack/pipeline-delay timing | A (exact) |
| 8 | Reactive-Agent | Agents reacting to an oracle scheduled-jump shock; the reactive cascade | A (exact) |

**Tier A** means the check is bit-exact on the fill sequence. One wrong fill = failure.
**Tier B** means the check is statistical. Your output must be close to the reference on
aggregate measures, but not identical.

Why does family choice matter for scenario design? Because the tolerance model and the
stylized-fact ceilings that apply are determined by the family. Family 1 ceilings are
tight; Family 5 ceilings are explicitly designed for long, noisy runs.

### Notes for the newer families and unit types

- **Memory/replay ("MR") units** — dense, high-churn matching and replay scenarios.
  Authored and scored under Family 1 (Tier A); no new family. Unit ids `t3-mr-*`.
- **Exchange-Protocol (Family 7)** — tests exchange *response* fidelity: self-trade
  prevention (`stp_policy`), execution-report accept/execute/cancel counts, and
  ack/pipeline-delay timing, checked by a `g3.5` gate on the message ledger. Set
  `stp_policy` and any `protocol_enforcement` rules in `exchange_config`; a
  `message_trace.parquet` is required. Unit ids `t3-mp01`..`t3-mp07`.
- **Reactive-Agent (Family 8)** — background agents react to an oracle `scheduled_jump`
  fundamental shock; the reactive cascade is the answer. Tier A (exact fill sequence) with
  a mandatory `message_trace.parquet`. Unit ids `t3-ra01`..`t3-ra06`.
- **BatchMarketSim (Family 6, batch)** — one unit bundles N independent sub-scenarios run
  in a single pass via the `simulate-batch` verb. Lay out each sub-scenario in its own
  directory under the unit's batch directory; a per-sub isolation gate requires each sub's
  output to reproduce its isolated reference, scored alongside aggregate throughput. Unit
  ids `t3-gbatch-*`.

---

## Step 2: Write `scenario.json`

Copy the template from `templates/scenario.json` and fill in the fields below. The schema
**does not currently describe a Track-3 scenario, and must not be used.**

`templates/` carries `scenario.json` (the fillable template), `card.toml`, `manifest.json` and
`trace_column_registry.json`, and no schema file. The shared toolkit does ship a
`qfbench2_common/schemas/sim_scenario.schema.json`, but it is stale: it requires a two-object
envelope `{"scenario": {...}, "events": {...}}`, while every shipped scenario is flat and carries
`schema_version: 2` plus blocks the schema has no properties for (`exchange_config`,
`oracle_config`, `latency_config`, `agent_configs`, `output_config`, `tolerance`).

> **Measured 2026-08-24:** all 132 scenario files in this repository — 66 under
> `regression_suite/scenarios/` and 66 under `units/*/scenario.json` — fail validation against
> that schema. Not some: **132 of 132.**

Until the toolkit schema is updated to `schema_version: 2`, `templates/scenario.json` and the
field list below are the contract. Validating against the stale schema tells you nothing except
that it is stale.

### Top-level required fields

```json
{
  "scenario_id": "<UUID v4>",
  "description": "<one plain sentence, ≤ 140 characters>",
  "family": <integer 1–8, or the scenario_family name string>,
  "seed": <integer, 0 ≤ seed ≤ 2147483647>,
  "horizon_ns": <nanoseconds of simulated time>
}
```

- **`scenario_id`** — a globally unique identifier. Generate with:
  `python -c "import uuid; print(uuid.uuid4())"`. No two scenarios in the registry may
  share an ID. The CI check `scenarios/index.json` enforces uniqueness.
- **`description`** — plain English, no markdown. Used in the HTML report.
- **`family`** — integer 1–8. Controls which verifier runs and which tolerances apply.
  Newer units may instead give the equivalent `scenario_family` name string
  (`matching-engine-semantics`, `agent-mix`, `latency-profile`, `oracle-noise`,
  `calibration-stylized-facts`, `throughput-scale`, `exchange-protocol`, `reactive-agent`).
- **`seed`** — the master random-number seed. All per-agent seeds, per-latency-model
  seeds, and oracle draws are derived deterministically from this value by the framework.
  Valid range: 0 to 2,147,483,647. Record the seed with the scenario so runs are reproducible.
- **`horizon_ns`** — the simulated time window in nanoseconds. One US equity session =
  23,400,000,000,000 ns (6.5 hours). Minimum: 1,000,000,000 ns (1 second). Longer
  horizons increase trace file size; keep uncompressed trace under 50 GB.

### `agent_mix` and `agent_configs` — which agents to spawn

`sim_scenario.schema.json` requires a top-level **`agent_mix`** object: a map of agent
type → count. This is the schema-validated summary of the population. The detailed
per-agent parameters live in **`agent_configs`**; the per-type counts in `agent_configs`
must match `agent_mix`. Example `agent_mix`:

```json
"agent_mix": { "NoiseTrader": 3, "MarketMaker": 1 }
```

```json
"agent_configs": [
  {
    "agent_type": "NoiseTrader",
    "count": 3,
    "params": {
      "order_size_mean": 10,
      "order_size_std": 2,
      "arrival_rate_hz": 2.0,
      "price_offset_ticks": 5
    }
  },
  {
    "agent_type": "MarketMaker",
    "count": 1,
    "params": {
      "spread_ticks": 2,
      "depth_levels": 3,
      "size_per_level": 10,
      "rebalance_interval_ns": 1000000000
    }
  }
]
```

The exchange is not listed here. It is constructed by the harness, so `agent_configs` carries only
the trading population. The example above is the real `agent_configs` of
`units/t3-s001-price-time-priority`, so it runs as written.

- **`agent_type`** — a bare key from the fork's `AGENT_REGISTRY`, not a Python import path. The
  loader looks the string up directly, so a dotted class path will not resolve. The registry
  contains `NoiseTrader`, `MarketMaker`, `ValueTrader` and `MomentumTrader`.
- **`count`** — number of independent instances. Each gets a deterministically derived
  sub-seed.
- **`params`** — agent-specific parameters. See the agent class docstring. Unknown keys
  cause a validation error.

**Required:** at least one `ExchangeAgent` with `"count": 1`. Multiple exchange agents in
one scenario are not supported.

### `exchange_config` — exchange rules

```json
"exchange_config": {
  "tick_size": 0.01,
  "lot_size": 1,
  "stp_policy": "cancel_newest"
}
```

- **`tick_size`** — minimum price increment. Use `0.01` for US equity-style pricing.
  Must be exactly representable in double precision (powers of 2 or simple decimals).
- **`lot_size`** — minimum order quantity in shares. Use `1` for single-share granularity.
- **`stp_policy`** — self-trade prevention rule. Options:
  - `"cancel_newest"` — cancel the new (aggressor) order when it would self-trade
  - `"cancel_oldest"` — cancel the resting order instead
  - `"cancel_both"` — cancel both orders
  - `"none"` — allow self-trades (no STP)

### `latency_config` — network delay model

```json
"latency_config": {
  "model": "log_normal",
  "params": { "mu": 6.5, "sigma": 0.5 }
}
```

- **`model`** options:
  - `"constant"` — every agent has the same one-way latency: `{"latency_ns": 500000}`
  - `"log_normal"` — latency drawn from log-normal per message: `{"mu": <float>, "sigma": <float>}`. Units: nanoseconds (so `mu=6.5` ≈ 665 ns median).
  - `"empirical"` — reads a CDF table from a local file: `{"cdf_path": "latency_cdf.csv"}`. Path is relative to the scenario directory.
  - `"pareto"` (sealed scenarios only) — heavy-tailed: `{"alpha": 1.5, "x_min_ns": 100000}`

### `oracle_config` — fundamental value process

```json
"oracle_config": {
  "type": "mean_reverting",
  "params": { "mu": 100.0, "theta": 0.1, "sigma": 0.02, "s0": 100.0 }
}
```

- **`type`** options:
  - `"mean_reverting"` — Ornstein-Uhlenbeck process: `{"mu", "theta", "sigma", "s0"}`
  - `"gbm"` — Geometric Brownian Motion: `{"mu", "sigma", "s0", "dt_ns"}`
  - `"jump_diffusion"` — GBM plus Poisson jumps: `{"mu", "sigma", "s0", "jump_freq_per_min", "jump_sigma", "dt_ns"}`
  - `"external_csv"` — read a pre-computed series: `{"csv_path": "oracle.csv", "price_col": "price", "time_col": "time_ns"}`

### `output_config` — what to emit in the trace

```json
"output_config": {
  "trace_cols": ["t_ns", "agent_id", "msg_type", "side", "price", "size", "order_id"],
  "events_fields": ["n_events", "wall_clock_sec", "events_per_sec", "trace_sha256"]
}
```

- **`trace_cols`** — subset of the full column set in `templates/trace_column_registry.json`.
  At minimum include all seven columns above; some verifiers require them.
- **`events_fields`** — keys in `events.json`. The fields `scenario_id`, `seed`, and
  `trace_sha256` are always emitted and do not need to be listed. `events.json` now also
  carries `peak_memory_bytes` and `gpu_seconds` telemetry alongside these keys.
- **`message_trace.parquet`** — a message-level kernel ledger written alongside
  `trace.parquet`. Required by Exchange-Protocol (7), Reactive-Agent (8), and batch units;
  it feeds the latency/causality and `g3.5` checks.

---

## Step 3: Generate the reference trace

With `scenario.json` in place, run the simulation using the ABIDES fork:

```bash
python -m abides_fork.simulate \
    --config scenario.json \
    --seed <seed> \
    --out reference/trace.parquet
```

The `--seed` value must match the `seed` field in `scenario.json`. The framework writes:
- `reference/trace.parquet` — the event trace
- `reference/events.json` — run metadata including `trace_sha256` and `events_per_sec`

**Why does this step matter?** The `trace_sha256` in `events.json` is the sealed ground
truth. Every candidate simulator is checked against this exact trace. If you later re-run
the simulation and get a different hash, the scenario is non-deterministic and must be
fixed before sealing.

---

## Step 4: Verify determinism

Run the simulation a second time with the same seed and compare the SHA-256 hashes of the
two traces:

```bash
# Second run:
python -m abides_fork.simulate \
    --config scenario.json \
    --seed <seed> \
    --out reference/trace_check.parquet

# Compare hashes:
python - <<'EOF'
import hashlib, sys

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

a = sha256_file("reference/trace.parquet")
b = sha256_file("reference/trace_check.parquet")
if a == b:
    print("DETERMINISM OK:", a)
else:
    print("MISMATCH")
    print("  run 1:", a)
    print("  run 2:", b)
    sys.exit(1)
EOF
```

If the hashes differ, the scenario is non-deterministic. Do not proceed. Common causes:

- **`time.time()` in agent code** — use the simulated clock, not wall-clock time.
- **Un-seeded random draws in oracle code** — every RNG call must use the seeded RNG.
- **Python dict iteration order** — if your code iterates a dict whose keys are agent IDs
  and uses the iteration order to make decisions, the result can vary across Python
  versions. Use a sorted list.
- **Threads** — any background thread that touches simulation state introduces
  non-determinism. Avoid threads in scenario-generating code.

Once you have two matching hashes, copy the `trace_sha256` value from `events.json` into
`card.toml` under `[scoring.params].reference_sha256`. This value cannot be changed without
re-authoring the scenario.

---

## Step 5: Set semantic-equality tolerances in `card.toml`

Open the `card.toml` file (copy from `templates/card.toml`) and fill in the scoring
section. The required fields depend on the scenario family.

### All scenarios (required)

```toml
[task]
track = "simulation"

[scoring]
verifier = "t3.semantic_stylized"
metric = "events_per_sec"

[environment]
network = "none"
gpu = true          # every Track-3 unit runs on the single-GPU box; using the device is optional

[scoring.params]
reference_sha256 = "<64-char hex from events.json>"
```

### Family 1 and Family 6 (Tier A)

```toml
[scoring.params]
timestamp_tolerance_ns = 1000    # ±1 microsecond on fill timestamps
```

No other tolerance is needed. One wrong fill in any field = failure.

### Family 3 (Tier A, additional Kendall-τ check)

```toml
[scoring.params]
timestamp_tolerance_ns = 1000
kendall_tau_min = 0.999    # minimum rank correlation on event arrival sequence
```

### Family 2 (Tier B)

```toml
[scoring.params]
spread_bps_tolerance = 10.0                  # ±10 basis points on time-averaged spread
stylized_fact_ceilings = { ks = 0.08 }       # return-distribution KS (log-returns)
```

### Family 4 (Tier B, oracle RMSE)

```toml
[scoring.params]
stylized_fact_ceilings = { ks = 0.08 }
spread_bps_tolerance = 10.0
oracle_rmse_tolerance_pct = 20.0    # ±20% of reference RMSE
oracle_rmse_reference = <float>     # RMSE from reference trace; compute manually
```

To compute `oracle_rmse_reference`, extract the mid-price series from `reference/trace.parquet`
and the oracle series from the simulation, then compute `sqrt(mean((mid - oracle)^2))`.

### Family 5 (Tier B, stylized-fact ceilings)

```toml
[scoring.params]
stylized_fact_ceilings = { ks = 0.08, acf_abs_l2 = 0.12, hill_abs = 1.5, depth_js = 0.10 }
```

Confirm the reference trace passes its own ceilings before sealing (see Step 6).

---

## Step 6: Confirm stylized-fact ceilings and throughput measurement

### Stylized-fact self-check (mandatory for all families)

Run the stylized-fact checker on the **reference trace itself**. A reference that fails its
own ceilings must not be sealed — it would make the admissibility gate unachievable.

```python
from qfbench2_common.scoring.stylized_facts import stylized_fact_report, admissible
import pandas as pd, numpy as np

# Load reference trace
ref = pd.read_parquet("reference/trace.parquet")
# Extract mid-price series (from QUOTE_UPDATE events)
bids = ref[(ref.msg_type=="QUOTE_UPDATE") & (ref.side=="BID")].set_index("t_ns")["price"]
asks = ref[(ref.msg_type=="QUOTE_UPDATE") & (ref.side=="ASK")].set_index("t_ns")["price"]
mid = ((bids + asks) / 2).ffill().dropna().values.astype(float)

report = stylized_fact_report(mid, mid, lags=20, k=100)
ok, breaches = admissible(report, {
    "ks": 0.08, "acf_abs_l2": 0.12, "hill_abs": 1.5, "depth_js": 0.10
})

if ok:
    print("Self-check PASSED:", report)
else:
    print("Self-check FAILED:", breaches)
    # Fix the oracle or agent configuration and re-run from Step 3.
```

Why does this matter? The stylized-fact check compares your candidate's output to the
reference. If the reference itself does not pass its own ceilings, then any correct
implementation will also fail — the ceiling is set in the wrong place.

Common causes of self-check failure:
- **Pure random walk oracle** (no mean-reversion) — produces a return series with ACF ≈ 0
  at all lags, no volatility clustering. Increase `theta` (mean-reversion speed) in the
  oracle params.
- **Frozen book** (no market makers, low order flow) — produces very few fill events,
  making the stylized-fact estimates unstable. Add a market-maker agent or increase
  noise-trader arrival rate.
- **Too-short horizon** — Family 5 scenarios need at least 2 hours of simulated time for
  the statistics to be stable. Shorter horizons increase variance, potentially failing the
  self-check by chance.

### Throughput measurement (Family 6 and the sealed benchmark)

For Family 6 scenarios, run the throughput measurement to confirm the scenario is actually
demanding and that the `events_per_sec` is consistent with the trace size:

```bash
python throughput/timer.py \
    --image track3-abides-baseline:latest \
    --scenario scenario.json \
    --runs 5
```

For this local command, check that:
- Run 1 (warm-up) is excluded from the median calculation.
- The median of runs 2–5 is the local throughput measurement for the baseline.
- The baseline `events_per_sec` is consistent with `n_events ÷ wall_clock_sec` within
  ±5%.

These are local timer defaults, not the current provisional Development service or the
planned official Final protocol. Final repeat counts, warm-up treatment and runtime identity
need their own published commitment; see [the timing profiles](../README.md#how-throughput-is-measured).

---

## Review checklist before sealing

Before submitting a scenario for inclusion in the sealed suite, verify every item:

- [ ] `scenario.json` carries every required field listed above and matches the shape of
      `templates/scenario.json`. Do **not** validate against the toolkit's
      `sim_scenario.schema.json` — it is stale and rejects all 132 shipped scenarios (see the
      note in "Top-level required fields"). Do not create a local copy to work around this.
- [ ] Running the simulation twice with the same seed produces identical `trace_sha256`
      values.
- [ ] The reference trace passes the stylized-fact self-check (all four gated metrics —
      ks, acf_abs_l2, hill_abs, depth_js — at or below their ceilings when
      `candidate = reference`).
- [ ] `card.toml` field `[task].track` is `"simulation"`.
- [ ] `card.toml` field `[scoring].verifier` is `"t3.semantic_stylized"`.
- [ ] `card.toml` field `[scoring].metric` is `"events_per_sec"`.
- [ ] `card.toml` field `[environment].network` is `"none"`.
- [ ] `card.toml` field `[environment].gpu` is `true`. Every Track-3 unit runs on the same
      pinned single-GPU box, so this is `true` on every Track-3 card without exception;
      using the device is optional and the ranked metric is unchanged. Do not set it to
      `false` to signal "this unit is CPU-shaped" — that is what `gpu_seconds > 0` in
      `events.json` reports.
- [ ] `card.toml` field `[scoring.params].reference_sha256` is set to the `trace_sha256`
      from `events.json`.
- [ ] `manifest.json` lists every file in the scenario directory with its SHA-256
      checksum. Update with:
      **not** `qfbench2 manifest build` — that command defaults `--data-subdir` to
      `environment/data`, which is Track 1's layout. On a Track 3 unit that directory does
      not exist, so it writes `files: []`, discards every existing row, and
      `qfbench2 manifest verify` then reports OK because an empty manifest covers nothing
      (measured 2026-08-24). Until that is fixed, update the changed rows in place and
      confirm with `qfbench2 manifest verify <scenario_dir>`. There is no `manifest.update`.
- [ ] The scenario directory contains no file that references an external URL or remote
      path. All data files (`cdf_path`, `csv_path`, `oracle.csv`, etc.) are local, relative
      paths included in `manifest.json`.
- [ ] The scenario's `README.md` (in `units/<scenario_dir>/`) explains what the simulation
      tests and what a valid accelerated implementation must preserve.
- [ ] For Family 1 and 6 scenarios: run `run_regression.py` with the reference image and
      confirm the scenario passes.

---

## Why each step exists

| Step | Why it cannot be skipped |
|---|---|
| Step 1: Pick family | The family determines the verifier, tier, and ceilings. Wrong family = wrong check. |
| Step 2: Write `scenario.json` | Without a valid config, the harness cannot run. Schema validation catches typos early. |
| Step 3: Generate reference trace | The reference trace is the answer key. Without it, the check has nothing to compare against. |
| Step 4: Verify determinism | A non-deterministic scenario produces a different trace each run. The hash comparison becomes meaningless. |
| Step 5: Set tolerances | Tolerances in `card.toml` control how strict the verifier is. Default tolerances may be too strict or too loose for your scenario's parameters. |
| Step 6: Self-check and throughput | If the reference fails its own ceilings, no participant can pass. If throughput measurement is skipped, you do not know whether the scenario is actually demanding. |
