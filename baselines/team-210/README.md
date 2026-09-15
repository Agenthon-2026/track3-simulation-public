# Team 210 Simulation Agent Baseline

This directory packages the Team 210 participant simulation baseline for Track 3 (Accelerated Market Simulation).

## Architecture

1. **CLI Contract**: Implements both `simulate` and `simulate-batch` executables conforming to `interface_version = "2.0"`.
2. **Deterministic Execution**: Uses seeded ABIDES-jockeys simulation with strict event-level reproducibility.
3. **Optimized Kernels**: Ships vectorized order matching and state caching overlays.
4. **Network**: Fully offline execution under `--network=none` as required for Track 3.
