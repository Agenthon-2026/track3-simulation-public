# Semantic Regression Suite

## 1. Purpose

The Semantic Regression Suite is a set of **65 deterministic public regression scenarios**, plus a sealed set whose size is not disclosed, designed to verify that a candidate simulator preserves the full matching-engine semantics of the reference ABIDES implementation. Passing this suite is the **first of two admissibility gates** for Track 3. A candidate that does not pass the regression suite is ineligible for throughput scoring regardless of its performance characteristics.

> **The scenarios are sealed; the evaluation logic is not.** Every gate, tolerance and ceiling this document describes is published here and implemented in code you can read — `qfbench2_track_simulation/semantics.py`, `qfbench2_track_simulation/scoring.py`, `qfbench2_track_simulation/batch.py` and `run_regression.py` in this directory. What stays sealed is which scenarios you are graded on, with what parameters and seeds, and their reference traces.

The suite is structured to cover the core correctness invariants that any compliant simulator must uphold:

- **Price-time priority ordering.** When two resting orders at the same price compete for a fill, the order that arrived first (by simulated timestamp) must fill first. No tie-breaking by agent ID, order size, or any other criterion is permissible.
- **Partial-fill correctness.** A resting order that is partially filled must remain on the book at the reduced size. Its timestamp priority must not be reset by the partial fill. Subsequent fills must consume remaining quantity before touching orders at the same price that arrived later.
- **Cancel/replace atomicity.** A cancel-replace (modify) operation must be processed as a single atomic step from the perspective of other agents' order streams. It is not permissible for another agent's order to fill against the pre-modify resting order after the cancel half of the operation has been processed but before the replace half is processed.
- **Self-trade prevention.** The STP policy declared in `exchange_config.stp_policy` must be applied consistently. The verifier checks that no fill event involves the same firm ID on both sides of the trade when STP is enabled.
- **Market-order fill semantics.** A market order must fill against the best available resting limit orders in price-time priority order, walking the book until the order quantity is exhausted or the book is empty. Unfilled residual market-order quantity must be cancelled (not left resting) unless the scenario declares `allow_market_order_rest: true`.
- **Order-book state consistency.** A correct simulator keeps the book internally consistent — bids strictly below asks except transiently during a fill, positive resting quantities, no cancelled or fully filled order still resting. Note how this is *enforced*: the verifier does **not** reconstruct a book from your trace and test it. There is no crossed-book check, no phantom-order check and no fill-omission check anywhere in the harness or the scorer. These properties are enforced indirectly, because a trace that violates them cannot reproduce the reference's exact event multiset and sequence. Expect a coverage or fill-sequence breach, not a book-consistency diagnostic.

---

## 2. Suite Structure

The suite contains 65 public regression scenarios drawn from the eight scenario families (F1–F8) defined in `../docs/CATEGORIES.md`:

- **F1 — Matching-Engine Semantics** (Tier A) — includes the dense high-churn memory/replay (MR) scenarios.
- **F2 — Agent-Mix / Market-Regime** (Tier B)
- **F3 — Latency Topology / Profile** (Tier A)
- **F4 — Oracle-Noise Robustness** (Tier B)
- **F5 — Calibration / Stylized-Fact** (Tier B)
- **F6 — Throughput / Scale** (Tier A) — includes the batched multi-scenario (BatchMarketSim) scenarios.
- **F7 — Exchange-Protocol** (Tier A)
- **F8 — Reactive-Agent** (Tier A)

Tiering follows `../docs/CONCEPTS.md` and `../docs/CATEGORIES.md`: Tier A (exact) covers Families 1, 3, 6, 7, 8; Tier B (statistical) covers Families 2, 4, 5.

The **per-family breakdown** of the 65 public regression scenarios is:

| Family | Tier | Public Scenario Count |
|--------|------|-----------------------|
| F1 — Matching-Engine Semantics (incl. MR) | A | 14 |
| F2 — Agent-Mix / Market-Regime | B | 11 |
| F3 — Latency Topology / Profile | A | 9 |
| F4 — Oracle-Noise Robustness | B | 0 |
| F5 — Calibration / Stylized-Fact | B | 12 |
| F6 — Throughput / Scale (single-scenario) | A | 6 |
| F7 — Exchange-Protocol | A | 7 |
| F8 — Reactive-Agent | A | 6 |
| **Total (single-scenario)** | | **65** |

Counted from `scenarios/*.json` (excluding `index.json`), whose `scenario_id` values resolve to the matching unit under `../units/`; `scenarios/index.json` carries the same `"count": 65`.

**How 65 scenarios relate to the 72 units in `../units/`.** The extra seven are: the six batched multi-scenario BatchMarketSim units (`t3-gbatch-*`), which fall under F6 but run through the `simulate-batch` verb and carry their own `batch.json` + `scenarios/` rather than a flat scenario file here; and the worked exemplar `t3-EXAMPLE-vectorized-matching`, which is documentation and is not part of the regression run.

The 65 public scenarios are distributed in this repo with reference traces (the flat `*.json` files in `scenarios/`). A further set of scenarios is sealed (operator-only); its size is not disclosed.

**Correction — the six `gb_*.json` scenario `description` fields overstate throughput and event count.** All six carry the same copy-pasted sentence, "driving ~68k events at ~20k events/sec". No `gb_*` unit was measured on the evaluation fleet, and the repository's own shipped `../units/t3-gb-*/events.json` disagree with both numbers:

| Scenario / unit | `n_events` shipped | `events_per_sec` shipped | Description claims |
|---|---|---|---|
| `gb_base_30agent_30s` | 68,351 | 13,171 | ~68k @ ~20k |
| `gb_pop_128_agents` | 303,603 | 10,875 | ~68k @ ~20k |
| `gb_highfreq_40hz_60s` | 500,530 | 9,674 | ~68k @ ~20k |
| `gb_horizon_240s` | 555,032 | 11,317 | ~68k @ ~20k |
| `gb_pop_horizon_scale` | 725,591 | 11,212 | ~68k @ ~20k |
| `gb_mega_throughput` | 1,168,360 | 10,571 | ~68k @ ~20k |

Only `gb_base_30agent_30s` is close on event count; the rest are understated by up to 17x, because each unit deliberately scales a different throughput axis away from that base. The rate is overstated for all six. The `description` strings are inert metadata — nothing reads them, and they are byte-pinned by each unit's `manifest.json` — so they are left unchanged here rather than edited, and this table is the number to trust. Neither figure is a target: the hardware behind the shipped runs is not recorded, and `wall_clock_sec` there covers the simulation loop rather than the whole container.

Why the files are not edited: each `gb_*.json` is byte-identical to its unit's `scenario.json`, and that byte string is sha256-pinned in the unit's `manifest.json`. Editing the prose would break the manifest checksum `.github/validate_units.py` enforces, for no gain — the strings gate nothing. Verify both claims from a clone:

```bash
diff scenarios/gb_mega_throughput.json ../units/t3-gb-mega-throughput/scenario.json && echo identical
shasum -a 256 ../units/t3-gb-mega-throughput/scenario.json
grep -c "$(shasum -a 256 ../units/t3-gb-mega-throughput/scenario.json | cut -d' ' -f1)" \
     ../units/t3-gb-mega-throughput/manifest.json
```

The same applies to the wider throughput picture: the ~65,000 events/sec ABIDES baseline this repository used to publish as the figure to beat is **withdrawn**, no fleet-measured baseline exists yet, and the shipped references measure **13,793 events/sec geomean** on unspecified hardware. See `../baselines/README.md` §1.

### Example public scenarios

All public scenarios are real files in `scenarios/` (`index.json` plus the named JSON configs); their ids are fixed and must not be renamed or renumbered. Three representative examples:

**s001-price-time-priority** (`s001_price_time_priority.json`, Family 1, Tier A). A simple 3-agent book that verifies price-time priority fill ordering across 50 limit orders. The reference trace specifies the exact fill sequence.

**s012-partial-fill-cancel-race** (`s012_partial_fill_cancel_race.json`, Family 1, Tier A). A partial fill interleaved with a cancel request on the same order; verifies that the cancel/fill interaction is processed atomically.

**s019-latency-jitter-kendall** (`s019_latency_jitter_kendall.json`, Family 3, Tier A). Log-normal latency jitter across 10 agents; verifies that the message-ordering Kendall-tau against the reference is >= 0.999.

### The sealed scenarios

The sealed scenarios are **authored and sealed** in the private repository. They span all eight
families, in both single-scenario and batched (BatchMarketSim) form, and they stress the same
family properties as the public examples at harder settings — deeper books and larger populations
(F1), more extreme agent-mix ratios (F2), heavier-tailed latency profiles (F3), jump-diffusion
oracle stress (F4), longer calibration runs (F5), larger-scale throughput scenarios (F6),
exchange-response fidelity (F7), and reactive-cascade shocks (F8).

How many there are, how they are labelled, which family each label belongs to, and their
parameters, seeds and reference traces are all sealed until after the competition. The one sealed
identifier named in participant-facing documentation is `SS-BENCH`, the throughput benchmark you
are ranked on — named so that the ranking is describable, not because its contents are known.

What is **not** sealed is how they are graded. They use the same `scenario.json` schema, the same
family tiers, the same tolerance model and the same checks as the public scenarios, all documented
in this file and implemented in `../qfbench2_track_simulation/`.

---

## 3. Tolerance Model

Tolerances are divided into two tiers. The tier assignment is fixed per family; a unit card may state it explicitly in `[scoring.params].semantic_tier`, and all 72 public cards do.

> **This local harness is stricter than the official gate, on purpose. Know the difference.**
>
> | | `run_regression.py` (here) | Official CodaBench gate |
> |---|---|---|
> | Tier-A exact check | on **every** scenario | only on Tier-A families |
> | Tier-B statistical check | on Tier-B scenarios, *in addition to* Tier A | **instead of** Tier A |
> | Stylized-fact ceilings | on **every** scenario | only on Family 5 |
>
> Both call the same functions in `../qfbench2_track_simulation/semantics.py`, so a metric never means two different things. What differs is which checks are applied where. A green run here is necessary but not sufficient. This suite applies some checks more widely than the official gate and omits others entirely, so it does not imply the official verdict: in particular it never reads `message_trace.parquet`, so the message-ledger checks the official gate runs are not exercised here at all. A red run here on a Tier-B scenario's fill sequence, or on a non-Family-5 scenario's stylized facts, is worth fixing but is not on its own an official failure.

### Tier A — Structural Semantic Equality (Families 1, 3, 6, 7, 8)

Applied to: all scenarios in Families 1, 3, 6, 7, and 8.

Fill events must appear in identical sequence in the candidate trace and the reference trace. Sequence equality means:

- The same number of fill events in the same order.
- Each fill event has the same `order_id`, `side`, `price`, and `size` as the corresponding reference event.
- Simulated timestamps may differ by at most +/- 1 microsecond (1,000 nanoseconds) from the reference timestamp.

A single deviation in any of these fields — a transposed fill, a missing fill, a fill at the wrong price, or a timestamp outside the tolerance window — is an immediate FAIL for that scenario. There is no partial credit. Every Tier A scenario must pass individually.

Two further Tier-A checks apply to **every** Tier-A family, not only Family 3:

- **Exact event count.** The candidate must emit exactly as many rows as the reference, and the emitted row count is the ranked numerator, so a padded or truncated trace is refused here. Determinism for one implementation and seed does not guarantee the reference trace. The baseline assigns separate random states in a fixed order; changing the seed or draw assignment can change stochastic message delivery. The checks inspect the emitted trace, not the RNG implementation. See the Tier-A note in the top-level `README.md`.
- **Exact bidirectional event coverage, plus Kendall-tau ordering.** Events are keyed on `(order_id, msg_type, agent_id, side, price, size)` with per-key occurrence. Every reference event must appear in the candidate and every candidate event must appear in the reference — a missing event and an extra event are each a breach — and the Kendall-tau rank correlation of the two orderings, computed over the *whole* trace, must be >= 0.999 (`[scoring.params].kendall_tau_floor`).

An earlier revision of this document presented Kendall-tau as a Family-3 extra. It is not: `semantics.check_tier_a` runs the same comparison for Families 1, 3, 6, 7 and 8.

### Tier B — Statistical Proximity (Families 2, 4, 5)

Applied to: all scenarios in Families 2, 4, and 5.

Two metrics are evaluated per scenario, and both are computed from the candidate's own trace — there is no comparison against any series the candidate does not emit:

- **KS distance.** The two-sample Kolmogorov-Smirnov statistic between the candidate's mid-price log-return series and the reference's must be <= 0.08 (the same calibrated return-distribution KS ceiling as the stylized-fact gate). The mid-price series is reconstructed from `QUOTE_UPDATE` bid/ask pairs, forward-filled; fill prices are the fallback when a scenario has fewer than two quote events.
- **Spread proximity.** The time-averaged bid-ask spread, expressed in basis points of the reference mid-price level, must be within `[scoring.params].spread_bps_tolerance` (default 10 bps) of the reference's.

Numeric sanity runs first on both traces: a non-finite `t_ns`, `price` or `size`, or a non-positive `price` or `size`, fails the scenario before any statistic is computed.

> **No oracle-tracking RMSE check runs.** An earlier revision of this document listed a third Tier-B metric — mid-price RMSE against the oracle's fundamental-value series, within +/- 20% of the reference's. It is not evaluated. The oracle's fundamental path is not present in `trace.parquet`, so it cannot be computed from a submission's output; the public scorer does not implement it, and the private scorer's branch for it is unreachable because nothing supplies the reference RMSE or the oracle series it needs.
>
> **There is no 80% majority rule.** An earlier revision said a Tier-B family passed if at least 80% of its scenarios did. No scorer implements any such aggregation. `run_regression.py` reports `all_pass` only when `failed == 0 and errored == 0`, and the CodaBench gate grades each unit on its own. **Every Tier-B scenario must pass, exactly like Tier A.** Assume no slack.

The per-scenario pass/fail breakdown is reported in `results/report.json` to assist debugging.

---

## 4. How to Run

The regression runner is `run_regression.py` in this directory.

Build the reference cache first (once per clone; it symlinks into `units/`):

```
python build_reference_cache.py
```

```
python run_regression.py \
    --candidate-image ghcr.io/my-org/my-simulator:latest \
    --scenarios-dir scenarios/ \
    --reference-dir reference_traces/ \
    --output-dir results/ \
    --workers 4
```

**Arguments:**

- `--candidate-image` — Docker image reference for the candidate simulator. The harness pulls this image and runs it once per scenario (network disabled), mounting the scenario read-only at `/input/scenario.json` and writing to `/output/`. The image must expose the same CLI the baseline does: `simulate --config /input/scenario.json --out /output/trace.parquet` for single scenarios (and `simulate-batch --batch-dir /input/scenarios --out-dir /output` for batched BatchMarketSim units — see below). Exchange-protocol and reactive-agent units additionally require the container to write a `/output/message_trace.parquet` message-level kernel ledger. The seed is read from inside `scenario.json`; the harness does **not** pass a `--seed` flag.
- `--scenarios-dir` — directory searched recursively for scenario `*.json` files (`index.json` is skipped). Each file's `scenario_id` field (a UUID) selects its reference trace. Public scenarios are the flat `*.json` files in `scenarios/`; sealed scenarios are loaded from `--reference-dir` automatically.
- `--reference-dir` — path to the reference trace store. Must contain one subdirectory per scenario, named by `scenario_id`, each containing `trace.parquet` and `events.json`.
- `--output-dir` — directory where results are written. Created if it does not exist.
- `--workers` — number of parallel scenario runs. Each worker runs one scenario at a time in its own Docker container. Default: 1. Do not exceed the number of physical CPU cores; over-parallelisation causes cross-scenario interference and unreliable timing.

**Batched (BatchMarketSim) units.** Throughput-scale units that bundle N independent sub-scenarios are run with `simulate-batch --batch-dir /input/scenarios --out-dir /output`. Each sub `<sub>` writes `/output/<sub>/trace.parquet`, `/output/<sub>/message_trace.parquet`, and `/output/<sub>/events.json`; the container also emits `/output/batch_events.json` with the aggregate (`n_scenarios`, `total_events`, `events_per_sec`, and a `per_scenario` block). A **per-sub isolation gate** requires each sub's output to reproduce its isolated single-scenario reference — batching may not perturb any sub's trace relative to running it alone — in addition to the aggregate throughput measurement.

**Output files:**

- `results/report.json` — structured report with one entry per scenario. Each entry contains: `scenario_id`, `family`, `tier`, `status` (`pass` or `fail`), `metrics` (the computed values for each checked metric), and `breach_detail` (populated only on failure, with the specific field and value that caused the failure).
- `results/summary.txt` — human-readable pass/fail table with per-family aggregates.

**Exit codes:**

- `0` — every scenario passed, individually, in both tiers. `RegressionReport.all_pass` is `failed == 0 and errored == 0`; there is no per-family threshold.
- `1` — one or more failures.

**Example report entry (passing):**

```json
{
  "scenario_id": "s001-price-time-priority",
  "family": 1,
  "tier": "A",
  "status": "pass",
  "metrics": {
    "fill_sequence_match": true,
    "max_timestamp_delta_ns": 312
  },
  "breach_detail": null
}
```

**Example report entry (failing):**

```json
{
  "scenario_id": "s001-price-time-priority",
  "family": 1,
  "tier": "A",
  "status": "fail",
  "metrics": {
    "fill_sequence_match": false,
    "max_timestamp_delta_ns": 1840
  },
  "breach_detail": {
    "event_index": 4821,
    "field": "order_id",
    "reference_value": "ord-00291",
    "candidate_value": "ord-00294",
    "description": "Fill sequence diverged at event 4821: candidate filled order ord-00294 but reference filled ord-00291 (price-time priority violation)."
  }
}
```

---

## 5. Adding New Public Scenarios

To add a new scenario to the public portion of the suite:

1. Follow the full authoring process in `../AUTHORING-GUIDE.md`. The scenario must belong to one of the eight scenario families (F1–F8) to be eligible for inclusion in the regression suite.
2. Place the completed scenario (and any referenced data files) as a flat `*.json` file in `scenarios/`, following the existing naming (e.g. `s001_price_time_priority.json`); the `scenario_id` UUID lives inside the file.
3. Generate the reference trace and place `trace.parquet` and `events.json` in `reference_traces/<scenario_id>/` (the subdirectory named by the scenario's UUID — the key the harness looks up).
4. Add an entry to `scenarios/index.json` with the fields used by the existing public entries: `scenario_id`, `file`, `family`, `tier`, and `description`.
5. Run the full suite locally to confirm the new scenario passes against the reference trace: `python run_regression.py --candidate-image <reference-image> ...`.
6. Submit a pull request. CI will re-run the full suite against the reference image and reject the PR if any existing scenario regresses or the new scenario fails.

Scenario ids are unique and stable. Do not reuse or renumber the id of an existing scenario even if an earlier scenario is removed.

---

## 6. Sealed Scenarios

The sealed scenarios are stored in the Track-3 private repository. They are not distributed to participants and are not accessible through the public registry; their count, labels, configs, seeds, and reference traces stay sealed until after the competition.

Sealed scenarios use the same `scenario.json` schema, the same tolerance model, and the same `run_regression.py` runner as public scenarios. The harness loads them from a path specified by the `--sealed-scenarios-dir` argument, which is populated automatically in the competition evaluation environment.

Sealed scenario configs are generated by the scenario authoring process described in `AUTHORING-GUIDE.md`, with one additional constraint: sealed seeds are drawn so that **no sealed scenario shares a seed with any public scenario**. A candidate therefore cannot reproduce a sealed trace by exhaustively re-running the seeds it can see. The numeric ranges the seeds are drawn from are part of the sealed configuration and are not published.

Sealed scenarios are reviewed and re-generated for each competition cycle. A scenario is rotated out if its reference trace has been publicly disclosed (e.g., through a participant leak) or if its parameter family has been exhausted by public scenario coverage.
