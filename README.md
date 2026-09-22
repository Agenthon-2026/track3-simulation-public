# Track 3 — Semantic-Preserving Market Simulation

## Executive summary (read this first)

Track 3 asks one question: **can you build a faster market simulator that still behaves
like a real stock exchange?**

You receive the open-source **ABIDES** simulator as your starting point. ABIDES is correct
but slow. Your job is to produce a faster version — submitted as a Docker image — that:
1. Passes a correctness check over the 72 public units plus a sealed scenario set (the **semantic regression suite**)
2. Passes a 4-metric realism check (the **stylized-fact admissibility gate**)
3. Is then ranked by speed (`events_per_sec` on a sealed benchmark scenario)

Only simulators that pass both gates receive a leaderboard rank. Speed without correctness
scores zero.

**Scope.** Track 3 is about the *discrete-event market simulation* only — the `abides-core` kernel and
the `abides-markets` exchange / agents / order book. You do **not** need `abides-gym` (the
reinforcement-learning wrapper); it is out of scope here, and its legacy dependencies (`gym`, `ray`, …)
are unnecessary and do not build on modern Python. Install `abides-core` + `abides-markets` only —
by naming those two subdirectories, as Step 1 of the quick start does. ABIDES has no top-level
package and no `[all]` extra, so there is no repository-root install that could honour this scope
anyway.

New to this track? Read `docs/CONCEPTS.md` first — it defines every term in plain English.
Then come back here for the submission format and quick-start steps. The current Development
service provides provisional practice scores; see [how throughput is measured](#how-throughput-is-measured)
for the distinction from planned official Final timing.

---

## What this public repo contains

This repo is your starter kit. It contains:
- The public half of the regression suite (65 scenarios, with reference traces)
- The throughput timer (for local benchmarking)
- The ABIDES baseline: the `abides_fork` `simulate` adapter and a `Dockerfile` that fetches
  upstream ABIDES at the pinned commit and builds the baseline image (`baselines/`)
- Documentation in `docs/`

It does **not** contain:
- The sealed scenarios used in the final correctness check (how many there are is not published)
- The sealed reference traces those scenarios compare against
- The sealed throughput benchmark scenario (`SS-BENCH`)
- The final scorer logic (the private gate-and-rank implementation)

---

## Submission format

Your submission is a **Docker image** that accepts two verbs — `simulate` (a single scenario)
and `simulate-batch` (a batch of scenarios run in one pass):

```bash
docker run --rm \
    --network none \
    -v /path/to/scenario.json:/input/scenario.json:ro \
    -v /path/to/output/:/output/ \
    <your-image>:latest \
    simulate \
    --config /input/scenario.json \
    --out /output/trace.parquet
```

For batched multi-scenario throughput runs (family 6, throughput-scale), the image also accepts
the `simulate-batch` verb, which runs N independent sub-scenarios in one pass:

```bash
docker run --rm \
    --network none \
    -v /path/to/scenarios/:/input/scenarios:ro \
    -v /path/to/output/:/output/ \
    <your-image>:latest \
    simulate-batch \
    --batch-dir /input/scenarios \
    --out-dir /output
```

`simulate-batch` writes `/output/<sub>/{trace,message_trace}.parquet` + `events.json` for each
sub-scenario, plus an aggregate `/output/batch_events.json`. Each sub-scenario is checked under a
per-sub **isolation gate** — its output must reproduce the trace it produces when run in isolation —
alongside the aggregate throughput number.

For a single `simulate` run, the image must write the following files to `/output/`:

### `trace.parquet`

A columnar Parquet file (Snappy-compressed) with one row per exchange event. Required
columns:

| Column | Type | Description |
|---|---|---|
| `t_ns` | int64 | Event timestamp in nanoseconds since simulation epoch |
| `agent_id` | int32 | Originating agent identifier |
| `msg_type` | string | Event type (see below) |
| `side` | string | `BID`, `ASK`, or null |
| `price` | int64 | Price in integer ticks |
| `size` | int64 | Order size in shares |
| `order_id` | int64 | Unique order identifier, stable across fill/cancel events |

Valid `msg_type` values: `ORDER_SUBMITTED`, `ORDER_ACCEPTED`, `ORDER_FILLED`,
`PARTIAL_FILL`, `ORDER_CANCELLED`, `ORDER_REPLACED`, `QUOTE_UPDATE`.

### `events.json`

A flat JSON file with these required fields:

```json
{
  "scenario_id": "<UUID matching the scenario config>",
  "seed": 42,
  "n_events": 1482931,
  "wall_clock_sec": 12.4,
  "events_per_sec": 119591.2,
  "trace_sha256": "<SHA-256 of trace.parquet>",
  "peak_memory_bytes": 2147483648,
  "gpu_seconds": 3.1
}
```

The `peak_memory_bytes` and `gpu_seconds` fields are resource telemetry (feeding the secondary
diagnostics in `throughput/`); the other six fields are the core record.

The `events_per_sec` value must be consistent with `n_events ÷ wall_clock_sec` within
±5%. The harness verifies this consistency check and disqualifies submissions that report
inflated throughput.

On the Development board your `events_per_sec` is a self-report: no organizer clock times a
Development run, so the number you write is the score input. It is therefore bounded — a
self-reported single-market rate above 1e9 events/sec is refused as implausible, and for a batch
unit that bound is multiplied by the unit's number of markets. This applies to the Development
board only; on the Final path the rate is measured by the organizers' host, so there is no
self-reported magnitude left to bound.

### `message_trace.parquet`

A message-level record of the kernel's inter-agent message ledger. It feeds the
latency/causality checks and the g3.5 protocol-fidelity gate.

**The card decides, not the family — and most units require it.** `requires_message_ledger = true`
is set on **59 of the 72** public units, well beyond the exchange-protocol and reactive-agent
families this page previously named. Read **`[scoring.params].requires_message_ledger`** from the
unit's `card.toml` rather than inferring from the family; "may omit it" applies only to the 13 units
whose card actually says so.

```toml
[scoring.params]
requires_message_ledger = true
```

The key is under `[scoring.params]`, not `[scoring]` — measured, 72 of 72 cards put it there, and
`_CardPolicy` reads `card["scoring"]["params"]`. A lookup on the wrong table returns nothing, which
reads as "not required" and lands you back in the failure this section exists to prevent.

Batch units need one **per sub-scenario**: `score_subs` requires a ledger for every sub
unconditionally, so a batch card reading `false` does not exempt it.

### `batch_events.json` (batch runs only)

Written once per `simulate-batch` run at `/output/batch_events.json`. Aggregates the batch:
`n_scenarios`, `total_events`, **`wall_clock_sec`**, `events_per_sec`, and a `per_scenario`
breakdown.

`wall_clock_sec` is **required**: `check_aggregate` reads `total_events`, `wall_clock_sec` and
`events_per_sec` together, and a missing key fails the entire batch with
`"non-numeric batch_events fields"` — a message that does not name the field it wanted.

### Development resources

The selected Development launcher applies a **4-CPU quota** and **16 GiB memory** per unit,
with swap disabled. Cards request a GPU for permitted local code; using it is optional.
Where the card supplies no timeout, the launcher uses a **1,800-second** container ceiling,
including creation and an image pull when needed. The separate ingestion-stage clock is
**43,200 seconds (12 hours)** across sequential units; scoring has its own stage clock.
Simulation remains offline and receives no House allocation. The planned House timing release
for other tracks changes neither these clocks nor Simulation's compute or network limits.

See the [Development runtime guide](https://github.com/Agenthon-2026/Agenthon2026-public/blob/v2.4.4/docs/DEVELOPMENT-RUNTIME.md)
for process, temporary-space and output limits. The card's `disk = "10G"` is not used by this
launcher and does not establish a writable 10 GiB workspace. The provisional Development
profile and shared queue do not certify official Final timing. Follow the
[image submission guide](https://github.com/Agenthon-2026/Agenthon2026-public/blob/v2.4.4/docs/IMAGE-SUBMISSIONS.md)
for public pulls or an organizer-confirmed private mirror.

### Firewall

Your container runs with `--network none`. It may not make any outbound network requests
during simulation. All data (scenario config, latency CDF files, oracle CSV files) is
provided via the mounted `/input/` directory.

---

## The two baselines

### Baseline 1 — ABIDES Python (local performance reference)

The reference baseline is the ABIDES Python engine pinned to a specific commit,
driven by the `abides_fork` `simulate` adapter. ABIDES itself is not vendored here; the
`baselines/Dockerfile` fetches it at the pinned commit, applies four ordered patches, and
layers the adapter on top. Build the image and measure a local comparison for your optimizations:

```bash
docker build --platform=linux/amd64 -t track3-abides-baseline:latest baselines/
python throughput/timer.py --image track3-abides-baseline:latest \
    --scenario regression_suite/scenarios/as06_throughput_fast.json
```

`--scenario` is required; `as06_throughput_fast.json` is the public throughput example.

See `baselines/README.md` for the pinned commit, the adapter internals, and
`baselines/build_and_validate.sh` (build + validate against the public reference traces).

An admissible submission at or below the recorded reference rate receives the informational
`t3.throughput_nonimproving` label and still receives its score and rank. The comparison uses
the mean rates over the evaluation roster; it is not a same-instance baseline measurement.
See `baselines/README.md` §3. There is no minimum baseline speed to beat for admission.

### Baseline 2 — vectorized reference (internal performance ceiling)

An in-house NumPy-vectorized limit-order-book simulator developed by the QFBench team. It is
**not open-sourced** and is not shipped in this repo; it serves as the admissibility
cross-check reference and the upper performance reference point. The logical interface a fast
submission must satisfy (the `simulate` CLI plus the output schema) is documented in
`baselines/README.md` §2. You may implement any internal architecture — vectorized NumPy,
Numba, Cython, Rust via PyO3, … — as long as that CLI and the output schema are preserved.

---

## How the regression suite works

The harness runs your Docker image on each scenario and checks the output.
The scenarios come from two sources:

- **65 public scenarios** (in `regression_suite/scenarios/`) — you can see the configs and
  the reference traces. Use these for development.
- **Sealed scenarios** (held in the Track-3 private repository, not distributed) — you cannot
  see these configs or traces, and their number is not published. The harness loads them
  automatically in the evaluation environment.

### Running the public scenarios locally

The reference traces ship inside each unit, at `units/<slug>/`. The regression runner looks
them up by `scenario_id`, so build the local cache once before your first run — it is a
build artifact, gitignored, and derived entirely from `units/`:

```bash
python regression_suite/build_reference_cache.py
```

If you cloned without Git LFS content, run `git lfs pull` first; the builder refuses to
create a cache full of 130-byte pointer stubs.

```bash
python regression_suite/run_regression.py \
    --candidate-image <your-image>:latest \
    --scenarios-dir regression_suite/scenarios/ \
    --reference-dir regression_suite/reference_traces/ \
    --output-dir run_outputs/ \
    --workers 4
```

All 65 public scenarios should pass against the ABIDES baseline before you start
optimizing. If they do not, your environment has a problem — check Docker version and
bind-mount permissions.

### Tolerance model (two tiers)

**Tier A** (Families 1, 3, 6, 7, 8 — exact): your trace must have the same row count as the
reference, the same fill events in the same order at the same prices and sizes, exact event
coverage in both directions (no missing events, no extra ones), and Kendall-τ ≥ 0.999 on the event
ordering. The only numeric tolerance is ±1 µs on event timestamps.

> **Tier A compares the emitted trace with the reference. A shared seed alone does not
> guarantee a matching trace.**
>
> Most published single-scenario units use stochastic message latency. The baseline's
> `baselines/abides_fork/config.py` seeds a NumPy global generator, then uses it to seed
> separate random states for the oracle, exchange agent, each scenario agent in construction
> order, latency model, and kernel. Changing those seed draws can change latency and the
> order in which messages reach the exchange, even when both implementations are deterministic.
>
> Preserve the reference's random choices when optimizing the baseline, or otherwise verify
> that your implementation produces a trace satisfying the same Tier-A checks. Those checks
> inspect outputs, not your RNG implementation. Batching, parallelism, different data structures,
> and a different RNG implementation are allowed when the resulting trace passes the published
> event, fill, ordering, and timestamp checks. The timestamp tolerance does not relax the other
> checks.
>
> Tier B uses the statistical comparison described below and in `docs/CATEGORIES.md`;
> it does not require the Tier-A trace match.

**Tier B** (Families 2, 4, 5 — statistical): your mid-price series must be statistically
close to the reference — return-distribution KS ≤ 0.08 (the same calibrated KS check as the
stylized-fact gate) and time-averaged spread within ±10 bps.

**There is no majority rule, in either tier.** Every scenario is graded on its own and every
scenario must pass; a single failure anywhere is inadmissible. Earlier revisions of this page said
80% of each Tier-B family had to pass — no scorer implements that, so do not budget for it.

---

## How the stylized-fact admissibility gate works

Before ranking, the harness checks that your simulator produces realistic market statistics
on the Family 5 (calibration) scenarios. All four checks must pass:

| Check | Ceiling | What it catches |
|---|---|---|
| Return distribution KS distance | ≤ 0.08 | Wrong return shape overall |
| ACF of \|r_t\| error (RMS over lags 1, 5, 10, 20, 50) | ≤ 0.12 | Missing volatility clustering |
| Hill tail exponent absolute error (top-100 order stats) | ≤ 1.5 | Tails too thin or too fat |
| Depth distribution JS divergence (20-bin quote-size histograms) | ≤ 0.10 | Wrong order-book depth structure |

**All four are computed locally too, including the depth divergence.**
`run_regression.py` builds the candidate and reference depth histograms itself (via
`semantics.depth_histogram`) and passes them to the same `stylized_fact_report` the sealed scorer
calls, so `stylized_fact_report.json` carries all four numbers. An earlier revision of this page
said the depth check ran only at sealed scoring; it does not.

> **One KS, one ceiling.** The return-distribution KS above (≤ 0.08) is the *same*
> statistic and *same* calibrated ceiling used by the Tier-B statistical check — the 0.08
> comes from the natural run-to-run variation of correct simulations. There is a single
> return-distribution KS check, applied wherever a distributional comparison is needed.

Get these numbers locally by running the regression suite: `run_regression.py` writes a
stylized-fact report per scenario as it goes.

```bash
cat run_outputs/<scenario_id>/stylized_fact_report.json
# {"ks": 0.0, "acf_abs_l2": 0.0, "hill_abs": 0.0, "depth_js": 0.0}
```

Running the unmodified baseline against its own reference traces gives 0.0 everywhere, as above —
that is the expected reading before you change anything, not an empty result.

`<scenario_id>` is the UUID from the scenario's `scenario.json`, which is also the directory name
under `run_outputs/` and `reference_traces/`.

> `qfbench2_common.scoring.stylized_facts` is a **library module, not a command**. It defines no
> `argparse` parser and no `__main__` block, so
> `python -m qfbench2_common.scoring.stylized_facts --candidate ... --reference ...` exits 0,
> ignores both flags and prints nothing — which reads as "no problems found". Earlier revisions of
> this page published it as a command; it never was one. Import it if you want it directly:
> `from qfbench2_common.scoring.stylized_facts import stylized_fact_report, admissible`.

---

## How throughput is measured

### Provisional Development

The current Development service uses a shared worker queue and the developer scoring profile
(`build_developer_verifier`), with `rankable = False`. Its per-unit throughput comes from your
reported `events_per_sec`, checked for consistency and subject to the existing admissibility
gates. Report real event counts and elapsed time. Development scores and displayed standings
are practice feedback; they do not establish official comparable timing across submissions or
Final ranking. Development does not promise a dedicated, otherwise-idle timing instance.

### Planned official Final timing

Official Final timing requires the organizer's production path and validated timing evidence.
The production scorer's existing requirements below are **not the current Development timing
service**. The repeat producer and validator must be made consistent and verified together
before this path is ready for Final. In particular, the current whole-output-tree comparison
includes changing telemetry such as elapsed time; participants must keep reporting real timing,
not replace it with constants to make repeats match. The correction and worked examples still
need a versioned publication. See the [acknowledged timing issue](https://github.com/Agenthon-2026/track3-simulation-public/issues/5).

In the production scorer, the official number is `events_per_sec`, **measured by the organizer's
runner, never read from your `events.json`**. Its current per-unit requirements are:

1. The candidate image is run several times on the same scenario. How many repeats, and whether
   the first is discarded as warm-up, are committed in advance in the evaluation plan — they are
   not chosen after the fact from what was observed.
2. Discard exactly the warm-up repeats committed in that plan. Do not infer the official
   count from the local timer's defaults.
3. Every remaining repeat must be individually valid *and* must have produced the same output
   bytes and the same event count as the run that was scored. A submission whose repeats disagree
   is refused rather than having its best repeat kept.
4. Each repeat's rate is the runner's own event count (the parquet footer row count) divided by
   the runner's own wall clock. The unit's score is the **median** of those rates.

Your self-reported `events_per_sec` is still checked for internal consistency (g1: within ±5% of
`n_events ÷ wall_clock_sec`; g3: `n_events` equal to the real row count), but it is not the ranked
quantity and no branch of the production scorer reads it. See
`qfbench2_track_simulation/telemetry.py`.

Ranking is by the **arithmetic mean of the per-unit rates over the complete evaluation roster**,
descending (`LEADERBOARD_SORT = "desc"`), not a median over a throughput-only subset. Each
official per-unit rate comes from the repeat measurement described above. A bootstrap
confidence interval is reported alongside the score; resampling is by
scenario **family** rather than by unit, because units within a family share a generator and an
agent mix and so are correlated (see `cluster_key` in `qfbench2_track_simulation/scoring.py`).
A tie in the ranking score is broken in favour of the Final submission uploaded earlier (see
[Competition schedule and submission limits](#competition-schedule-and-submission-limits)); the
bootstrap CI lower bound does not decide it.

### Local timing

Measure your local baseline:

```bash
python throughput/timer.py --image <your-image>:latest \
    --scenario regression_suite/scenarios/as06_throughput_fast.json
```

The local timer varies seeds and defaults to five runs with the first discarded. Those are
developer-tool defaults, not the final evaluation plan. Official repeat counts, warm-up treatment
and measured runtime identity must be published with that plan. Local throughput is a guide for
your own comparisons, not a predictor of your leaderboard rank.

### Secondary diagnostics (local reports)

The primary leaderboard rank stays raw `events_per_sec`. The developer tools in `throughput/`
can produce additional diagnostics, but they do not establish an official leaderboard result:

- **speedup** vs. the CPU-ABIDES baseline, **efficiency** (events/sec per GPU-hour or per
  CPU-core-hour), and **memory efficiency** (events per peak resident byte) — `throughput/diagnostics.py`.
- a **speed–realism Pareto frontier** over (throughput, stylized-fact realism) — `throughput/frontier.py`.
- four **special awards** — Best GPU Acceleration, Best Speed-Realism Frontier, Best
  Latency-Semantics Preservation, Best Systems Diagnosis — `throughput/awards.py`.
- an optional submitted **SimProfile** (`profile.json`: per-component timing, `gpu_utilization`,
  `peak_memory_bytes`), validated against the run's measured wall-clock and memory by
  `throughput/simprofile.py`, feeding Best Systems Diagnosis. It is diagnostic only, never an
  admissibility gate.

Generate the offline report with
`python -m throughput.report --submission <output-dir> --reference <reference-dir> --out report.json`.
These diagnostics are omitted on the official scoring path; do not assume a same-instance
speedup or award measurement is
available merely because a local report can compute one. See `throughput/README.md` §8.

---

## Installing the shared toolkit

All scoring logic lives in the shared toolkit, which ships from its own public repository.
Install it from there:

```bash
# Pin toolkit v2.4.4: the scorer in this repository imports from it at package import.
# The installed package reports version 2.4.4.
pip install "qfbench2-common[data] @ git+https://github.com/Agenthon-2026/Agenthon2026-public.git@v2.4.4#subdirectory=common"
```

> ### Install the pinned tag, not a branch
>
> `Agenthon-2026/Agenthon2026-public` carries the `qfbench2-common` package, and `v2.4.4` is the
> tag CI installs (`QFBENCH2_COMMON_REF` in `.github/workflows/ci.yml`) and the tag whose descriptor
> contract matches what the scorer accepts. **This version is required, not just recommended:**
> the scorer imports `stable_output_binding` from it at `import qfbench2_track_simulation`, so an
> older toolkit fails with `cannot import name 'stable_output_binding'`. Reinstall the toolkit with
> the command above before `pip install .`. The package declares `qfbench2-common>=2.4.4`, so with an
> older toolkit installed `pip install .` stops first with
> `ERROR: No matching distribution found for qfbench2-common<3,>=2.4.4` — that message means
> "reinstall the toolkit from the git tag above", never `pip install qfbench2-common` from PyPI
> (the name is not published there). Do not pin `v2.3.1`: it refuses a descriptor the
> verifier accepts, demanding a non-empty `models` where the current contract allows `"models": []`. **Pin a tag rather than installing from a branch** — an unpinned toolkit is how a local
> result and a scored result come to disagree without either side noticing.
>
> Use **Python 3.13** for the toolkit and scoring tools. The ABIDES baseline has its own
> Python 3.11 environment, as described in `baselines/README.md`.
>
> Please do not vendor or reimplement the toolkit. The scorer imports the same code, and a local
> copy that drifts from it is the one failure mode this package exists to prevent.

Do not copy-paste scoring code into your repo — it will drift from the canonical version.

---

## Quick-start (6 steps)

**Step 1** — Install the toolkit in your Python 3.13 scoring environment and build the
baseline image. Run every step from this repository's root. The Docker build fetches the pinned
ABIDES source and applies all four required patches in order.

```bash
pip install "qfbench2-common[data] @ git+https://github.com/Agenthon-2026/Agenthon2026-public.git@v2.4.4#subdirectory=common"
docker build --platform=linux/amd64 -t track3-abides-baseline:latest baselines/
```

The baseline stays inside its Python 3.11 image; the scorer reads its output files. If you need
a non-Docker ABIDES checkout, use the separate Python 3.11 environment and complete four-patch
instructions in `baselines/README.md`. An unpatched editable install is not the same baseline.

**Step 2** — Build the reference-trace cache. `run_regression.py` resolves references by
`scenario_id` under `regression_suite/reference_traces/`, which is **gitignored and empty in a
fresh clone** — it is a build artifact derived from `units/`. Skipping this makes step 3 look
like a scenario failure. See "Running the public scenarios locally" above for the detail.

```bash
git lfs pull                                    # the builder refuses pointer stubs
python regression_suite/build_reference_cache.py
```

**Step 3** — Run the public regression suite against the unmodified baseline:

```bash
python regression_suite/run_regression.py \
    --candidate-image track3-abides-baseline:latest \
    --scenarios-dir regression_suite/scenarios/ \
    --reference-dir regression_suite/reference_traces/ \
    --output-dir run_outputs/ --workers 4
```

All 65 public scenarios must pass. If they do not, stop and fix your environment.

**Step 4** — Read the baseline stylized facts. Step 3 already computed them; there is no separate
command to run (see "How the stylized-fact admissibility gate works" above for why):

```bash
cat run_outputs/<scenario_id>/stylized_fact_report.json
```

**Step 5** — Measure baseline throughput:

```bash
python throughput/timer.py --image track3-abides-baseline:latest \
    --scenario regression_suite/scenarios/as06_throughput_fast.json
```

Record this number. It is your improvement target.

**Step 6** — Implement your accelerated simulator. Common approaches:
- Replace the Python limit-order-book core with a compiled extension (see
  `baselines/README.md` §2 for the interface contract your `simulate` CLI must satisfy).
- Vectorize agent stepping with NumPy.
- Re-implement the matching engine in Rust or C++ and call it from Python via a C
  extension module.

Whatever approach you take, re-run the regression suite and stylized-fact checker after
each change. Do not let correctness slip.

---

## Directory structure

```
track3-simulation-public/
├── README.md                        ← this file
├── AGENTS.md                        ← agent rules for this repo
├── SUBMISSION_CLI.md                ← the submission CLI contract in full
├── SCENARIO-CATEGORIES.md           ← pointer to docs/CATEGORIES.md
├── AUTHORING-GUIDE.md               ← pointer to docs/AUTHORING-GUIDE.md
├── canary_registry.json             ← contamination canaries for the public units
├── docs/
│   ├── CONCEPTS.md                  ← plain-English glossary of all concepts
│   ├── CATEGORIES.md                ← detailed eight-family reference
│   ├── AUTHORING-GUIDE.md           ← how to design and seal a scenario
│   ├── NVIDIA-STACK.md              ← where the NVIDIA stack does and does not fit
│   └── PROFILING.md                 ← the nsys → SimProfile recipe
├── qfbench2_track_simulation/       ← the public scoring package (importable)
│   ├── scoring.py                   ← the g0–g3 gate wiring and the two scorer factories
│   ├── semantics.py                 ← Tier-A / Tier-B checks + message-ledger gates
│   ├── batch.py                     ← BatchMarketSim aggregate + per-sub isolation gate
│   ├── telemetry.py                 ← the ranked-timing derivation from trusted evidence
│   ├── domain.py, host_metrics.py, limits.py
├── regression_suite/
│   ├── README.md                    ← how the regression suite works
│   ├── run_regression.py            ← the local regression harness
│   ├── build_reference_cache.py     ← builds reference_traces/ from units/
│   ├── scenarios/                   ← index.json + 65 public scenario configs
│   └── reference_traces/            ← BUILT, gitignored: one dir per scenario_id (UUID)
├── throughput/
│   ├── timer.py                     ← local throughput measurement
│   ├── diagnostics.py               ← speedup / efficiency / memory-efficiency
│   ├── frontier.py                  ← speed-realism Pareto frontier
│   ├── awards.py                    ← the four special awards
│   ├── report.py                    ← offline per-unit + aggregate diagnostics report
│   ├── run_unit.py                  ← the local (non-rankable) unit runner
│   └── simprofile.py                ← SimProfile verifier (profile.json)
├── baselines/
│   ├── README.md
│   ├── Dockerfile                   ← builds the baseline image (fetches ABIDES at the pin)
│   ├── build_and_validate.sh        ← docker build + run_regression against the references
│   ├── simulate                     ← the `simulate` CLI shim (image entrypoint)
│   ├── simulate-batch               ← the `simulate-batch` CLI shim (batch entrypoint)
│   ├── patches/                     ← build-time overlays applied to upstream ABIDES
│   ├── abides_fork/                 ← the simulate adapter (config/agents/trace/simulate)
│   └── gpu_starter/                 ← optional CUDA/CuPy starter + its gate-compat check
├── scripts/                         ← organizer-side generators and helper tools
├── tests/                           ← the guards CI runs
├── templates/                       ← card.toml / manifest.json / scenario.json templates
└── units/                           ← 72 public units: 65 single-scenario, 6 batch, 1 exemplar
    ├── t3-EXAMPLE-vectorized-matching/   ← the worked exemplar (README + card + scenario)
    ├── t3-s001-price-time-priority/      ← a single-scenario unit: card, scenario,
    │                                        trace.parquet, message_trace.parquet, events.json
    └── t3-gbatch-homog-4/                ← a batch unit: batch.json, scenarios/,
                                             checks/reference_data/
```

There is no top-level `scoring/` package; an earlier revision of this tree showed one. The public
scoring code is the `qfbench2_track_simulation/` package above, which is what
`.github/workflows/ci.yml` lints, type-checks and tests.

## Competition schedule and submission limits

Development runs through **October 12, 2026**. The joint **Final + Verification phase runs
October 13–25, 2026**. Each team makes **one final submission per track**; organizers perform
verification within that same phase, with no separate participant Verification submission.
If two Final submissions finish this track with the same ranking score, the tie is broken in
favour of the one uploaded earlier.
Registration and Development close together on October 12, 2026 at **23:59 Anywhere on Earth (AoE, UTC−12)**. The joint Final + Verification phase closes on October 25, 2026 at **23:59 AoE**. Other competition dates and task/data cutoffs are unchanged.

At the participant Development opening, Track 3 allows **5 uploads per team per day**
and **20 total uploads per team for this track during Development**. Use your team's single
designated CodaBench account. Local validation and packaging use no attempts; held or cancelled
uploads still count. See [submission limits](SUBMISSION_CLI.md#development-submission-limits).
