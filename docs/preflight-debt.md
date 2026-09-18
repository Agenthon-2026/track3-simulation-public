# Preflight Coverage

Repository CI and `scripts/preflight.sh` cover the participant preparation tool.
Some checks still require infrastructure unavailable to the public test runner.

| Gap | Current Check | Resolution |
| --- | --- | --- |
| Shared dev-rules hooks are not installed in this fork | Explicit preflight before commits/pushes and the PR CI job | Integrate the managed rules/hook distribution when onboarding this fork; this PR does not vendor a second rules copy |
| Docker correctness and registry reachability are not PR unit tests | `prepare_submission.py verify` performs real image runs and writes evidence | Run before each candidate-image submission, and after toolkit/scoring changes |
| Submission packaging changed through toolkit v2.4.3 | Existing tests still check the superseded assigned-ID and single-file ZIP flow | Follow [the current plan](SUBMISSION-READINESS.md): migrate to derived team identity, `models: []`, `category: simulator`, and official team-claim 2.0 packing; separately confirm the competition URL |
| Large exemplar and official hardware measurements | Named as unverified in the readiness document and verification artifact | Resolve on suitable amd64 hardware and the official evaluator before Final |
