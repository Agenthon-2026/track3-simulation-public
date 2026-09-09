# Track 3 — Simulation Engine Baselines

## Executive summary (read this first)

Use the ABIDES baseline to check your simulator and compare local performance. Correctness
determines admission; an admissible simulator remains ranked even below the recorded baseline
rate. `throughput/timer.py` is a local developer tool, not the official timing protocol.

> **No throughput figure on this page was measured on the evaluation fleet, and the ~65,000
> events/sec ABIDES baseline is withdrawn as a target.** The 2026-06-23 figures were written when
> this repository was created and the benchmark hardware was still described as "4× AMD EPYC vCPU"
> with an unnamed GPU; they predate both the B200 hosts and the gVisor sandbox every ranked run now
> executes under. The hardware in §3 was measured on 2026-08-20 — the throughput numbers were not.
> What replaces 65,000 is the one figure this repository can reproduce from its own files: the 65
> shipped reference runs, **geometric mean 13,793 events/sec** (range 3,471–18,046), on hardware
> that is not recorded. See §1 for the derivation and §3 for the informational baseline
> comparison — neither number is an admission threshold. A fleet baseline is coming; it is tracked in
> a tracking issue the organizers will open with the measurement.

---

## 1. Baseline 1: ABIDES Python Stack with Track 3 Overlays

**Source:** https://github.com/jpmorganchase/abides-jpmc-public
**License:** BSD-3-Clause (see full text at the link above)
**Pinned commit:** `f9cbe51342b7dedd9587e4e069040d68a5c6477f`

This is the upstream source pin, recorded in `abides_fork_commit`. The runnable baseline
also depends on this Track 3 revision, the four ordered patches and adapter below, and its
Python/system dependencies. The upstream SHA alone is not an immutable image identity.
ABIDES is fetched at build time rather than vendored as a git submodule; there is no
`baselines/abides-python/` checkout in this repository.

### What it is

ABIDES (Agent-Based Interactive Discrete Event Simulation) is the open-source
multi-agent financial market simulator released by J.P. Morgan. It drives a
limit-order-book (LOB) via a discrete-event queue with heterogeneous agent archetypes
(market makers, momentum traders, noise traders, etc.). The Python implementation is
single-threaded and event-driven; each event invocation calls back into pure-Python
agent logic, which makes it straightforward to extend but imposes significant
per-event overhead.

### Throughput

| Quantity | events/sec | Provenance |
|---|---|---|
| Geometric mean over the 65 public reference runs | **13,793** (range 3,471–18,046, median 14,302) | **Measured**, and reproducible from this repository — see below. Hardware not recorded. |
| 4 vCPU (x86-64), 16 GiB RAM, GPU unused (CPU-only baseline) | ~50,000–80,000 | **Not measured on this fleet.** Written 2026-06-23, hardware since replaced. |

The wide range in the second row (50 k–80 k) was attributed to scenario complexity: scenarios with
many active agents and complex order-book states run slower than sparse, low-agent-count scenarios.
That effect is real and visible in the measured row too — the 5.2× spread from 3,471 to 18,046
across the 65 reference runs is the same phenomenon.

**A row reading "geometric mean over public regression scenarios: ~65,000 events/sec" used to sit
in this table. It is withdrawn.** It named exactly the quantity the first row now reports, and the
repository's own shipped data puts that quantity 4.7× lower. No run producing the 65,000 figure is
recorded anywhere in this repository; it was written on 2026-06-23, against hardware this
repository no longer runs on, and it is the exact arithmetic midpoint of the 50 k–80 k row above.
**Do not treat 65,000 as a target, a floor, or a number to beat.** The constant
`ABIDES_BASELINE_EVENTS_PER_SEC` in
`qfbench2_track_simulation/domain.py` still carries the value: it is retained there as a pinned
historical input to the clip-ceiling derivation, is read by no scoring path, and is documented as
withdrawn.

**How the measured row is derived.** Each of the 65 public units carries an `events.json` written
by `baselines/abides_fork/simulate.py` from the pinned ABIDES fork — the same baseline this section
describes. Reproduce it from a clone:

```bash
python - <<'PY'
import glob, json, math, statistics
r = [json.load(open(p))["events_per_sec"] for p in sorted(glob.glob("units/*/events.json"))]
print(len(r), min(r), max(r), math.exp(sum(map(math.log, r)) / len(r)), statistics.median(r))
PY
# 65 3471.4630392468134 18046.378079211532 13792.95705430894 14302.424699152642
```

**What the measured row is not.** The hardware those runs used is not recorded, and their
`wall_clock_sec` covers the simulation loop rather than the whole container, so 13,793 is not a
fleet measurement either — it is the only throughput number in this repository you can reproduce
from what ships in it. It is also not explained away by the sandbox: per §3 a Python event loop is
nearly free under gVisor (allocation 0.3%, heap −0.9%, i.e. noise).

**No fleet-measured baseline exists yet.** `timer.py --runs 5 --discard-warmup` has been named here
as the protocol since 2026-06-23, but no run of it on the B200 hosts, under the gVisor sandbox every
ranked run executes in, is recorded. Until one is — tracked in
a tracking issue the organizers will open with the measurement — the honest statement is
that the fleet baseline is unknown and a measured one is coming. Measure your own machine
(`throughput/timer.py`) and improve on that; ranking is relative to other submissions, not to any
number on this page.

### The `simulate` adapter

Stock ABIDES has no `simulate` CLI that reads a Track 3 `scenario.json` and emits the
canonical `trace.parquet`. That verb is provided by the **`abides_fork` adapter** in
this directory (`baselines/abides_fork/`), layered on the unmodified pinned engine:

- `config.py` — maps a `scenario.json` (`exchange_config` / `oracle_config` /
  `latency_config` / `agent_configs`) onto an ABIDES config (mirrors `rmsc04`).
- `agents.py` — lightweight scheduled traders (`NoiseTrader`, `MarketMaker`,
  `ValueTrader`, `MomentumTrader`) that produce the order flow each scenario describes.
- `trace.py` — maps ABIDES event logs onto the canonical 7-column trace schema.
- `simulate.py` — the CLI: runs the scenario, writes `trace.parquet` + `events.json`.

### Build the baseline image (recommended)

The `Dockerfile` in this directory builds a local image. It fetches the pinned ABIDES commit
and applies these patches in order before installing the engine and adapter:

1. `order_size_model.pomegranate-free.patch` — replace the pomegranate dependency.
2. `kernel_message_ledger.patch` — expose the required message ledger.
3. `exchange_protocol_stp.patch` — support configured self-trade prevention.
4. `oracle_scheduled_jump.patch` — support configured oracle jumps.

From the repository root, prepare the public reference cache in your Python 3.13 scoring
environment before building and validating the image:

```bash
git lfs pull                              # fetch public trace data, not LFS pointer files
python regression_suite/build_reference_cache.py
./baselines/build_and_validate.sh           # docker build + regression_suite/run_regression.py
```

These are local regression checks. They do not establish the final runtime identity or an
official throughput score. If a check fails, preserve the supplied reference traces and
diagnose the build, patches, random streams and outputs; do not replace the references to
make a candidate pass.

### Installing and running locally (without Docker)

Run from the repository root. **The ABIDES baseline is the one deliberate exception to the org's
Python 3.13 standard**: ABIDES requires pandas 1.x, and that pinned stack (`numpy==1.26.4`,
`pandas==1.5.3`) publishes no cp313 wheels, so the baseline stays on 3.11 — it is the exact stack
that generated the frozen reference traces. It is self-contained: the harness only *reads* the
parquet it writes, so it does not constrain the 3.13 evaluation container or your own submission
image.

```bash
# Clean Python 3.11 environment with the pinned stack (baseline exception — see above;
# coloredlogs is a runtime import of abides_core.abides and is not declared in ABIDES's setup.cfg)
python3.11 -m venv .venv-abides
source .venv-abides/bin/activate
python -m pip install numpy==1.26.4 pandas==1.5.3 scipy==1.17.1 pyarrow==15.0.2 coloredlogs==15.0.1

# Fetch the pinned source and apply all four overlays in Dockerfile order
git clone https://github.com/jpmorganchase/abides-jpmc-public
git -C abides-jpmc-public checkout f9cbe51342b7dedd9587e4e069040d68a5c6477f
git -C abides-jpmc-public apply "$PWD/baselines/patches/order_size_model.pomegranate-free.patch"
git -C abides-jpmc-public apply "$PWD/baselines/patches/kernel_message_ledger.patch"
git -C abides-jpmc-public apply "$PWD/baselines/patches/exchange_protocol_stp.patch"
git -C abides-jpmc-public apply "$PWD/baselines/patches/oracle_scheduled_jump.patch"
python -m pip install --no-deps abides-jpmc-public/abides-core abides-jpmc-public/abides-markets

# Run a public regression scenario through the adapter
PYTHONPATH=baselines python -m abides_fork.simulate \
    --config regression_suite/scenarios/s001_price_time_priority.json \
    --out /tmp/baseline_trace.parquet
```

This writes `trace.parquet` and an `events.json` sidecar next to `--out`. Always check
out the exact pinned commit and apply all four overlays. Deactivate this environment before
returning to the Python 3.13 scoring tools. The Dockerfile remains the reference build recipe;
the floating base image and apt layer mean a complete immutable runtime manifest is still needed.

### Interface contract

**All Track 3 submissions must honour this CLI interface exactly.** The evaluation
harness calls your Docker image with:

```
simulate       --config /input/scenario.json --out /output/trace.parquet
simulate-batch --batch-dir /input/scenarios   --out-dir /output
```

- `/input/scenario.json` — read-only bind-mount of the scenario configuration.
  Schema: `common/schemas/sim_scenario.schema.json`.
- `/output/trace.parquet` — the full event trace, one row per market event.
  Must conform to the column spec in `common/schemas/sim_scenario.schema.json §outputs`.
- `/output/message_trace.parquet` — message-level kernel ledger. Required by the
  exchange-protocol, reactive-agent, and batch units; feeds the latency/causality and
  g3.5 message-ledger checks.
- `/output/events.json` — aggregate summary written alongside the trace. The `events`
  schema in `common/schemas/sim_scenario.schema.json` requires **all six** keys:
  `scenario_id` (str), `n_events` (int), `wall_clock_sec` (number), `events_per_sec`
  (number), `seed` (int), `trace_sha256` (64-hex str). (The g1 schema gate rejects an `events.json`
  missing any of the six.) **`n_events` must equal the row count of `trace.parquet`** (for
  `simulate-batch`, `total_events` must equal the sum of the per-sub trace row counts). The harness
  counts events itself from the emitted trace and uses that count as the numerator of the ranked
  `events/sec`; the declared value is cross-checked against it and a mismatch fails the run, so an
  over-declared `n_events` cannot raise a submission's throughput.
  It now also carries two telemetry keys — `peak_memory_bytes` (int) and `gpu_seconds`
  (number) — consumed by the secondary diagnostics (see §3).

The `simulate-batch` verb (BatchMarketSim) runs the sub-scenarios under `/input/scenarios`
in one pass, writing `/output/<sub>/{trace,message_trace}.parquet` + `events.json` per
sub, plus an aggregate `/output/batch_events.json`
(`n_scenarios` / `total_events` / `events_per_sec` / `per_scenario`). Batch units
(`t3-gbatch-*`) rank on the aggregate throughput reported in `batch_events.json`.

Submissions that exit non-zero, fail to write `trace.parquet`, or write an
`events.json` missing any required key will be marked `shared.schema.invalid_output`
and `t3.parse_error`, and will not receive a throughput score.

---

## 2. Baseline 2: Vectorized Reference Simulator (Interface Stubs)

This is an in-house NumPy-vectorized LOB simulator developed by the QFBench team.
It is **not open-sourced** and is not available to participants. It serves two purposes:

1. **Admissibility pass reference:** the harness cross-checks that candidate output
   traces are semantically equivalent to this simulator's output on the regression
   scenarios before awarding a throughput score.
2. **Internal performance reference point:** it is how the organizers sanity-check what the
   scenarios cost a well-optimized implementation. It does **not** enter the score — there is no
   normalization step for it to be the ceiling of (see the note under the table below).

### Throughput

| Hardware | Observed throughput | Measured on this fleet? |
|---|---|---|
| 4 vCPU (x86-64), 16 GiB RAM, GPU unused (CPU-only baseline) | ~400,000 events/sec | **No** — 2026-06-23, hardware since replaced |

Achieving or exceeding this figure is not required and earns nothing. **There is no normalized
throughput score.** Ranking uses the arithmetic mean of per-unit `events_per_sec` over the
complete evaluation roster, descending (`LEADERBOARD_SORT = "desc"`). Each official unit rate
is the median of its measured repeat rates. No normalization maps that aggregate onto a 0–1
mark, and the vectorized reference figure is not the top of the scoring scale.
An earlier revision of this page said submissions in this range "receive maximum normalized
throughput marks" and called this simulator the "upper reference point in the scoring
normalization" — there is no such normalization in the scorer. Treat the number as orientation
only, and note that it has never been reproduced on the evaluation hardware.

### Public interface stub (Python 3.13)

Participants may implement any internal architecture — vectorized NumPy, Numba JIT,
compiled Cython extensions, Rust via PyO3, a compiled C extension, etc. — as long as
the Docker CLI interface and the output schema described in §1 are preserved. The
following Python stub documents the logical interface that the reference simulator
satisfies:

```python
from pathlib import Path
import pandas as pd

class VectorizedLOBSimulator:
    """
    Vectorized limit-order-book simulator interface.

    Parameters
    ----------
    scenario_config : dict
        Parsed contents of the scenario JSON (matches sim_scenario.schema.json).
    seed : int
        Random seed for reproducible agent behaviour. Must be a non-negative
        integer < 2**31 (enforced by the seed-derivation protocol in timer.py).
    """

    def __init__(self, scenario_config: dict, seed: int) -> None:
        """Initialise simulator state from config and seed. Must not perform I/O."""
        ...

    def run(self) -> tuple[pd.DataFrame, dict]:
        """
        Execute the full simulation and return results.

        Returns
        -------
        trace_df : pd.DataFrame
            Full event trace. Columns must match the spec in
            common/schemas/sim_scenario.schema.json §outputs.columns.
        events_dict : dict
            Aggregate summary. Must contain at minimum:
              - "n_events": int       — used by timer.py for events/sec
              - "scenario_id": str    — echoed from config for traceability
              - "seed": int           — the seed used
        """
        ...
        # Returns (trace_df, events_dict)
```

The `simulate` CLI entry point in compliant submissions is expected to:

1. Parse `/input/scenario.json` into a `dict`.
2. Extract the `seed` field (or fall back to `scenario_config["base_seed"]`).
3. Instantiate the simulator, call `.run()`, and serialise outputs.

---

## 3. Performance Reference Points

Ranking uses the **arithmetic mean of per-unit rates over the complete evaluation roster**,
descending. The `t3.throughput_nonimproving` label is informational: an admissible submission
at or below the recorded reference rate keeps its score and rank. This is the existing
[organizer clarification](https://github.com/Agenthon-2026/track3-simulation-public/issues/1#issuecomment-5534948011),
not a new scoring rule. No number in the table below is a minimum admission speed. The
vectorized reference is a performance reference point only.

| Configuration | events/sec | Provenance | Role |
|---|---|---|---|
| **Unmodified ABIDES baseline** | 13,793 geomean (range 3,471–18,046, median 14,302) | **Measured**, from the `events_per_sec` in the 65 shipped public `units/*/events.json`, all written by the pinned baseline. Hardware not recorded, and `wall_clock_sec` there covers the simulation loop rather than the whole container. | Not a threshold — the only reproducible baseline number in this repository |
| **Unmodified ABIDES baseline** | ~65,000 — **withdrawn, see below** | **No measurement recorded.** Written 2026-06-23 against the hardware this repository described at the time. | **None.** Not a target, not a floor, not read by any scoring path |
| **Vectorized reference** | ~400,000 | **Not measured on this fleet**, same 2026-06-23 provenance. | Internal reference (not a gate) |

**65,000 is withdrawn as a target.** This table previously gave it the role "ranking floor
(`t3.throughput_nonimproving` at or below)", which made it the figure to beat. It was never run on
the B200 workers or under gVisor, and this repository's own shipped reference runs sit
about 4.7× below it on the same engine — a gap the sandbox does not explain (per §3 a Python event
loop is nearly free under gVisor: allocation 0.3%, heap −0.9%, i.e. noise). Withdrawing it does not
change any score: the value survives only as `ABIDES_BASELINE_EVENTS_PER_SEC` in
`qfbench2_track_simulation/domain.py`, a pinned historical input to the clip-ceiling derivation that
no scoring path reads.

**There is no fleet-measured baseline yet, and 13,793 is not one.** 13,793 is reproducible from the
files in this repository (§1 shows the command) on hardware that is not recorded. A `timer.py` run
on the evaluation fleet is tracked in
a tracking issue the organizers will open with the measurement. Until it lands, treat
every events/sec figure on this page as scale, not as a measurement of the machine your submission
will be scored on — and tune against your own measured baseline, since ranking is relative to other
submissions.

A row reading "typical competitive submission: 150,000–600,000 events/sec" used to sit in this
table. It was removed rather than re-qualified: no such range was ever measured, there were no
submissions to measure it from, and a fabricated band is worse than no band — it invites tuning
towards a number that means nothing.

**What the informational comparison uses.** The `t3.throughput_nonimproving` label compares
the mean per-unit rate with the mean rate recorded in the reference `events.json` files over
the evaluation roster. It is not a median over throughput-only units. The reference rates
were frozen when those traces were generated; their hardware is not recorded. This comparison
does not re-run ABIDES on the evaluation instance and is not a measured same-instance speedup.

That is also why this label is informational rather than disqualifying: it compares a
host-measured rate against a rate frozen on unrecorded hardware, so it says "your run was not
faster than the recorded baseline run", not "your simulator is slow". You still receive a rank.

**Official benchmark hardware:**

Measured on the fleet 2026-08-20, not quoted from a spec page. Every host is identical, and
this is your compile target for the whole competition.

**What your container gets** (the caps on every Track 3 unit card):

- CPU: **4 vCPU**, `cpus = 4`
- RAM: **16 GiB**, `memory = "16G"`
- Disk: 10 GiB
- GPU: one **NVIDIA B200**, attached to every timed run
- Network: disabled at runtime (`network = "none"`) — the CUDA runtime and every other
  dependency must be vendored into your image

**The host underneath** (larger than your caps; listed so you know the microarchitecture you are
compiling for, not as a resource budget):

| | |
|---|---|
| GPU | NVIDIA **B200**, compute capability **10.0**, 183359 MiB, ECC and persistence mode on |
| Driver | **580.173.02** |
| CUDA toolkit on host | **13.0.3** (driver supports up to 13.0) |
| CPU | **Intel Xeon Platinum 8570** |
| OS | Ubuntu 24.04.4 LTS, kernel 6.11.0-1016-nvidia |
| Container stack | Docker 29.7.2, gVisor `release-20260803.0`, nvidia-container-toolkit 1.19.1-1 |

Inside a task container you will see kernel `4.19.0-gvisor`, the B200 with all 183359 MiB, and
driver 580.173.02.

### What the sandbox costs, by workload shape

Your run is sandboxed under gVisor. That is not a flat tax, and knowing where it falls is the
difference between optimising the right thing and the wrong thing for a week.

Measured by NVIDIA on 2026-08-25 under a real Track 3 card's caps (`--cpus=4 --memory=16G`), five
repeats on two hosts, wall-clock throughout, stable to within a point across hosts:

| workload shape | `runsc-gpu` | `runc` | sandbox cost |
|---|---|---|---|
| CPU-bound arithmetic | 46,644,253 | 50,084,398 | **6.9%** |
| allocation churn | 11,951,054 | 11,989,250 | **0.3%** |
| heap operations | 7,786,441 | 7,716,932 | **−0.9%** (noise) |
| raw syscalls | 718,093 | 4,755,762 | **84.9%** |
| loopback socket IPC | 281,129 | 1,105,189 | **74.6%** |

**GPU work has no measurable steady-state penalty** — 0.0218 s against 0.0217 s on a repeated
matmul. The only GPU cost is context creation on the first CUDA call, which varied between +65 ms
and +365 ms across hosts. That lands inside your timed window, so pay it once and reuse the
context rather than creating one per scenario.

**The shape of this is good news for a discrete-event simulator.** Object churn and heap operations
— the bulk of a Python event loop — are free. What is expensive is precisely what gVisor
intercepts: raw syscalls and loopback IPC.

So the guidance is specific rather than vague. **The sandbox does not tax your event loop, your
allocations, or your GPU work. It taxes syscalls and IPC.** A design that batches writes and avoids
a syscall per event pays almost nothing for running sandboxed. One that treats syscalls as free —
per-event logging, a socket between worker processes, an `fsync` in the hot path — loses most of
its speedup to the sandbox rather than to its own algorithm, and will read as an algorithmic
disappointment when it is not one.

An earlier figure of "~9% overhead" circulated between us and NVIDIA. It came from a spin loop,
which measures only the first row of that table; treat it as superseded.

> ### CUDA 12.x images work. Do not rebuild for 13.x on our account.
>
> The driver tops out at CUDA 13.0 and is **backward compatible**, so a 12.x image runs fine.
> Verified on this hardware: `cupy-cuda12x` JIT-compiles for `sm_100` against a CUDA 12.8 base.
> The GPU starter image we ship is itself 12.x. The 13.0.3 figure above is what the host happens
> to carry — it is **not** a requirement, and reading it as one would send you rebuilding for
> nothing.

Compile for `sm_100`. A PTX-only build will JIT on first launch, which lands inside your timed
window; ship cubins for `sm_100` if that matters to you.

A GPU is available to every submission; using it is **optional**. Track 3 is ranked on raw
events/sec and nothing else, so a well-optimized CPU simulator competes on equal terms — the
device is an opportunity, not a requirement. Note that the admissibility gates (Tier-A exact
fills, Kendall-τ ≥ 0.999, message-ledger causality) punish approximation, and discrete-event
simulation resists batching, so a GPU port is not a free win.

Track 3's timing contract requires the same pinned, otherwise-idle instance, with submissions
run sequentially; matching the GPU SKU alone is insufficient. The final measured runtime record
and repeat/warm-up commitment remain release deliverables. The developer tools can report
**secondary diagnostics** — speedup against a chosen CPU-ABIDES reference, efficiency
(events/sec per GPU- or CPU-core-hour), and memory
efficiency (events per peak resident byte) — derived from the `events.json`
`peak_memory_bytes` / `gpu_seconds` telemetry. These are local reports, not official rankings;
the official path omits these secondary diagnostics. See `../throughput/README.md` §8.

### Throughput classification labels

These are the canonical `FailureLabel` values (see
`common/qfbench2_common/failure_labels.py`):

| Condition | Label | Ranked? |
|---|---|---|
| Mean events/sec above the recorded reference mean (see §3) | (no label; normal) | Yes, if admissible |
| Mean events/sec at or below that recorded reference mean | `t3.throughput_nonimproving` | Yes, if admissible |
| Output dir/trace/`events.json` missing or malformed | `t3.parse_error` (+ `shared.schema.invalid_output`) | No — inadmissible |
| Semantic equality check fails (Tier A fill sequence or Tier B proximity) | `t3.semantic_regression_fail` | No — inadmissible |
| Stylized-fact ceiling breached (Family 5) | `t3.stylized_fact_breach` | No — inadmissible |
| Sealed reference trace fails its SHA-256 checksum | `t3.reference_integrity_error` | Evaluation halted (operator-side) |

`t3.throughput_nonimproving` changes neither admission nor the ranked score. An otherwise
admissible submission remains on the throughput leaderboard. There is no separate scoring
dimension that compensates for failing a required correctness check.

---

## 4. Dependency Constraints

Your participant image and the organizer's scoring image are separate environments. Include
every runtime dependency in your image, including Python and standard scientific libraries if
your simulator uses them. A package installed in the scorer is not available inside your image.
The toolkit uses Python 3.13; the reference ABIDES image deliberately uses Python 3.11. Neither
requires your simulator to share the scorer's Python environment.

Packaging notes for common implementation choices:

| Package | Notes |
|---|---|
| Numba | Include full conda/pip install in your Docker image; LLVM is large — plan image size accordingly |
| CuPy | Bundle a runtime supporting B200 `sm_100`; compatible CUDA 12.x images work (see §3) |
| JAX | Bundle the CPU or GPU dependencies your image needs; GPU builds must support the published device and driver |
| Cython / compiled extensions | Must be compiled for `linux/amd64`; build in your Dockerfile |
| Rust extensions (via PyO3/maturin) | Must be compiled for `linux/amd64`; multi-stage builds recommended |
| Any other package | Vendor it; the network is disabled at runtime |

**Network is disabled at runtime** (`docker run --network=none`). Any attempt to
make outbound connections will fail silently. Submissions must not depend on remote
model weights, API calls, or package downloads at inference/simulation time.

**Image size guidance:** keep your image lean with multi-stage builds and only the dependencies
you need. This guide does not establish an image-size limit or a platform acceptance guarantee;
the final transfer and resource instructions must specify those conditions.
