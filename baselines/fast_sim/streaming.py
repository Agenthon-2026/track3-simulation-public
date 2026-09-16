"""Bound native output buffers while preserving integer and nullable schemas."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


@dataclass(frozen=True)
class StoredTable:
    path: Path
    num_rows: int


class UnstoredLedger:
    """Count actual kernel messages without persisting an optional ledger."""

    def __init__(self):
        self.num_rows = 0

    def write(self, table: pa.Table) -> None:
        self.num_rows += table.num_rows

    def close(self):
        return self


class ParquetSink:
    """Write independent row groups; never collect all simulation rows in RAM."""

    def __init__(self, path: Path, empty: pa.Table):
        from fast_sim.native import as_pandas

        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.num_rows = 0
        # The shared reader uses pandas metadata to restore nullable int64. Attach
        # the empty frame's metadata without converting every full chunk to pandas.
        if "t_send_ns" in empty.column_names:
            empty = pa.Table.from_pandas(as_pandas(empty), preserve_index=False)
        self.schema = empty.schema
        strings = [f.name for f in self.schema if pa.types.is_string(f.type)]
        integers = {
            f.name: "DELTA_BINARY_PACKED"
            for f in self.schema
            if pa.types.is_integer(f.type)
        }
        self.writer = pq.ParquetWriter(
            self.path,
            self.schema,
            compression="zstd",
            compression_level=3,
            use_dictionary=strings,
            column_encoding=integers,
        )

    def write(self, table: pa.Table) -> None:
        self.writer.write_table(table.replace_schema_metadata(self.schema.metadata))
        self.num_rows += table.num_rows

    def close(self) -> StoredTable:
        self.writer.close()
        return StoredTable(self.path, self.num_rows)
