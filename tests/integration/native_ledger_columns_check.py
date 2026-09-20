"""Exercise the compiled ledger columns against independently constructed tables.

Run with Cython, a C compiler, NumPy and PyArrow installed:
python tests/integration/native_ledger_columns_check.py
The test extension copies the production ledger class into a temporary module;
the participant extension is neither modified nor given a test-only API.
"""

from pathlib import Path
import importlib.util
import subprocess
import sys
import tempfile
import unittest

import numpy as np
import pyarrow as pa


ROOT = Path(__file__).resolve().parents[2]
NAMES = (
    "AGENT_WAKEUP",
    "OrderExecutedMsg",
    "OrderAcceptedMsg",
    "OrderCancelledMsg",
    "LimitOrderMsg",
    "CancelOrderMsg",
    "QuerySpreadMsg",
    "QuerySpreadResponseMsg",
    "MarketOrderMsg",
    "MarketHoursRequestMsg",
    "MarketClosePriceRequestMsg",
    "MarketHoursMsg",
    "MarketClosePriceMsg",
    "MarketClosedMsg",
)
SCHEMA = pa.schema(
    [
        ("seq", pa.int64()),
        ("t_recv_ns", pa.int64()),
        ("t_send_ns", pa.int64()),
        ("latency_ns", pa.int64()),
        ("src_id", pa.int32()),
        ("dst_id", pa.int32()),
        ("message_id", pa.int64()),
        ("msg_type", pa.string()),
        ("order_id", pa.int64()),
        ("causal_parent", pa.int64()),
    ]
)


def compile_ledger(directory):
    source = (ROOT / "baselines/fast_sim/_native.pyx").read_text()
    prefix = source[: source.index("cdef inline long long _round_i64")]
    ledger = source[
        source.index("cdef class CLedger:") : source.index("cdef class CPriceLevel:")
    ]
    fixture = """
def ledger_from_rows(rows):
    cdef CLedger ledger = CLedger()
    for row in rows:
        ledger.append_c(
            row[0], row[1], row[2], row[3], row[4], row[5],
            row[6], row[7], row[8], row[9], row[10],
        )
    return ledger
"""
    (directory / "ledger_columns.pyx").write_text(prefix + ledger + fixture)
    (directory / "setup.py").write_text(
        "from setuptools import setup\n"
        "from Cython.Build import cythonize\n"
        "setup(ext_modules=cythonize('ledger_columns.pyx', quiet=True))\n"
    )
    completed = subprocess.run(
        [sys.executable, "setup.py", "build_ext", "--inplace"],
        cwd=directory,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        raise RuntimeError(completed.stdout + completed.stderr)
    library = next(directory.glob("ledger_columns*.so"))
    spec = importlib.util.spec_from_file_location("ledger_columns", library)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def expected_table(rows):
    values = []
    for row in sorted((row for row in rows if row[9] >= 0), key=lambda row: row[9]):
        mid, src, dst, sent, received, latency, code, oid, parent, seq, flags = row
        values.append(
            {
                "seq": seq,
                "t_recv_ns": received,
                "t_send_ns": None if flags & 1 else sent,
                "latency_ns": latency,
                "src_id": src,
                "dst_id": dst,
                "message_id": mid,
                "msg_type": NAMES[code] if 0 <= code < len(NAMES) else "AGENT_WAKEUP",
                "order_id": None if flags & 2 else oid,
                "causal_parent": None if flags & 4 else parent,
            }
        )
    return pa.Table.from_pylist(values, schema=SCHEMA)


def row(index=0, *, seq=0, code=0, flags=0):
    return (
        index,
        -17,
        2147483647,
        -(2**63),
        2**63 - 1,
        987654321,
        code,
        2**63 - 2,
        -(2**63) + 1,
        seq,
        flags,
    )


class LedgerColumnTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scratch = tempfile.TemporaryDirectory(prefix="ledger-columns-")
        cls.native = compile_ledger(Path(cls.scratch.name))

    @classmethod
    def tearDownClass(cls):
        cls.scratch.cleanup()

    def check_rows(self, rows):
        ledger = self.native.ledger_from_rows(rows)
        expected = expected_table(rows)
        actual = ledger.to_arrow()
        self.assertTrue(actual.equals(expected, check_metadata=True))
        # Materialization does not consume or mutate ledger state.
        self.assertTrue(ledger.to_arrow().equals(expected, check_metadata=True))
        arrays = ledger.to_arrays()
        if expected.num_rows:
            for key in ("t_send_na", "oid_na", "parent_na"):
                self.assertEqual(arrays[key].dtype, np.dtype("bool"))
                self.assertTrue(np.isin(arrays[key].view(np.uint8), [0, 1]).all())
        else:
            self.assertIsNone(arrays)
        return actual

    def test_empty_and_all_undelivered(self):
        self.check_rows([])
        self.check_rows([row(seq=-1), row(1, seq=-77, flags=7)])

    def test_null_flags_types_integer_limits_and_stable_ties(self):
        rows = [row(i, seq=(31 - i) // 3, code=i - 2, flags=i % 8) for i in range(32)]
        rows.insert(4, row(999, seq=-1))
        self.check_rows(rows)

    def test_multiple_buffer_growths_and_random_row_order(self):
        rng = np.random.RandomState(417)
        rows = [
            row(
                i,
                seq=int(rng.randint(-10, 100)),
                code=int(rng.randint(-4, 20)),
                flags=int(rng.randint(0, 256)),
            )
            for i in range(4097)
        ]
        self.check_rows(rows)


if __name__ == "__main__":
    unittest.main()
