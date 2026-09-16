#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
python_bin="${PYTHON:-python3}"
git diff --check
git diff --cached --check
"$python_bin" -m ruff check scripts/prepare_submission.py scripts/benchmark_candidates.py scripts/validate_candidate_parameters.py scripts/check_exemplar_output.py scripts/verify_candidate_output.py tests/test_submission_preparation.py tests/test_candidate_benchmark.py tests/test_candidate_parameters.py tests/test_local_arm64.py tests/test_verifier_process.py
"$python_bin" -m ruff format --check scripts/prepare_submission.py scripts/benchmark_candidates.py scripts/validate_candidate_parameters.py scripts/check_exemplar_output.py scripts/verify_candidate_output.py tests/test_submission_preparation.py tests/test_candidate_benchmark.py tests/test_candidate_parameters.py tests/test_local_arm64.py tests/test_verifier_process.py
"$python_bin" -m ruff check scripts/restore_candidate_history.py baselines/fast_sim/streaming.py tests/integration/native_streaming_check.py
"$python_bin" -m ruff format --check scripts/restore_candidate_history.py baselines/fast_sim/streaming.py tests/integration/native_streaming_check.py
"$python_bin" .github/validate_units.py simulation --stdlib-only
"$python_bin" -m pytest tests/test_submission_preparation.py tests/test_candidate_benchmark.py tests/test_candidate_parameters.py tests/test_local_arm64.py tests/test_verifier_process.py -q
