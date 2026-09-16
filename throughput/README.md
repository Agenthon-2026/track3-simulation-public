# Track 3 — Throughput Metrics Layer (LOCAL DEVELOPER HARNESS — it cannot rank)

> ## Nothing in this directory produces an official number
>
> **The Runner owns official participant launch, lifecycle, timing, C2 and C3.** Track 3 owns the
> semantic rules, the telemetry requirements the Runner's evidence must satisfy, and this clearly
> non-rankable developer harness. Every record `run_unit.py` writes carries
> `rankable: false` and `profile: "developer"`, and the production scorer factory
> (`qfbench2_track_simulation.scoring.build_verifier`) has **no path that reads it**.
>
> The ranked events/sec comes from the trusted C2 run record: host-measured wall clock,
> Runner-measured parquet-footer row counts (frozen ruling R-3), telemetry at the frozen C7
> thresholds (50 ms sampling, coverage >= 0.95, GPU resolved by UUID and attributed to the
> participant cgroup), and every repeat validated. See
> `qfbench2_track_simulation/telemetry.py`.
>
> Use this harness to time your own image locally. It will not tell you where you would rank, and
> the number it prints is not comparable with anybody else's.
>
> Three bounds apply, because a practice run still executes an untrusted image on your machine: a
> hard deadline (`--timeout-sec`, container killed through its cidfile), capped log capture, and a
> C3 no-follow **sanitizing** copy for retained output. The harness never re-invokes the
> participant's image for housekeeping — the `_reclaim_output` helper that did is deleted.

The **primary** Track-3 rank is raw `events_per_sec` aggregated over the complete C1 roster,
descending, measured by the RUNNER on the official benchmark hardware (see
`../baselines/README.md`). Everything in this directory is the **secondary metrics layer**
(Phase 4/5): re-timing, diagnostics, the speed–realism frontier, the four special awards,
and the SimProfile verifier. **None of it re-orders the primary `events/sec` leaderboard** —
these signals are *reported, not ranked*.

Local retained output defaults to **10 GiB per file and 10 GiB per invocation**, including
all subs of a batch. The authorized local arm64 experiment freezes
`--execution-platform linux/arm64 --disk-gib 64` in its benchmark plan, allowing
64 GiB for both the bounded scratch disk and retained output. This matches the
bounded local scratch disk, so reception does not add
a smaller byte ceiling to a completed run. The earlier shared defaults of 64 MiB/file and
256 MiB/tree rejected validly named large traces before local scoring. Both the copy and its
verification use the same expanded limits; records include `retention_byte_limits`.
The path allowlist, file-count/depth bounds and no-follow checks still apply.
This is a local experiment assumption while the official output cap remains unpublished
([organizer reply](https://github.com/Agenthon-2026/track3-simulation-public/issues/1#issuecomment-5534948011)),
not proof of platform acceptance. The card's `disk = "10G"` and the local runtime disk cap
remain the default for amd64. The arm64 results carry `qualification_scope: local-arm64`
and cannot share promotion history with another platform or resource policy. Official
10G adaptation requires separate acceptance. A complete trace must still fit the
declared disk; a shorter diagnostic cannot qualify it.

They are computed on the DEVELOPER profile only. On the official path the private
`final_scorer` omits them and records why: GPU-award eligibility keys on measured GPU
utilization, and the C2 telemetry block does not yet carry a participant-cgroup-attributed GPU
busy-seconds field, so computing eligibility from this harness's own sampler on an official run
would be deriving an award from inadmissible evidence. That field is an open contract request.

---

## 1. Run telemetry

Every run's `/output/events.json` carries the six schema keys (`scenario_id`, `n_events`,
`wall_clock_sec`, `events_per_sec`, `seed`, `trace_sha256`) plus two telemetry keys consumed
by this layer:

| Key | Type | Meaning |
|---|---|---|
| `peak_memory_bytes` | int | peak resident memory over the run |
| `gpu_seconds` | number | GPU time consumed (0 when the run never touched the device) |

Both are **self-reported by the submission**, exactly like `wall_clock_sec` — the container
writes what it observed. The host **re-measures** wall-clock and peak memory independently
(`timer.py`, §2), so the diagnostics and the SimProfile verifier never have to trust the
submission's own numbers. `peak_memory_bytes` / `gpu_seconds` are telemetry only; a run is
never made inadmissible for a missing or implausible value (the g1 schema gate checks only the
six required keys).

---

## 2. `timer.py` — local timing protocol

The developer measurement runs a candidate Docker image N times (default 5) with
seeds drawn from a seed family derived from the scenario's base seed, **discards the first run
as warm-up**, and reports the **median `events/sec`** plus the full distribution. Warm-up is
discarded to remove cold-start effects (JIT/XLA kernel compilation, OS page cache, Python
import overhead) that are not intrinsic to the simulator; `--no-discard-warmup` includes all
runs for pure-binary submissions. These are local defaults, not the final evaluation plan.
Official repeat counts and warm-up treatment are committed in that plan; the official scorer
requires each measured repeat to reproduce the scored output, rather than substituting this
tool's varying-seed runs.

```
python timer.py \
    --image ghcr.io/my-org/my-sim:latest \
    --scenario /path/to/scenario.json \
    --runs 5 \
    --discard-warmup \
    --output results.json
```

Python API: `measure_throughput(image, scenario_path, runs=5, discard_warmup=True) ->
ThroughputResult` (`.median_events_per_sec`, `.std_events_per_sec`). The host-measured
wall-clock and peak memory it captures are the ground truth against which the SimProfile
verifier (§7) is checked.

---

## 3. `diagnostics.py` — the secondary diagnostics

Pure functions over the `events.json` telemetry, the CPU-ABIDES baseline throughput, and the
card `[environment]`. `compute(...)` returns a frozen `Diagnostics(speedup_vs_cpu_abides,
efficiency, efficiency_unit, memory_efficiency, telemetry_self_reported, gpu_utilization)`.
**Reported, not ranked** — the primary leaderboard is untouched by everything here.

| Diagnostic | Field | Definition |
|---|---|---|
| Speedup | `speedup_vs_cpu_abides` | submission `events/sec` ÷ the supplied CPU-ABIDES reference rate; comparable only when the measurement conditions match |
| Efficiency | `efficiency` | `events/sec` ÷ GPU-hours when `gpu_seconds > 0` (the run actually used the device) **or** ÷ CPU-core-hours (`cpus × wall_clock_sec`) otherwise; `efficiency_unit` is `events_per_gpu_hour` or `events_per_cpu_core_hour` |
| Memory efficiency | `memory_efficiency` | `n_events` ÷ `peak_memory_bytes` (events per resident byte; higher = more compact) |
| GPU utilization | `gpu_utilization` | measured `gpu_seconds` ÷ `wall_clock_sec`. **Eligibility signal, never ranked on** — it is how the GPU award separates "the device was attached" from "the device was used" (§6) |

Any field is `None` when its input is unavailable (no baseline, zero telemetry). A submission
that wins throughput by burning more cores is penalized on efficiency exactly as one burning
more GPU-hours would be. Every Track-3 unit card declares `[environment] gpu = true` — a GPU is
attached to every timed run, and using it is optional — so the card flag no longer separates
"GPU units" from "CPU units": the efficiency branch turns on `gpu_seconds > 0`, i.e. on whether
the run actually used the device.

---

## 4. `report.py` — offline per-unit + aggregate report

Consumes a submission's per-unit `events.json` plus the matching reference `events.json`
(whose `events_per_sec` is that unit's CPU-ABIDES baseline) and emits the three diagnostics per
unit and their medians across the submission. When a `<unit>/card.toml` is absent (or omits the
keys), card `[environment]` `cpus`/`gpu` fall back to the frozen Track-3 container contract —
`4` / `true` — never to a CPU-shaped guess, which would silently discard a run's real GPU time.
The fallback cannot invent GPU accounting either: the efficiency branch still requires
`gpu_seconds > 0`.

```
python -m throughput.report --submission <out_dir> --reference <ref_dir> --out report.json
```

Both dirs hold one `<unit>/events.json` per unit, matched by subdirectory name. Output JSON:
`{"units": {<unit>: <Diagnostics>...}, "aggregate": {"median_speedup_vs_cpu_abides",
"median_efficiency", "median_memory_efficiency", "n_units"}}` (medians skip `None`). Emitted
as a local report; it neither establishes a live leaderboard result nor re-orders the primary ranking.

---

## 5. `frontier.py` — speed–realism frontier

Each admissible submission is a `FrontierPoint(label, throughput, sf_divergence)`, where
`throughput` is `events/sec` and `sf_divergence` is the normalized aggregate of its
stylized-fact divergences — `realism_from_divergences(divergences, ceilings)` returns the mean
of `divergence / ceiling` over the reported facts (KS, ACF, Hill, depth-JS), keeping facts on
different scales commensurable. A bounded `realism = 1 / (1 + sf_divergence)` in `(0, 1]` is
exposed as a property. `pareto_frontier(points)` returns the Pareto set — submissions no other
submission beats on **both** speed and realism — sorted by descending throughput. It
contextualizes the leaderboard ("who is buying speed with realism") without re-ordering it.

---

## 6. `awards.py` — four special awards

Each award is a ranking over one diagnostic, computed offline across admissible submissions;
each sits **beside** the primary leaderboard and never re-orders it. `select_all(entries)`
returns all four winner labels (or `None` when no submission is eligible):

| Award | Selector | Basis |
|---|---|---|
| Best GPU Acceleration | `best_gpu_acceleration` | highest `speedup_vs_cpu_abides` among submissions whose **measured** `gpu_utilization` reaches `GPU_UTILIZATION_FLOOR` — see the note below |
| Best Speed–Realism Frontier | `best_speed_realism_frontier` | Pareto-frontier point maximizing balanced `throughput × realism` |
| Best Latency-Semantics Preservation | `best_latency_semantics_preservation` | tightest `latency_margin` (most faithful kernel timing) |
| Best Systems Diagnosis | `best_systems_diagnosis` | best SimProfile `quality` (§7) |

> **Why Best GPU Acceleration does not rank on `efficiency`.** It used to, and that was inverted.
> `efficiency = events_per_sec / (gpu_seconds / 3600)` puts GPU time in the *denominator* while
> eligibility was only `gpu_seconds > 0`, so the metric decreases monotonically in GPU use and the
> award goes to whoever touches the device least — measured, a CPU simulator issuing one tiny memcpy
> beat a saturated genuine port by roughly 3,700×. Host-side measurement does not fix it: those
> `gpu_seconds` are exactly what NVML honestly reports, so the winner is not lying.
>
> A utilization floor alone does not fix it either — with `gpu_seconds` still in the denominator a
> floor just relocates the optimum onto itself. So eligibility and ranking are separated: a run
> qualifies by having *used* the device (`gpu_utilization >= GPU_UTILIZATION_FLOOR`, host-measured),
> and qualifying runs are ranked by **speedup over the CPU-ABIDES baseline**, which contains no
> `gpu_seconds` and so leaves nothing to minimise. Utilization is never ranked on either, since
> rewarding it would only invert the defect the other way and pay submissions to keep the device
> pointlessly busy. `efficiency` stays a reported diagnostic — it orders correctly among submissions
> at comparable utilization — but it no longer decides the award.

---

## 7. `simprofile.py` — SimProfile verifier

A submission may attach a `profile.json` sidecar declaring where the wall-clock went, plus GPU
utilization and a peak-memory claim:

```json
{
  "components": {"matching": <sec>, "event_queue": <sec>, "latency": <sec>,
                 "agent_logic": <sec>, "io": <sec>},
  "gpu_utilization": <float in [0, 1]>,
  "peak_memory_bytes": <int>
}
```

`verify_profile(profile, *, wall_clock_sec, peak_memory_bytes, tol=0.10) -> ProfileVerdict`
validates the profile against the run's **host-measured** wall-clock + peak memory (from
`timer.py`, §2): non-negative component times, components summing to the wall-clock within
`tol`, `gpu_utilization` in `[0, 1]`, and a peak-memory claim not wildly under-reported.
`ProfileVerdict(valid, breaches, quality)` scores `quality` in `[0, 1]` (granularity ×
consistency, zeroed by any breach); this feeds the **Best Systems Diagnosis** award. SimProfile
is **diagnostic only — never an admissibility gate**; a submission is never rejected for an
absent or poor profile.

---

## 8. Local reports and official scoring

`throughput.report.build_report(output_dir, reference_dir)` produces developer diagnostics.
The official scoring path omits those diagnostics and records the reason, as described at
the top of this page. The existence of a local speedup or award calculation does not establish
the corresponding official measurement or a leaderboard column.

The primary score is the arithmetic mean of per-unit rates over the complete evaluation roster,
not the median of a throughput-only subset. An admissible submission retains its score and
rank when the informational `t3.throughput_nonimproving` label is present; see
`../baselines/README.md` §3.

### The ranked number's provenance

For each official unit, `telemetry.ranked_timing` derives the median of the measured repeat
rates from trusted C1/C2 evidence. It checks the committed repeat policy, measurement controls,
and agreement with the scored output. `host_metrics.json` and the participant's `events.json`
remain useful locally but cannot replace that evidence.

There are two separately named factories:

| Factory | Score source | `rankable` | Used for official ranking |
|---|---|---|---|
| `build_verifier` (**production**) | trusted C1 + C2 timing only | `True` | yes |
| `build_developer_verifier` | harness file, else the self-report | **always `False`** | no |

`_official_score` reads `ctx["_t3_timing"]` and nothing else — its docstring states that there is
no branch in it that reads the submission's own `events_per_sec`. Handed a context without trusted
evidence, the production factory raises `OrganizerFault` naming
`build_developer_verifier`; it does not quietly degrade. The self-report path survives only in the
developer factory, where every result carries `rankable = False`.

`host_metrics.json` therefore still matters for local practice, but the **ranked** number no longer
depends on it — C2 supplies host-measured timing.

> **`QFB2_T3_REQUIRE_HOST_TELEMETRY` no longer exists.** It was the flag that would one day have
> made the fallback fatal. Setting it anywhere — worker, scoring container, CI — does nothing, on
> any component built from this revision. `tests/test_scoring_gate.py` asserts both that the name
> is absent from `scoring.py` and that `os.environ` is absent from it entirely, so no environment
> variable can ever again decide whether the ranked path has a fallback.
>
> If you are reading the flag in a shipped artifact, that artifact predates this change and the
> fix is to rebuild it, not to set the variable.
