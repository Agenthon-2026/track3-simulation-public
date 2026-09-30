# Profiling with Nsight → SimProfile (Best Systems Diagnosis)

How to turn an Nsight Systems (`nsys`) profile of your simulator into the `profile.json` sidecar that
feeds the **Best Systems Diagnosis** special award. The profile is diagnostic-only — it is never an
admissibility gate, and a submission is never rejected for an absent or poor profile.

**Golden rule:** profile on **your own machine**, and submit only the resulting `profile.json`. The
profiler's overhead must never touch the ranked run — the harness times `events/sec` externally, and
GPU time / peak memory for the award are measured host-side by the harness, not read from your
profile. (Running `ncu` on the shared eval box is a perf-counter/isolation decision for the
organizers — default is no; profile locally.)

> ### CPU-time accounting under the earlier gVisor setup (history)
>
> Development and Final runs use the `runc` container runtime, which reports CPU time to the
> process normally. An earlier setup ran units under gVisor, and **gVisor does not report CPU time
> accurately to the process inside it**. NVIDIA measured `os.times()` reporting 0.55 cores against a
> `--cpus=2` cap, on a workload that completed 91% of the same work `runc` did (2026-08-25). A
> CPU-time profile taken inside a unit under that setup — `os.times()`, `resource.getrusage()`,
> `/proc/stat`, `time.process_time()`, and any library built on them — is wrong by roughly that
> factor, so do not compare one with a current profile.
>
> **Track 3's ranked metric is unaffected**: `events/sec` is wall-clock based and measured
> host-side by the harness, not read from anything your process reports. Wall-clock inside the
> container (`time.perf_counter`) is also fine.
>
> Under that setup, a CPU-time-based profile taken inside a unit told you your simulator was idle
> when it was saturated. Wall-clock, or profiling locally, avoids the question entirely — which is
> what this document asks you to do anyway.

## 1. Annotate your hot path with NVTX ranges

Name the ranges to match the SimProfile components: `matching`, `event_queue`, `latency`,
`agent_logic`, `io`. More components (≥ 4) score higher on granularity.

```python
import nvtx  # pip install nvtx  (host-side dev dependency; not in your eval image)

with nvtx.annotate("matching", color="green"):
    match_orders(book)
with nvtx.annotate("event_queue", color="blue"):
    step_event_queue(kernel)
# ... latency, agent_logic, io ...
```

(CuPy users can equivalently use `cupy.cuda.nvtx.RangePush("matching")` / `RangePop()`.)

## 2. Profile a run (on your machine)

```bash
nsys profile -o run --trace=cuda,nvtx --force-overwrite=true \
    python -m abides_fork.simulate --config scenario.json --out /tmp/trace.parquet
```

NVTX works even with no GPU, so a CPU-only submission can still produce a profile (its
`gpu_utilization` is just 0).

## 3. Export the NVTX summary as JSON

```bash
nsys stats --report nvtx_sum --format json run.nsys-rep > nvtx.json
```

## 4. Convert to a SimProfile

```bash
python scripts/nsys_to_profile.py --nsys-json nvtx.json \
    --gpu-util 0.72 --peak-memory 2147483648 \
    --out profile.json --wall-clock 3.4      # --wall-clock is an optional self-check
```

`--gpu-util` is the mean GPU utilization over the run (0–1); `--peak-memory` is peak resident bytes.
The optional `--wall-clock` runs the profile through `throughput.simprofile.verify_profile` and
prints the verdict so you can check it before submitting.

## 5. Submit it

Drop `profile.json` next to your `trace.parquet` in `/output`. That's it.

## What `verify_profile` checks (so your profile scores well)

- Component times are **non-negative** and **sum to the run wall-clock within ±10%** — so annotate
  the *whole* hot path, not just a few kernels.
- `gpu_utilization` is in `[0, 1]`; `peak_memory_bytes` is not wildly under-reported vs the measured
  peak.
- Quality = granularity (saturating at ~4 components) × consistency with the wall-clock; any breach
  zeroes it.

The award ranks on that quality, but only against **host-measured** ground truth — a self-consistent
fabricated profile does not win (see `throughput/README.md` §7). Profile honestly; the point is to
show where your simulator actually spends its time.
