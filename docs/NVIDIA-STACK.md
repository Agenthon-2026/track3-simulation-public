# The NVIDIA stack in Track 3 (Simulation)

How the NVIDIA technology stack maps to Track 3, and how participants should (and should not) use
it. Track 3 asks you to make a discrete-event market simulator **faster on a fixed hardware SKU
while preserving exact ABIDES semantics** — the gates reject any acceleration that changes the
observable event stream. The NVIDIA tools are therefore *acceleration options and a diagnosis
skill*, not a solution recipe.

## Framing: encouraged, not mandated

Track 3 is already the **leveled** track — every submission runs on the same fixed SKU
(`baselines/README.md` §5) with **no runtime LLM** (`network=none`). The "same tools so a bigger
model can't win" argument that levels the LLM tracks does not apply here; there is no model in the
loop. Mandating GPU usage would in fact *un*-level T3, because a well-engineered CPU submission is a
first-class result. So the stack is **encouraged and documented, never required**, and no award is
gated on using it. What is rewarded is the outcome — throughput among admissible submissions
(ranked) and the profiling/diagnosis skill (special awards).

## Per-tool fit

| Tool | Fit for T3 | How to use it | Caveat |
|---|---|---|---|
| **Nsight** (nsys) | **Strong** — this *is* the skill the awards reward | Profile your `simulate` run, name NVTX ranges to match SimProfile components, submit the profile → **Best Systems Diagnosis** | Profiler overhead must never touch the ranked run; profile on your own machine. `ncu` on the shared box is a perf-counter/isolation decision (default: no) |
| **CUDA** | **Partial — one route** | Port hot kernels (matching, event processing) to GPU; vendor the CUDA runtime in your image (`network=none`) | DES is branch-heavy and largely sequential; a naive batched port breaks the exact-fill gates. Real speedups come from *semantics-preserving* parallelism (e.g. across independent scenarios), not from approximating the event order |
| **RAPIDS / cuDF** | **Dev-side only** | Trace/ledger forensics on gate rejections; organizer-side gate acceleration | **Not** the ranked hot loop — vectorizing the order book over a cuDF frame reorders events and fails Tier-A exactness. cuDF does not belong in the eval image |
| **cuOpt** | **No fit** | — | There is no discrete-optimization surface anywhere in Track 3 — public or sealed (deterministic price-time matching, STP policies, DES loops). **Recommend dropping cuOpt from the T3 row** of the sponsor mapping |

## What this means concretely

- **To be admissible:** reproduce the reference exactly (Tier-A) or within statistical tolerance
  (Tier-B). The provided CPU ABIDES already does this; your job is to go faster without breaking it.
- **To rank well:** raise `events/sec`. GPU (CUDA) is one route; so are constant-factor CPU
  optimizations and parallelizing across the independent scenarios of the batch family.
- **To win a special award:** GPU efficiency (Best GPU Acceleration), the speed–realism frontier,
  latency-semantics preservation, or systems diagnosis (Nsight → SimProfile). The two
  telemetry-dependent awards use **host-measured** GPU time / peak memory, not your self-report —
  see `throughput/README.md`.

## Where the tooling lives

- Fixed-SKU box + pinned CUDA toolkit (the compile target): `baselines/README.md` §5.
- Awards, diagnostics, and the host-telemetry integrity rules: `throughput/README.md`.
- The nsys → SimProfile recipe (`docs/PROFILING.md`) and the optional CUDA/CuPy starter
  (`baselines/gpu_starter/`) are both in this repository now. Each is one route, not the expected
  one, and the starter passes the gates itself.
- What the gVisor sandbox of an earlier setup cost, by workload shape (history; Development and
  Final runs use `runc`): `baselines/README.md` §3.
