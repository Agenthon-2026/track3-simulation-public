"""Compare compiled streaming output with the complete in-memory native engine.

Run inside the rebuilt participant image with this repository at /workspace:
python /workspace/tests/integration/native_streaming_check.py
These bounded differential cases are diagnostics, not the full G2 experiment.
"""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import pandas as pd
import pyarrow.parquet as pq

from abides_fork.config import build_config
from fast_sim.engine import reset_abides_counters
from fast_sim.native import as_pandas, snapshot_native, stream_native
from fast_sim._native import (
    classify_native_executions,
    run_native_sim,
    stream_native_sim,
)


ROOT = Path(__file__).resolve().parents[2]


def spec_for(scenario):
    reset_abides_counters()
    return snapshot_native(build_config(deepcopy(scenario)))


class NativeStreamingTests(unittest.TestCase):
    def compare(self, scenario, chunk_rows):
        spec = spec_for(scenario)
        started = time.perf_counter()
        original = run_native_sim(deepcopy(spec))
        buffered_seconds = time.perf_counter() - started
        with tempfile.TemporaryDirectory() as directory:
            paths = [
                Path(directory) / name
                for name in ("trace.parquet", "message_trace.parquet")
            ]
            started = time.perf_counter()
            results = stream_native(deepcopy(spec), paths, chunk_rows=chunk_rows)
            if original[0].num_rows > 1_000_000:
                print(
                    json.dumps(
                        {
                            "rankable": False,
                            "diagnostic": True,
                            "horizon_ns": scenario["horizon_ns"],
                            "buffered_kernel_and_conversion_sec": buffered_seconds,
                            "streaming_two_pass_and_write_sec": time.perf_counter()
                            - started,
                            "rows": [t.num_rows for t in results],
                            "files": {p.name: p.stat().st_size for p in paths},
                        }
                    ),
                    flush=True,
                )
            hashes = [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]
            for expected, result, path in zip(original, results, paths):
                actual = pq.read_table(path)
                self.assertTrue(
                    actual.equals(expected, check_metadata=False), str(path)
                )
                self.assertEqual(result.num_rows, expected.num_rows)
                if "t_send_ns" in expected.column_names:
                    pd.testing.assert_frame_equal(
                        pd.read_parquet(path), as_pandas(expected)
                    )
                if expected.num_rows > chunk_rows * 5:
                    self.assertGreater(pq.ParquetFile(path).num_row_groups, 1)
            stream_native(deepcopy(spec), paths, chunk_rows=chunk_rows)
            self.assertEqual(
                hashes,
                [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths],
            )

    def test_public_families_and_long_latency(self):
        cases = [
            "t3-s001-price-time-priority",
            "t3-mp01-stp-newest-baseline",
            "t3-st05-latency-spike-pareto",
            "t3-ra01-fundamental-shock-mid",
            "t3-sf-05-deep-book-liquidity",
            "t3-ca-thin-book-depth",
            "t3-as06-throughput-fast",
        ]
        for unit in cases:
            with self.subTest(unit=unit):
                scenario = json.loads(
                    (ROOT / "units" / unit / "scenario.json").read_text()
                )
                # Bounded test only. The acceptance roster is never shortened.
                scenario["horizon_ns"] = min(scenario["horizon_ns"], 100_000_000)
                self.compare(scenario, 137)

    def test_reordered_execution_and_cancel_delivery(self):
        scenario = json.loads(
            (ROOT / "units/t3-mp01-stp-newest-baseline/scenario.json").read_text()
        )
        scenario["horizon_ns"] = 30_000_000
        scenario["latency_config"] = {
            "model": "uniform",
            "params": {"min_ns": 0, "max_ns": 7_000_000},
        }
        for policy in ("cancel_newest", "cancel_oldest"):
            scenario["exchange_config"]["stp_policy"] = policy
            for seed in (91571, 91572, 91573):
                with self.subTest(policy=policy, seed=seed):
                    scenario["seed"] = seed
                    self.compare(scenario, 3)

    def test_dense_exemplar_prefix(self):
        scenario = json.loads(
            (ROOT / "units/t3-EXAMPLE-vectorized-matching/scenario.json").read_text()
        )
        scenario["horizon_ns"] = 1_000_000_000
        self.compare(scenario, 262144)

    def test_optional_ledger_preserves_exact_trace_and_message_count(self):
        from fast_sim.simulate import simulate
        from fast_sim.simulate_batch import _run_one

        scenario = json.loads(
            (ROOT / "units/t3-EXAMPLE-vectorized-matching/scenario.json").read_text()
        )
        # Exercise streaming directly; a one-second prefix uses buffered CLI storage.
        scenario["horizon_ns"] = 1_000_000_000
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "scenario.json"
            config.write_text(json.dumps(scenario))
            full = stream_native(
                spec_for(scenario),
                (root / "full/trace.parquet", root / "full/message_trace.parquet"),
            )
            lean = stream_native(
                spec_for(scenario), (root / "lean/trace.parquet", None)
            )
            full_hash = hashlib.sha256(
                (root / "full/trace.parquet").read_bytes()
            ).hexdigest()
            lean_hash = hashlib.sha256(
                (root / "lean/trace.parquet").read_bytes()
            ).hexdigest()
            self.assertEqual(lean_hash, full_hash)
            self.assertEqual([t.num_rows for t in lean], [t.num_rows for t in full])
            self.assertFalse((root / "lean/message_trace.parquet").exists())
            print(
                json.dumps(
                    {
                        "rankable": False,
                        "diagnostic": True,
                        "storage_mode": "streamed",
                        "optional_ledger": {
                            "full_bytes": sum(
                                p.stat().st_size
                                for p in (root / "full").glob("*.parquet")
                            ),
                            "lean_bytes": (root / "lean/trace.parquet").stat().st_size,
                            "n_events": lean[0].num_rows,
                            "n_messages": lean[1].num_rows,
                            "trace_sha256": lean_hash,
                        },
                    }
                ),
                flush=True,
            )
            scenario["horizon_ns"] = 10_000_000
            config.write_text(json.dumps(scenario))
            full_events = simulate(
                config, root / "cli-full/trace.parquet", require_message_ledger=True
            )
            lean_events = simulate(config, root / "cli-lean/trace.parquet")
            for key in ("trace_sha256", "n_events", "n_messages"):
                self.assertEqual(full_events[key], lean_events[key])
            self.assertNotIn("message_trace_sha256", lean_events)
            self.assertFalse((root / "cli-lean/message_trace.parquet").exists())
            _run_one((str(config), str(root / "batch/trace.parquet")))
            self.assertGreater(
                pq.ParquetFile(root / "batch/message_trace.parquet").metadata.num_rows,
                0,
            )
            scenario["scenario_family"] = "unknown-family"
            config.write_text(json.dumps(scenario))
            simulate(config, root / "unknown/trace.parquet")
            self.assertTrue((root / "unknown/message_trace.parquet").exists())

    def test_optional_ledger_counts_only_deliveries_across_stop_time(self):
        scenario = json.loads(
            (ROOT / "units/t3-mp01-stp-newest-baseline/scenario.json").read_text()
        )
        scenario["horizon_ns"] = 3_000_000_000
        # Latency exceeds the one-second post-close drain window. Exchange
        # computation also forces busy-recipient rescheduling of queued messages.
        scenario["latency_config"] = {
            "model": "uniform",
            "params": {"min_ns": 0, "max_ns": 2_000_000_000},
        }
        scenario["exchange_config"]["compute_delay_ns"] = 10_000_000
        for agent in scenario["agent_configs"]:
            agent["params"]["rebalance_interval_ns"] = 100_000_000

        for seed in (91571, 91572, 91573):
            with self.subTest(seed=seed), tempfile.TemporaryDirectory() as directory:
                scenario["seed"] = seed
                spec = spec_for(scenario)
                root = Path(directory)
                full = stream_native(
                    deepcopy(spec),
                    (root / "full/trace.parquet", root / "full/message_trace.parquet"),
                    chunk_rows=37,
                )
                lean = stream_native(
                    deepcopy(spec), (root / "lean/trace.parquet", None), chunk_rows=37
                )
                ledger = pd.read_parquet(root / "full/message_trace.parquet")
                self.assertGreater(full[0].num_rows, 0)
                self.assertEqual(full[1].num_rows, len(ledger))
                self.assertEqual(lean[1].num_rows, len(ledger))
                self.assertEqual(lean[0].num_rows, full[0].num_rows)
                self.assertTrue(
                    pq.read_table(root / "lean/trace.parquet").equals(
                        pq.read_table(root / "full/trace.parquet"), check_metadata=False
                    )
                )
                self.assertFalse((root / "lean/message_trace.parquet").exists())

                # Nominal receive times invert only after a busy recipient's
                # event is requeued; mode 3 must count its eventual delivery once.
                self.assertTrue((ledger.t_recv_ns.diff() < 0).any())
                self.assertFalse(ledger.duplicated(["message_id", "dst_id"]).any())
                broadcast = ledger[ledger.msg_type == "MarketClosePriceMsg"]
                self.assertGreater(broadcast.dst_id.nunique(), 1)
                self.assertEqual(broadcast.message_id.nunique(), 1)

                # Extend only the drain window to reveal messages already sent
                # by delivered parents but still pending when the short run ends.
                extended = deepcopy(spec)
                extended["stop_time"] += 4_000_000_000
                _, extended_messages = run_native_sim(extended)
                drained = as_pandas(extended_messages)
                pd.testing.assert_frame_equal(
                    ledger, drained.iloc[: len(ledger)].reset_index(drop=True)
                )
                delivered_keys = set(zip(ledger.message_id, ledger.dst_id))
                pending = drained[
                    (drained.t_send_ns <= spec["stop_time"])
                    & (drained.t_recv_ns > spec["stop_time"])
                    & drained.causal_parent.isin(ledger.message_id)
                ]
                pending = pending[
                    [
                        key not in delivered_keys
                        for key in zip(pending.message_id, pending.dst_id)
                    ]
                ]
                self.assertGreater(len(pending), 0)
                self.assertTrue((pending.msg_type == "MarketClosePriceMsg").any())
                self.assertLess(lean[1].num_rows, len(drained))

    def test_replay_count_mismatch_is_an_error(self):
        scenario = json.loads(
            (ROOT / "units/t3-s001-price-time-priority/scenario.json").read_text()
        )
        scenario["horizon_ns"] = 10_000_000
        spec = spec_for(scenario)
        partial, count = classify_native_executions(deepcopy(spec))

        class Sink:
            def write(self, table):
                pass

        with self.assertRaisesRegex(ValueError, "classification pass"):
            stream_native_sim(deepcopy(spec), partial, count + 1, Sink(), Sink())

    def test_optional_ledger_streaming_cli(self):
        from fast_sim.simulate import simulate

        scenario = json.loads(
            (ROOT / "units/t3-EXAMPLE-vectorized-matching/scenario.json").read_text()
        )
        # This prefix crosses the one-million estimated-placement dispatch threshold.
        scenario["horizon_ns"] = 3_000_000_000
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "scenario.json"
            config.write_text(json.dumps(scenario))
            full = simulate(
                config, root / "full/trace.parquet", require_message_ledger=True
            )
            lean = simulate(config, root / "lean/trace.parquet")
            for key in ("trace_sha256", "n_events", "n_messages"):
                self.assertEqual(full[key], lean[key])
            self.assertFalse((root / "lean/message_trace.parquet").exists())
            # Multiple row groups confirm that the CLI used bounded streaming.
            self.assertGreater(
                pq.ParquetFile(root / "lean/trace.parquet").num_row_groups, 2
            )
            print(
                json.dumps(
                    {
                        "rankable": False,
                        "diagnostic": True,
                        "horizon_ns": scenario["horizon_ns"],
                        "cli_optional_ledger": {
                            "full_bytes": sum(
                                p.stat().st_size
                                for p in (root / "full").glob("*.parquet")
                            ),
                            "lean_bytes": (root / "lean/trace.parquet").stat().st_size,
                            "n_events": lean["n_events"],
                            "n_messages": lean["n_messages"],
                            "trace_sha256": lean["trace_sha256"],
                        },
                    }
                ),
                flush=True,
            )

    def test_resource_failure_does_not_start_hybrid(self):
        from fast_sim.engine import run_scenario

        scenario = json.loads(
            (ROOT / "units/t3-s001-price-time-priority/scenario.json").read_text()
        )
        for error in (OSError("disk full"), MemoryError("allocation failed")):
            with self.subTest(error=type(error).__name__):
                with patch("fast_sim.native.run_native", side_effect=error), patch(
                    "fast_sim.engine.Kernel"
                ) as hybrid:
                    with self.assertRaises(type(error)):
                        run_scenario(scenario)
                    hybrid.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
