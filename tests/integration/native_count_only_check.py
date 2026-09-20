"""Optional buffered ledgers preserve all simulation results and delivery counts.

Run inside the rebuilt participant image with this repository at /workspace:
python /workspace/tests/integration/native_count_only_check.py
"""

from copy import deepcopy
import json
from pathlib import Path
import pickle
import tempfile
import unittest

import pyarrow.parquet as pq

from fast_sim._native import run_native_sim
from fast_sim.engine import run_scenario
from fast_sim.native import as_pandas, run_native_from_spec
from fast_sim.native_boot import spec_from_scenario
from fast_sim.simulate import simulate
from fast_sim.simulate_batch import _run_one
from fast_sim.streaming import UnstoredLedger


ROOT = Path(__file__).resolve().parents[2]
OPTIONAL_UNITS = (
    "t3-gb-base-30agent-30s",
    "t3-gb-highfreq-40hz-60s",
    "t3-gb-horizon-240s",
    "t3-gb-mega-throughput",
    "t3-gb-pop-128-agents",
    "t3-gb-pop-horizon-scale",
)


def scenario_for(unit):
    return json.loads((ROOT / "units" / unit / "scenario.json").read_text())


def rng_states(spec):
    """Serialize every input RNG state; omit the unrelated oracle object."""
    states = [row["random_state"].get_state() for row in spec["agents"]]
    states.append(spec["latency"]["random_state"].get_state())
    oracle = spec.get("oracle_spec")
    if oracle is not None:
        states += [oracle[key].get_state() for key in ("random_state", "global_rs")]
    return pickle.dumps(states)


class NativeCountOnlyTests(unittest.TestCase):
    def compare(self, scenario):
        full_spec = spec_from_scenario(deepcopy(scenario))
        lean_spec = deepcopy(full_spec)
        full_trace, full_ledger = run_native_sim(full_spec)
        lean_trace, count = run_native_sim(lean_spec, count_only=True)
        self.assertIsInstance(count, UnstoredLedger)
        self.assertEqual(count.num_rows, full_ledger.num_rows)
        self.assertTrue(lean_trace.equals(full_trace, check_metadata=True))
        self.assertEqual(rng_states(full_spec), rng_states(lean_spec))
        return full_trace, as_pandas(full_ledger), count

    def test_six_complete_optional_scenarios(self):
        for unit in OPTIONAL_UNITS:
            with self.subTest(unit=unit):
                trace, ledger, count = self.compare(scenario_for(unit))
                self.assertGreater(trace.num_rows, 0)
                self.assertGreater(count.num_rows, 0)
                self.assertEqual(count.num_rows, len(ledger))

    def test_stop_boundary_busy_requeue_and_shared_id_broadcast(self):
        scenario = scenario_for("t3-mp01-stp-newest-baseline")
        scenario["horizon_ns"] = 3_000_000_000
        scenario["latency_config"] = {
            "model": "uniform",
            "params": {"min_ns": 0, "max_ns": 2_000_000_000},
        }
        scenario["exchange_config"]["compute_delay_ns"] = 10_000_000
        for agent in scenario["agent_configs"]:
            agent["params"]["rebalance_interval_ns"] = 100_000_000

        for seed in (91571, 91572, 91573):
            with self.subTest(seed=seed):
                scenario["seed"] = seed
                _, ledger, count = self.compare(scenario)
                # Requeued deliveries retain their original receive timestamps.
                self.assertTrue((ledger.t_recv_ns.diff() < 0).any())
                self.assertFalse(ledger.duplicated(["message_id", "dst_id"]).any())
                broadcast = ledger[ledger.msg_type == "MarketClosePriceMsg"]
                self.assertGreater(broadcast.dst_id.nunique(), 1)
                self.assertEqual(broadcast.message_id.nunique(), 1)
                self.assertTrue((ledger.msg_type == "AGENT_WAKEUP").any())

                extended = spec_from_scenario(deepcopy(scenario))
                extended["stop_time"] += 4_000_000_000
                _, drained = run_native_sim(extended)
                # Extending only the drain window reveals pending messages that
                # neither complete-table nor count-only output may count early.
                self.assertLess(count.num_rows, drained.num_rows)

    def test_file_policy_keeps_required_batch_and_in_memory_ledgers(self):
        scenario = scenario_for(OPTIONAL_UNITS[0])
        scenario["horizon_ns"] = 100_000_000
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
            self.assertNotIn("message_trace_sha256", lean)
            self.assertFalse((root / "lean/message_trace.parquet").exists())

            # File path omission is an explicit request from the CLI. Ordinary
            # native / in-memory entrypoints still return materialized ledgers.
            _, counted, _ = run_native_from_spec(
                spec_from_scenario(deepcopy(scenario)),
                (root / "direct/trace.parquet", None),
            )
            self.assertIsInstance(counted, UnstoredLedger)
            self.assertEqual(counted.num_rows, full["n_messages"])
            memory_trace, memory_ledger, _ = run_scenario(deepcopy(scenario))
            self.assertNotIsInstance(memory_ledger, UnstoredLedger)
            self.assertEqual(memory_ledger.num_rows, full["n_messages"])
            self.assertTrue(
                memory_trace.equals(pq.read_table(root / "full/trace.parquet"))
            )

            _run_one((str(config), str(root / "batch/trace.parquet")))
            full_ledger = pq.read_table(root / "full/message_trace.parquet")
            self.assertTrue(
                full_ledger.equals(pq.read_table(root / "batch/message_trace.parquet"))
            )
            for family in ("unknown-family", "event-queue"):
                scenario["scenario_family"] = family
                config.write_text(json.dumps(scenario))
                simulate(config, root / family / "trace.parquet")
                self.assertTrue(
                    full_ledger.equals(
                        pq.read_table(root / family / "message_trace.parquet")
                    )
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
