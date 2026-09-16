#!/usr/bin/env bash
# Build the Track 3 baseline image and validate it against the shipped public
# reference traces using the local regression harness. This does not produce an official score.
#
#   ./baselines/build_and_validate.sh [image_tag]
#
# Requires: docker (with buildx for --platform on non-amd64 hosts) and a Python
# env with qfbench2-common + numpy/pandas/scipy/pyarrow installed (for the harness).
# First fetch the public LFS data and run regression_suite/build_reference_cache.py
# in the Python 3.13 scoring environment; see baselines/README.md.
#
# A clean run prints PASS for every public scenario. Because scoring is semantic
# (Tier A exact fills + stylized-fact proximity), the linux/amd64 image must
# reproduce the reference traces' fill sequences. If a mismatch appears, preserve
# the supplied references and diagnose the build, patches, random streams and output.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # baselines/
REPO="$(cd "$HERE/.." && pwd)"                          # track3-simulation-public/
IMAGE="${1:-track3-abides-baseline:latest}"

echo ">>> Building $IMAGE (linux/amd64) ..."
docker build --platform=linux/amd64 -t "$IMAGE" "$HERE"

echo ">>> Running regression harness against $IMAGE ..."
python "$REPO/regression_suite/run_regression.py" \
    --candidate-image "$IMAGE" \
    --scenarios-dir "$REPO/regression_suite/scenarios" \
    --reference-dir "$REPO/regression_suite/reference_traces" \
    --output-dir "$REPO/regression_suite/run_outputs" \
    --workers 3
