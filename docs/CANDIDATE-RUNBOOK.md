# Local candidate qualification

Use immutable images and retain the evidence behind each decision. These tools
measure local, non-rankable results; the acceptance rules are in
[SUBMISSION-READINESS.md](SUBMISSION-READINESS.md). Run the controller on the native
Linux host that owns Docker so container memory and scratch use are measurable.

## Environment and images

Use Python 3.13 and the pinned toolkit:

```bash
pip install 'qfbench2-common[data] @ git+https://github.com/Agenthon-2026/Agenthon2026-public.git@v2.4.3#subdirectory=common' scipy
```

The root `Dockerfile` builds the complete participant. For a challenge, reuse its
immutable numerical runtime and rebuild both extensions. `BASE_IMAGE`, `IMAGE` and
`REFERENCE_IMAGE` below are registry references ending in `@sha256:...`; preserve
build logs and manifests. Set `TARGET=linux/arm64` for the authorized local scope,
or `linux/amd64` for the default compatibility scope.

```bash
docker build --platform "$TARGET" -t track3-base .
docker build --platform "$TARGET" --build-arg NATIVE_BASE="$BASE_IMAGE" \
  -f baselines/Dockerfile.native-candidate -t track3-challenge .
python scripts/validate_candidate_parameters.py reference-wheels --out out/reference-wheels
docker build --platform "$TARGET" --build-arg REFERENCE_BASE="$BASE_IMAGE" \
  -f baselines/Dockerfile.parameter-reference -t track3-reference .
```

The reference invokes pinned ABIDES with the existing patches, not `fast_sim`.
Its extra logging wheels are checked against committed hashes. Publish built
images with the authorized registry credential, then use their digests throughout.
PR CI rebuilds native modules and runs bounded streaming differentials; it does not
perform the full baseline or promote images.

## Delivery and independent parameters

`candidate.json` contains only image, license, website team number and phase.
The toolkit derives the team alias and builds both ZIP members. Keep the Team Key
in an owner-only file outside the repository and pass only its path.

```bash
python scripts/prepare_submission.py verify-delivery --candidate candidate.json \
  --execution-platform "$TARGET" --out out/delivery
python scripts/prepare_submission.py package --candidate candidate.json \
  --execution-platform "$TARGET" --verification out/delivery/delivery.json \
  --team-key-file /private/path/team-key --out out/development.zip
python scripts/validate_candidate_parameters.py generate --execution-platform "$TARGET" \
  --seed 9152400 --reference-image "$REFERENCE_IMAGE" --out out/holdouts
python scripts/validate_candidate_parameters.py run --plan out/holdouts/holdout.json \
  --image "$IMAGE" --out out/parameters
```

Packaging rechecks anonymous pulling and validates descriptor/claim binding; an
existing ZIP is never replaced. Parameters cover public families and batch sources,
legal seed/horizon/protocol variants and a native exception probe. Each comparison
uses shared semantics plus exact values and row order. Failed parameter sets become
regression evidence; implementation changes require a fresh set.

Create `out/holdout-index.json` as an image-to-evidence mapping using
`benchmark_candidates.index(Path('out/parameters/heldout-result.json'))`. Include
both images for paired experiments. The controller rechecks raw artifacts, complete
coverage and validator identity.

## Complete baseline

Prepare a dedicated disk-backed filesystem for `/mnt/t3-scratch`. Its capacity must
not exceed the plan's disk budget. The image runs read-only with `/tmp` and `/output`
on this filesystem; retained artifacts live outside it. Leave host memory for the
controller and bounded verifier in addition to the participant's container cap.

For the authorized local arm64 experiment, freeze 64 GiB explicitly:

```bash
python scripts/benchmark_candidates.py freeze --image "$IMAGE" --purpose baseline \
  --execution-platform linux/arm64 --disk-gib 64 --round B0 \
  --hypothesis 'Qualify the exact CPU baseline' --timeout 28800 --budget 259200 \
  --history out/arm64-history --holdout out/holdout-index.json --out out/b0-plan.json
python scripts/benchmark_candidates.py run --plan out/b0-plan.json \
  --scratch-volume /mnt/t3-scratch --out out/b0
python scripts/benchmark_candidates.py assess --evidence out/b0/evidence.json
python scripts/benchmark_candidates.py decide --evidence out/b0/evidence.json \
  --g1 out/development.g1.json --history out/arm64-history
```

Deadlines are local bounds, not official limits. The amd64 profile defaults to
10 GiB; use a separate history for each architecture/resource policy. Retention
uses the same byte budget as scratch, with shared no-follow sanitation, path and
file-count limits. Official output reception remains separately unverified.
`--diagnostic` never qualifies G2/G3.

Every invocation saves `execution.json` before retention/parsing and
`measurement.json` before semantic verification. Verification runs in a separate
process, capped at 4 GiB virtual memory on Linux and 30 minutes or the remaining
experiment budget. The exemplar is decoded one row group at a time; its reference
semantics remain unknown. Failures stop the plan, preserve evidence and return a
nonzero CLI status. SIGKILL cannot write a final record; the early checkpoints
remain evidence of the completed execution, not proof of acceptance.

## Challenge and history

Freeze a `screen` first: it covers each family's short/long horizon, every batch
and the unchanged exemplar, with one warmup and one sample. Supply realistic time
bounds for that workload. Screens never produce a partial-roster score or G2/G3.

Then freeze `confirmation --baseline "$B0" --image "$B1"` with a new output path,
the fixed hypothesis and both images' parameter evidence. The full protocol uses
one warmup and five complete paired groups, alternating A/B order. A confirmation
reservation is written before measured invocation; disposable CI workers export
it with `export-confirmation` and upload it before running. Restored reservations
prevent interrupted or expired experiments from granting another attempt.

```bash
python scripts/benchmark_candidates.py decide --evidence out/confirmation/evidence.json \
  --g1 out/b0.g1.json --g1 out/b1.g1.json --expected-stable "$B0" --history out/arm64-history
python scripts/benchmark_candidates.py rollback --to "$B0" --expected-stable "$B1" \
  --reason 'Regression reproduced in retained evidence' --history out/arm64-history
```

History is append-only with a hash chain and a locked stable-pointer comparison.
Rollback requires current qualifying evidence and intact delivery artifacts for
an earlier stable image. Complete pairs requalify both arms, so the latest valid
pair can supply B0's rollback evidence after a controller update. Raw records,
failures and expired evidence are never replaced by hand-written passes.
