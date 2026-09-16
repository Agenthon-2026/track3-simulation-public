"""Check the public exemplar's local output without claiming reference equivalence.

There is no public reference for this one unit. Reuse the published integrity,
schema and resource gates, read every trace row in bounded batches, and report
semantic comparison as unknown. This helper is never a production scorer.
"""

from __future__ import annotations

import json
from pathlib import Path
import tomllib


EXEMPLAR = "t3-EXAMPLE-vectorized-matching"
ROOT = Path(__file__).resolve().parents[1]


def check_trace(output: Path) -> dict:
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    registry = json.loads((ROOT / "templates/trace_column_registry.json").read_text())
    schema = pa.schema(
        [(c["name"], pa.type_for_alias(c["dtype"])) for c in registry["columns"]]
    )
    parquet = pq.ParquetFile(output / "trace.parquet")
    if not parquet.schema_arrow.equals(schema, check_metadata=False):
        raise ValueError("trace schema differs from the public registry")
    rows = 0
    previous = None
    # Limit Arrow to one row group at a time; long traces must not queue all groups.
    batches = (
        batch
        for group in range(parquet.num_row_groups)
        for batch in parquet.iter_batches(
            batch_size=262144, row_groups=[group], use_threads=False
        )
    )
    for batch in batches:
        for name in schema.names:
            if name != "side" and batch.column(name).null_count:
                raise ValueError("null in required column: " + name)
        if not pc.all(
            pc.is_in(
                batch.column("msg_type"), value_set=pa.array(registry["msg_types"])
            )
        ).as_py():
            raise ValueError("unknown event type")
        if not pc.all(
            pc.is_in(
                batch.column("side"),
                value_set=pa.array(registry["sides"], type=pa.string()),
            )
        ).as_py():
            raise ValueError("unknown side")
        for name in ("t_ns", "agent_id", "price", "size"):
            if pc.min(batch.column(name)).as_py() < 0:
                raise ValueError("negative value: " + name)
        if pc.min(batch.column("order_id")).as_py() < -1:
            raise ValueError("invalid order_id sentinel")
        times = batch.column("t_ns")
        if previous is not None and times[0].as_py() < previous:
            raise ValueError("timestamps decrease across batches")
        if (
            len(times) > 1
            and pc.any(pc.less(times.slice(1), times.slice(0, len(times) - 1))).as_py()
        ):
            raise ValueError("timestamps decrease within a batch")
        previous = times[-1].as_py()
        rows += batch.num_rows
    events = json.loads((output / "events.json").read_text())
    if rows <= 0 or rows != parquet.metadata.num_rows or rows != events["n_events"]:
        raise ValueError("decoded, footer and reported event counts disagree")
    return {
        "passed": True,
        "decoded_rows": rows,
        "schema": "trace_column_registry.json",
    }


def verify(unit: Path, output: Path) -> dict:
    from qfbench2_track_simulation import scoring

    if unit.name != EXEMPLAR or (unit / "trace.parquet").exists():
        raise ValueError("reference-free checks apply only to the public exemplar")
    card = tomllib.loads((unit / "card.toml").read_text())
    ctx = {
        "unit_dir": unit,
        "output_dir": output,
        "card": card,
        "split": card["task"]["split"],
    }
    scoring.build_developer_verifier(ctx)  # Select the existing developer profile.
    gates = {}
    for name, gate in scoring._GATES:
        if name == "g3_domain_semantics":
            break
        result = gate(ctx)
        gates[name] = {"passed": result.passed, "detail": result.detail}
        if not result.passed:
            break
    local = {"passed": False}
    if len(gates) == 3 and all(g["passed"] for g in gates.values()):
        try:
            local = check_trace(output)
        except Exception as exc:
            local = {"passed": False, "reason": str(exc)}
    gates["g3_domain_semantics"] = {
        "passed": None,
        "detail": {"status": "unknown", "reason": "no public reference"},
    }
    return {
        "admissible": local["passed"]
        and all(g["passed"] for k, g in gates.items() if k != "g3_domain_semantics"),
        "gates": gates,
        "local_trace_checks": local,
        "semantic_status": "unknown",
        "rankable": False,
    }
