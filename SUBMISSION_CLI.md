# QFBench 2.0 — Published Submission CLI Contract (`interface_version = 2.0`)

A submission is a **Docker image**. The organizer runs it in the sealed scoring environment;
the image must implement the track verb below. The harness invokes the image, the image reads
from a read-only input mount, writes to an output mount, and **exits 0** on success.

```
docker run --rm \
  --network=none|qfb2-eval \             # "none" (simulation) or the internal eval network (agent tracks) — see "Network modes"
  --cpus=<card.cpus> --memory=<card.memory> [--gpus all] \
  -v <unit-dir>:/input:ro \              # read-only inputs — the UNIT DIRECTORY itself is mounted at /input
  -v <run>/output:/output \              # outputs (deliverables + logs); a normal read-write bind
  [-v <run>/output:/app/output] \       # T1 ONLY: the same host dir, also at the QFBench path (see invariant 8)
  <SUBMISSION_IMAGE> <verb> [args]
```

**The verb is the container command.** It arrives as the first argument after the image
reference, so your image must either resolve it from `PATH` (build with no `ENTRYPOINT` — the
Track 3 reference baseline does this, shipping `simulate` and `simulate-batch` as executables)
or consume it as a leading positional (the `ENTRYPOINT ["python", "agent.py"]` pattern, where
`agent.py` declares `parser.add_argument("verb")`). An image that does not accept the verb fails
every unit — as `127` if the verb is not on `PATH`, as `126` if it is present but not executable,
or as whatever your own argument parser exits with if it consumes and rejects it. All three are
recorded as **your** failure, not an organizer fault, and score zero on that unit.

The harness logs `sha256(image)` (anti-cheat), enforces
`card.environment.{cpus,memory,gpu,timeout,network}`, and mounts only files whose `manifest.json`
checksum matches. `LABEL qfbench2.interface_version="2.0"` is required on the image.

## Network modes (per unit card, `[environment].network`)

There are exactly two network modes; every unit card declares one. **There is never open
internet** in official scoring.

| Mode | Who | Meaning |
|---|---|---|
| `none` | **Simulation (T3)** | Fully offline (`--network=none`). Exactly the historical closed-resource behavior; any attempted outbound connection fails the run. |
| `restricted` | **Agent tracks (T1 coding, T2 forecasting, T4 analysis)** | No open internet. Egress **only** through the organizer's audited proxy to the **organizer-hosted model endpoint** given by `MODEL_ENDPOINT` (open models, free to use, per-run budget). Every connection is logged (domain, bytes, timestamps); the log is the audit artifact for the verification phase. |

> ### ⚠️ Agent tracks: there is no third-party model-API access
>
> Read this before you design your agent. (Track 3 is unaffected — it runs fully offline.)
>
> The proxy allowlist contains the organizer-hosted endpoint and **nothing else**. Calls to
> `api.anthropic.com`, `api.openai.com`, `generativelanguage.googleapis.com` or any other vendor
> API **will be refused by the proxy**, and there is no route around it: the eval network is
> `--internal`, so the proxy is the only path off the host.
>
> Your two supported options are therefore:
>
> 1. **House endpoint** — call `MODEL_ENDPOINT` with `MODEL_NAME`. Free, metered per run.
> 2. **Bring your own adapter** — ship one LoRA adapter, rank ≤ 64, for the organizer-served
>    base. Call the same endpoint with your supplied adapter model id. See
>    [adapter-only BYO](#adapter-only-byo); do not bundle full model weights or run a model server.
>
> **No participant API keys exist.** The harness injects none and there is no mechanism for a
> submission to supply one, so a vendor key would have nothing to reach even if you had one.

Data and text cutoffs (gate `g2_cutoff_resource`) are unchanged and still enforced by the harness
in both modes — network access is for **model calls only**, never for fetching data.

### Submission categories

**Every submission declares a `category` in `submission.json`, and for Track 3 it is `simulator`.**
The field is required and the value is a closed enum: an absent category is a validation error, not
a default.

This page previously said "Track 3 sits outside these categories", and that was the dangerous
half-truth. Track 3 is outside the *agent tiers* — it runs at `network = "none"` and calls no model
— but the descriptor validator still requires the field, and the wrapper it validates against
coerced an absent value to `api`. The old page described `api` as the CPU tier, so omission
recorded the wrong submission kind. Track 3 still requires its dedicated GPU queue.

| Category | Track | What you bundle | Model access | Compute tier |
|---|---|---|---|---|
| `simulator` | **3 (simulation)** | a market simulator image; the CUDA runtime and every dependency **vendored** | none — `network = "none"` | the dedicated Track 3 queue, one worker, GPU attached |
| `api` | 1 / 2 / 4 | prompts / harness / system-prompts / agents (your contribution is the scaffolding) | the **house endpoint only**, via the proxy | the unit card's resource limits |
| `byo-large` / `byo-small` | 1 / 2 / 4 | one LoRA adapter plus your agent code | the house endpoint serving the organizer's base with your adapter loaded | the unit card's resource limits |

The `byo-*` descriptor values are legacy names, not separate small- and large-weights tiers.
The adapter requirement applies to BYO model submissions on the agent tracks; Track 3 uses
`simulator` and needs no adapter.

Queue routing keys on the **track**, not on the string you write: the category records what kind of
submission this is, and cannot be used to select a different box.

The canonical example descriptor is published by the shared toolkit, one per (track, phase), at
`qfbench2_common/contracts/fixtures/c5/simulation_<phase>.json`. Use those bytes; a hand-written
third example is how the generated wrapper and the documentation drifted apart in the first place.

### Adapter-only BYO

This section applies to Tracks 1, 2 and 4. Track 3 remains offline and calls no model.

A LoRA (low-rank adaptation) adapter contains parameter updates for the organizer's base model.
Package exactly one `adapter_model.safetensors` and `adapter_config.json` pair together in your
image. Use LoRA rank ≤ 64 and declare `target_modules` accurately. Full fine-tuning and shipping
full model weights are not permitted. The directory for extraction is supplied with submission
instructions; keep the pair in one relocatable directory rather than guessing a required path.

The BYO contract assigns serving to the organizer: static extraction runs none of your code;
the organizer starts the base with your adapter loaded and tears the server and extracted adapter
down when the submission finishes. Your submission must not run a model server. Its client uses
the supplied `MODEL_ENDPOINT` and `MODEL_NAME`; for BYO, `MODEL_NAME` identifies your adapter.
These are the packaging and serving requirements, not a statement that a particular endpoint
is currently available. See the [published BYO guide](https://github.com/Agenthon-2026/Agenthon2026-public/blob/09873cad2f3ea1171acd4e19cd8c10b3cb6a126f/starter-packs/track4/AGENTS.md#L401)
for local adapter preparation.

### Container environment contract (`restricted` mode, set by the harness)

| Variable | Value |
|---|---|
| `HTTP_PROXY` / `HTTPS_PROXY` | the audited egress proxy. **Read these from the environment; never hardcode a proxy host** — the address is an operational detail and it has changed. Most HTTP clients honour them automatically |
| `NO_PROXY` | hosts that must bypass the proxy |
| `MODEL_ENDPOINT` | the organizer-hosted OpenAI-compatible endpoint. This is the **only** model API you can reach |
| `MODEL_NAME` | the organizer-supplied model id for this run: the house model for `api`, or your adapter for BYO. Use it unchanged in client calls |
| `QFBENCH_NETWORK` | `restricted` (or `none` for simulation / local fallback) |

### Rules for model-API use (`restricted` mode)

1. **Vendor-side tools OFF.** Web search, code execution, retrieval, and any other vendor-side
   tool MUST be disabled in every API call. Enforced by rule + audit of the proxy logs.
2. **Pin model versions.** The house endpoint serves the organizer's pinned base; for a BYO
   adapter, pin its exact revision too. Floating aliases (`*-latest`) are not reproducible
   and are rejected at verification.
3. **Disclose training cutoffs.** The training cutoff of every model used, including BYO adapters, MUST
   be declared in submission metadata (`models[].training_cutoff` in `submission.json`).
4. **Pin temperature/seed** where the API supports it. `api`-category entries are verified
   *statistically* (bootstrap-CI overlap on organizer rerun for T2/T3/T4; for T1, the single-pass
   per-unit verdicts must agree exactly); BYO entries bit-reproducibly.
5. **Track 3 stays offline.** Simulation uses `network = "none"` and makes no model-API
   calls. Model-using submissions follow the House API allocation in their own track's guide:
   [Track 1](https://github.com/Agenthon-2026/track1-coding-public/blob/main/SUBMISSION_CLI.md#rules-for-model-api-use-restricted-mode),
   [Track 2](https://github.com/Agenthon-2026/track2-forecasting-public/blob/main/SUBMISSION_CLI.md#rules-for-model-api-use-restricted-mode), or
   [Track 4](https://github.com/Agenthon-2026/track4-analysis-public/blob/main/SUBMISSION_CLI.md#rules-for-model-api-use-restricted-mode).
   Those rules do not give a simulator model access.

**One leaderboard.** All categories rank on a single board; every entry is tagged with its
category, the models used (pinned versions), and their training cutoffs.

| Track | Verb | Inputs (under `/input`) | Required output (under `/output`) |
|---|---|---|---|
| **T1 Coding** | `solve --task-dir /input --out /app/output` | task spec + environment files | task-specified deliverables written to **`/app/output`** (QFBench/Harbor convention); the `checks/` step asserts correctness and writes **both** `reward.txt` and `reward.json` (see T1 note below) |
| **T2 Time-Series Forecasting** | `forecast --panels /input/panels/ --text /input/text/ --asof <YYYY-MM-DD> --out /output/forecast.parquet` | `panels/` — multivariate time-series parquet files; `text/` — time-stamped text corpus (news, FOMC, macro releases); all timestamps must be ≤ `--asof` (gate g2 enforces both) | joint predictive distribution conforming to `forecast.schema.json`; sidecar `forecast_meta.json` required; **`forecast_rationale.md` required and never scored** (see T2 note below) |
| **T3 Simulation** (single scenario) | `simulate --config /input/scenario.json --out /output/trace.parquet` | scenario config + ABIDES environment | message-level trace conforming to `sim_scenario.schema.json` + `events.json` (counts/timing) |
| **T3 Simulation** (batched, family GB) | `simulate-batch --batch-dir /input/scenarios --out-dir /output` | `batch.json` — the sub-scenario roster; `scenarios/` — one config per sub-scenario. These units have **no** top-level `scenario.json`. | one output subdir per sub-scenario, each with `trace.parquet` + `events.json`, plus `batch_events.json` at the root of `--out-dir` |
| **T4 Tabular Prediction** | `analyze --task /input/task.json --corpus /input/corpus/ --out /output/answer.json` | `task.json` — tabular dataset (rows = entities) + target column spec + `target_type` (classification/regression/ranking); `corpus/` — frozen evidence corpus | prediction + interval + citations per row conforming to `analysis.schema.json`; `target_type` in output must match card |

> **T2 status (2026-08-20).** The unit-layout half of this is closed: `--panels` names the STAGED
> unit's `panels/` directory. `stage_bundle.py` relocates root panels into `panels/` and its S6
> gate refuses to emit a unit whose `panels/` is empty, and participant containers mount the
> staged tree, never the raw repo. Verified by execution: `--panels /input/` dies with "no
> .parquet found"; `/input/panels/` scores the full chain. The interface half ships in `track2-forecasting-public` — a `forecast`
> CLI, a console-script entry point and a `Dockerfile`, built and run on linux/arm64 (GH200) and
> admitted by g0–g3 against the exemplar unit under `--network=none`.

> **T2 note — `forecast_rationale.md`.** Alongside `forecast.parquet` a submission writes
> `forecast_rationale.md` to `/output`: the derivation behind the distribution. Numbered steps —
> the anchor, each adjustment with its size and what supports it, then the scale and shape —
> naming the series and dates computed from and the documents cited, and ending with an
> adjustment ledger so the arithmetic can be followed.
>
> **It is required and it is never scored.** No submission is ranked higher or lower because of
> this file; `g1_schema` checks only that it exists and is non-empty, and no scoring code reads
> its content. It exists because a submission that recalled its answer and one that derived it
> are indistinguishable as a set of draws, so the parquet alone cannot support any review at all.
>
> It is read as a **screen over the top of the leaderboard**, not a gate: a flag opens a human
> review and cannot by itself produce a DNF or move a score. That restriction stands until a
> false-positive rate has been measured on a large honest corpus — the current measurement is
> 4/4 true positives and 0/4 false positives, and 0-of-4 carries a 95 % interval reaching ~0.6.
> Two alternative mechanisms were tested and rejected: re-executing the trace (backward-built
> traces reproduced *more* exactly than honest ones, so as a gate it favours the cheater) and
> scanning the text for leakage admissions (a guarded prompt drove self-declaration from 100 % to
> 0 % with no change in the numbers). Method and data:
> `track2-forecasting-public/docs/RATIONALE-REVIEW.md`.

**Your image must implement BOTH Track 3 verbs.** The harness picks the verb per unit, from the
unit's contents: a unit carrying `batch.json` + `scenarios/` is dispatched to `simulate-batch`,
everything else to `simulate`. Six of the public dev units (`t3-gbatch-*`) are batched.

**Contract invariants (enforced by gate `g0_integrity` / `g1_schema` / `g2_cutoff_resource`):**

1. The image must honor the card's network mode: `none` (simulation) means fully offline — any
   attempted outbound connection fails the run; `restricted` (agent tracks) means egress only
   through the audited proxy to the house model endpoint — any connection outside that allowlist
   fails the run, and no vendor model API is on it.
2. Output must validate against the track output schema *before* any scoring (`g1_schema`).
3. The image must not read any path outside `/input` and `/output`; the canary registry and held-out
   targets are never mounted.
4. Determinism: the harness sets `QFBENCH_SEED`; verification phase reruns on fresh seeds/resamples and
   compares against the final-phase result (reproducibility gate).
5. Wall-clock and resource caps are per-track (`card.environment`); exceeding them is a `g2` failure.
6. **T2 text cutoff (g2):** every document in `/input/text/` must have a timestamp field ≤ `--asof`.
   The harness checks text timestamps in addition to panel data timestamps. A document with a
   post-as-of date causes a `shared.leakage.cutoff_violation` failure label
   (`FailureLabel.LEAKAGE_CUTOFF` in `qfbench2_common.failure_labels`).
7. **T4 target type:** the `target_type` field in `/input/task.json` (and matching `card.toml`) declares
   the task as `classification`, `regression`, or `ranking`. The `answer.json` output must include a
   matching `target_type` field. Mixed task types within one unit are not allowed.
8. **T1 deliverable dir + dual reward (QFBench heritage):** Track 1 *is* QFBench, so it inherits
   QFBench's conventions. The agent writes its deliverables to **`/app/output`**, which is what
   `--out` is set to and what the units' `instruction.md` and `checks/test_outputs.py` say. The
   harness binds the run's output directory at **both** `/app/output` and `/output`, so the
   minority of units phrased against a bare `/output` are captured identically — writing to
   either path is safe, and neither is silently discarded.
   `checks/test.sh` runs **offline** via `python -m pytest` and writes **both**
   Harbor's `/logs/verifier/reward.txt` (1/0) **and** `<output>/reward.json` + `pytest_report.json`
   for the Agenthon g0–g3 verifier and DI failure-label overlay. The same unit therefore runs under
   **both** Harbor (`harbor run --path units ...`) and the Agenthon harness
   (`qfbench2 smoke <unit> <out> --track coding`). See
   `tracks/track1-coding/public/docs/QFBENCH-HERITAGE.md`. (T2/T3/T4 keep the generic `/output`
   contract above.)


## Open Division tag

Do **not** add `house_endpoint_only` -- or any key the descriptor schema does not list -- to
`submission.json`: the schema refuses unknown keys, so a submission carrying it is rejected
before it runs. Whether every model call used the house endpoint exclusively is read from
the audited egress-proxy logs during the verification phase; it drives an "Open Division"
display filter of the single leaderboard (never a separate ranking) and needs nothing
from you.
