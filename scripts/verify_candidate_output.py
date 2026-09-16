"""Verify retained output in a bounded process so parser failure preserves the run.

The child reuses the public developer gates. Its memory and wall time are separate
from participant timing; a killed or timed-out verifier never qualifies a candidate.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
MEMORY_BYTES = 4 * 1024**3
TIMEOUT_SEC = 1800


def limit_memory(byte_limit: int = MEMORY_BYTES) -> None:
    if sys.platform == "linux":
        resource.setrlimit(resource.RLIMIT_AS, (byte_limit, byte_limit))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def verify(unit: Path, output: Path) -> dict:
    from check_exemplar_output import EXEMPLAR, verify as verify_exemplar
    from qfbench2_common.smoke import run_smoke
    from qfbench2_track_simulation.scoring import build_developer_verifier

    if unit.name == EXEMPLAR and not (unit / "trace.parquet").exists():
        return verify_exemplar(unit, output)
    result = run_smoke(unit, output, build_developer_verifier)
    return {
        "admissible": result.admissible,
        "gates": {
            k: {"passed": v.passed, "detail": v.detail}
            for k, v in result.gate_results.items()
        },
        "semantic_status": "unknown"
        if unit.name == EXEMPLAR
        else "passed"
        if result.admissible
        else "failed",
        "rankable": False,
    }


def bounded_verify(unit: Path, output: Path, timeout: float = TIMEOUT_SEC) -> dict:
    if timeout <= 0:
        raise TimeoutError("experiment deadline reached before output verification")
    env = {**os.environ, "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"}
    with tempfile.TemporaryDirectory(prefix="t3-verifier-") as temporary:
        report = Path(temporary) / "result.json"
        subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                str(unit),
                str(output),
                str(report),
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=min(timeout, TIMEOUT_SEC),
            check=True,
        )
        return json.loads(report.read_text())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("unit", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    limit_memory()
    result = verify(args.unit, args.output)
    with args.report.open("x") as handle:
        json.dump(result, handle, allow_nan=False)
