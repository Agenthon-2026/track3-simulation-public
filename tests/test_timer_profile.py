"""The standalone timer must label every serialized result as a developer preview.

Runs without Docker, the toolkit, or pytest. Only container invocation is stubbed;
the actual aggregation, CLI parser, stdout JSON and output-file path execute.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_PATH = Path(__file__).resolve().parents[1] / "throughput" / "timer.py"
_SPEC = importlib.util.spec_from_file_location("_t3_timer_profile", _PATH)
assert _SPEC is not None and _SPEC.loader is not None
timer = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = timer
_SPEC.loader.exec_module(timer)


class TimerProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.scenario = self.root / "scenario.json"
        self.scenario.write_text(json.dumps({"scenario_id": "synthetic", "seed": 7}))

    def measurements(self) -> list:
        return [
            timer.RunMetrics(10.0, 10.0, None, None),
            timer.RunMetrics(20.0, 5.0, 2.0, 100),
            timer.RunMetrics(40.0, 2.5, 4.0, 300),
        ]

    def test_serialization_preserves_measurements_and_warmup_statistics(self) -> None:
        for discard, aggregates in [
            (True, (30.0, 30.0, 10.0, 20.0, 40.0)),
            (False, (20.0, 70.0 / 3.0, 12.472191289246473, 10.0, 40.0)),
        ]:
            with self.subTest(discard_warmup=discard):
                with patch.object(timer, "run_single", side_effect=self.measurements()):
                    with contextlib.redirect_stdout(io.StringIO()):
                        result = timer.measure_throughput(
                            "synthetic:local",
                            self.scenario,
                            runs=3,
                            discard_warmup=discard,
                        )
                record = result.to_dict()
                self.assertEqual(record.pop("profile", None), "developer")
                self.assertIs(record.pop("rankable", None), False)
                median, mean, std, minimum, maximum = aggregates
                self.assertAlmostEqual(record.pop("std_events_per_sec"), std)
                self.assertEqual(
                    record,
                    {
                        "image": "synthetic:local",
                        "scenario_id": "synthetic",
                        "seed_family": result.seed_family,
                        "raw_events_per_sec": [10.0, 20.0, 40.0],
                        "warmup_discarded": discard,
                        "median_events_per_sec": median,
                        "mean_events_per_sec": mean,
                        "min_events_per_sec": minimum,
                        "max_events_per_sec": maximum,
                        "wall_clock_seconds": [10.0, 5.0, 2.5],
                        "host_gpu_seconds": [None, 2.0, 4.0],
                        "median_host_gpu_seconds": 3.0,
                        "host_peak_memory_bytes": [None, 100, 300],
                        "median_host_peak_memory_bytes": 200,
                    },
                )

    def test_cli_stdout_and_output_file_carry_the_same_developer_record(self) -> None:
        output = self.root / "nested" / "result.json"
        stdout = io.StringIO()
        with patch.object(timer, "run_single", return_value=self.measurements()[1]):
            with contextlib.redirect_stdout(stdout):
                timer.main(
                    [
                        "--image",
                        "synthetic:local",
                        "--scenario",
                        str(self.scenario),
                        "--runs",
                        "1",
                        "--no-discard-warmup",
                        "--output",
                        str(output),
                    ]
                )
        record = json.loads(output.read_text())
        stdout_text = stdout.getvalue()
        printed, _ = json.JSONDecoder().raw_decode(
            stdout_text[stdout_text.index("\n{") + 1 :]
        )
        self.assertEqual(printed, record)
        self.assertEqual(record.get("profile"), "developer")
        self.assertIs(record.get("rankable"), False)
        self.assertEqual(record["raw_events_per_sec"], [20.0])
        self.assertEqual(record["wall_clock_seconds"], [5.0])
        self.assertEqual(record["median_events_per_sec"], 20.0)
        self.assertEqual(record["std_events_per_sec"], 0.0)


if __name__ == "__main__":
    unittest.main()
