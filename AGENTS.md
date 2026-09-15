# AGENTS.md — Rules for AI agents working in Track 3 public

## Executive summary (read this first)

This file adds Track 3 specific rules on top of the project-wide rules in
`../../AGENTS.md`. Read that file first. The three rules that matter most here:
**(1)** the firewall is absolute — no sealed traces, no answer keys, no sealed scenario
parameters may appear anywhere in this `public/` tree; **(2)** the shared scoring toolkit
(`qfbench2-common`) is imported, never copied; **(3)** every document opens with a plain-
English executive summary a finance student can follow.

---

## What is and is not in scope for this repo

**In scope** (safe to create or edit here):
- `docs/` — conceptual explainers, category reference, authoring guide
- `regression_suite/scenarios/` — public scenario configs (65 single-scenario of 72 public units;
  the 66th file in that directory is `index.json`, not a scenario)
- `qfbench2_track_simulation/scoring.py` — the public gate-wiring stub (g0–g3). Track 3 is the
  one track whose scorer is a package, not a `scoring/` directory: Tracks 2 and 4 do have
  `scoring/scoring.py`, and copying their layout here creates a second, unreachable scorer.
- `throughput/timer.py` — local measurement harness
- `baselines/` — pinned upstream ABIDES commit reference (no git submodule), vectorized stubs
- `templates/` — scenario and card templates
- `units/` — public example units (no sealed answers)

**Never touch** (not in scope, not present here):
- Reference traces for the sealed scenarios — those live in the Track-3 private repository
- Sealed scenario configs — those live in `private/sealed_scenarios/`
- The final scorer — that lives in the Track-3 private repository, at `scoring/final_scorer.py`
- Any file named `oracle_*`, `expected*`, `reference/`, `answer_key*`, or `solution/`

---

## Hardware and resource contract (frozen at the 2026-08-10 compute-caps freeze)

The current **provisional Development** service uses a shared worker queue and the developer
scoring profile (`rankable = False`). It provides practice feedback from checked self-reported
throughput; it does not provide official comparable timing or a dedicated timing instance.

The planned **official Final** timing contract requires the **same pinned, otherwise-idle,
single-GPU instance** — pin the *instance*, not just the SKU — with submissions run strictly
sequentially. A dedicated queue with one attached worker is the planned timing isolation.
Runtime identity, repeat/warm-up policy and the paired repeat producer/validator remain Final
release requirements. See [the timing profiles](README.md#how-throughput-is-measured).

Per-unit container caps, identical in every Track-3 `card.toml` `[environment]` block:

| Key | Value |
|---|---|
| `cpus` | `4` |
| `memory` | `"16G"` |
| `disk` | `"10G"` |
| `network` | `"none"` |
| `gpu` | `true` |

The device is **1 × NVIDIA B200** (compute capability **10.0**, 183359 MiB), driver
**580.173.02**, host CUDA toolkit **13.0.3**. Measured on the fleet 2026-08-20 and published in
[`baselines/README.md`](baselines/README.md); the fleet is eight identical hosts.

**This line said H100 until 2026-08-20 and that was wrong** — there is no H100 anywhere in the
fleet. It mattered: `sm_90` cubins do not run on `sm_100`, so anyone who compiled to the
documented target would have shipped an image that fails or silently falls back to PTX JIT inside
their timed window.

**CUDA 12.x images work.** The driver tops out at 13.0 and is backward compatible; `cupy-cuda12x`
was verified JIT-compiling for `sm_100` against a CUDA 12.8 base on this hardware, and the GPU
starter we ship is 12.x. Do not present 13.0.3 as a requirement.

Never write a driver version, CUDA version, compute capability, `sm_` arch, VRAM figure, or price
that is not in `baselines/README.md` — cite that table, do not invent a figure, and do not carry
one forward from an older draft without checking it against the table.

Rules that follow from this contract:

- **Using the GPU is optional.** Track 3 is ranked on raw `events/sec` and nothing else, so a
  well-optimized CPU simulator competes on equal terms. The admissibility gates (Tier-A exact
  fills, Kendall-τ ≥ 0.999, message-ledger causality) punish approximation, and discrete-event
  simulation resists batching, so a GPU port is not a free win. Never describe the GPU as
  mandatory or as the expected route in any document in this repo.
- **`network = "none"` is unchanged.** The CUDA runtime and every dependency must be **vendored
  into the image**; nothing may be fetched at run time.
- **Every Track-3 unit card declares `gpu = true`** — all 72 cards under `units/`, plus
  `templates/card.toml`. Because the flag no longer varies, it no longer discriminates:
  the **efficiency branch** is keyed on `gpu_seconds > 0` and **GPU-award eligibility** on measured
  `gpu_utilization >= GPU_UTILIZATION_FLOOR` — never on the card flag. Never write "GPU units vs CPU
  units". Eligibility moved off `gpu_seconds > 0` because that threshold is cleared by one trivial
  kernel; and the award ranks on `speedup_vs_cpu_abides`, NOT on `efficiency`, because efficiency
  puts GPU time in the denominator and so rewards using the device less. Do not "simplify" the award
  back to ranking on efficiency, and do not rank on utilization either — see `throughput/README.md`
  §6.
- **Never change another track's resource cards as part of a Track 3 update.** Other tracks
  have their own resource contracts. A repo-wide replacement of `gpu = false` with `gpu = true`
  can break those contracts; keep Track 3 resource changes scoped to `track3-simulation-*`.
- **Sealed Track-3 cards come from the private repo's card generator**, not from hand edits
  here. Any change to the `[environment]` block must land in that generator *and* in this repo
  in the same change — public and private must move together, or the two card sets diverge.

---

## Who owns the ranked number (read this before touching anything in `throughput/`)

**The Runner owns official participant launch, lifecycle, timing, C2 and C3.** Track 3 owns the
semantic rules, the telemetry requirements the Runner's evidence must satisfy, and a clearly
non-rankable local developer harness. Two consequences bind every change here:

1. **There are two named factories and only one of them ranks.**
   `qfbench2_track_simulation.scoring.build_verifier` is production: it requires the C1 plan and
   the C2 run record from `ctx`, and it has **no participant-rate fallback of any kind**.
   `build_developer_verifier` is the local practice and provisional Development profile and
   stamps `rankable = False` on everything it emits. Development's displayed practice standings
   do not turn those results into official Final timing. Do not add a flag, an environment
   variable or a "strict mode" that turns one into the other. The previous design had one factory with a fallback, and because the
   production ingestion path never wrote the handoff file, the fallback branch was taken on every
   unit — the leaderboard ranked numbers the submissions chose.
2. **`throughput/` is the developer harness.** It writes `rankable: false`, it is bounded (hard
   deadline, capped logs, C3 no-follow sanitizing retention), and it never re-invokes the
   participant's image. Nothing in the official path reads what it writes.

## Card fields the gates actually read

Three `[scoring.params]` keys are load-bearing. The first two were silently ignored by the
official gate while the private oracle and the practice harness honoured them, so a scenario that
overrode either was graded one way in Dev and another in Final. The third is new:

| Key | What reads it | Why it matters |
|---|---|---|
| `timestamp_tolerance_ns` | `semantics.check_tier_a`, threaded from the card | The official gate used to call `check_tier_a` with no tolerance argument, so it always used the 1,000 ns module default. Any scenario declaring a different tolerance was graded one way in Dev and another in Final |
| `kendall_tau_floor` | `semantics.check_tier_a`, threaded from the card | Documented as a per-scenario override and never read at all |
| `requires_message_ledger` | `scoring._CardPolicy` | The message-ledger gate used to trigger on whether a reference `message_trace.parquet` **happened to exist**, so not shipping one switched the JAX-resistance gate off silently. It is a declaration now. Default: required for every family except `throughput-scale`; an unrecognised family defaults to required |

**Cards are generated.** A value edited in `units/` but not in the generator survives only until
the next regeneration. `requires_message_ledger` must stay mirrored across FOUR templates —
`scripts/build_public_units.py`, `scripts/build_batch_units.py`, and both `_card()` and
`_batch_card()` in the private repo's `scripts/build_final_bundle.py` — plus
`templates/card.toml`. `tests/test_unit_cards.py` fails the build when the committed cards and
the generators disagree.

## Coding conventions specific to Track 3

- **Event types** — use the exact strings defined in `templates/trace_column_registry.json`:
  `ORDER_SUBMITTED`, `ORDER_ACCEPTED`, `ORDER_FILLED`, `PARTIAL_FILL`, `ORDER_CANCELLED`,
  `ORDER_REPLACED`, `QUOTE_UPDATE`. Do not invent new event types.
- **Submission verb** — the Docker container entrypoint must accept `simulate` as the
  command verb (and `simulate-batch` for batch BatchMarketSim units). Do not use `solve`,
  `forecast`, or `analyze` (those are other tracks).
- **Tolerance constants** — the default tolerances (`timestamp_tolerance_ns = 1000`,
  `kendall_tau_floor = 0.999`, `stylized_fact_ceilings.ks = 0.08`, `spread_bps_tolerance = 10.0`)
  default in `qfbench2_track_simulation/scoring.py` and are carried per scenario in the scenario JSON
  `tolerances` block. Override them there only when a specific scenario requires it.

## Scoring library usage

Import scoring functions from the shared library, never reimplement them:

```python
from qfbench2_common.scoring.stylized_facts import stylized_fact_report, admissible
from qfbench2_common.verifier import HierarchicalVerifier, GateResult
```

Install with:

```bash
pip install "qfbench2-common[data] @ git+https://github.com/Agenthon-2026/Agenthon2026-public.git@v2.4.0#subdirectory=common"
```

`v2.4.0` is the tag `QFBENCH2_COMMON_REF` in `.github/workflows/ci.yml` carries and the tag the
scorer runs. Pin the tag rather than installing from a branch — an unpinned toolkit is how a local
result and a scored result come to disagree without either side noticing.

## Quick self-check before marking work done

```bash
# Verify a scenario directory's manifest checksums
# (`qfbench2_common.manifest` is a module, not a package: `python -m ...manifest.verify`
#  raises ModuleNotFoundError. The CLI is the runnable form.)
qfbench2 manifest verify <scenario_dir>

# Run public regression suite
python regression_suite/run_regression.py \
    --candidate-image track3-abides-baseline:latest \
    --scenarios-dir regression_suite/scenarios/ \
    --reference-dir regression_suite/reference_traces/ \
    --output-dir /tmp/smoke_out/ --workers 1

# Check the firewall. Takes ONE unit directory; this repo has no `public/`.
# `|| break` would be wrong here: it stops at the first bad unit AND the loop exits 0,
# which is the same "guard that cannot fail" this line was fixed to remove.
fail=0; for u in units/*/; do qfbench2 manifest assert-public-safe "$u" || fail=1; done; exit $fail
```

If any of these fail, do not mark the work as done.
