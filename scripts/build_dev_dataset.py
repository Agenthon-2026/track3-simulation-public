#!/usr/bin/env python3
"""Build the Track 3 DEV-phase CodaBench dataset from the public units.

Produces TWO trees, because `ingest.py` bind-mounts each unit directory wholesale at /input:

    <out>/ingestion/input/ref/<unit>/   task setup only; mounted into the submission
    <out>/scoring/input/ref/<unit>/     complete unit, answers included; grader only

Track 3 is the track where this bites hardest: a unit stores its answer FLAT, beside its inputs --
`trace.parquet`, `events.json` and `message_trace.parquet` sit next to `scenario.json`, and batched
units keep per-sub copies under `checks/reference_data/`. Uploading `units/` verbatim as the
phase input_data hands every participant the exact trace the scorer grades against.

The declarations and the leak gate live in the hub (`qfbench2_common.dataset`) so T1, T2 and T4
inherit them rather than each re-deriving what an answer looks like. This script is the Track 3
caller; the mechanism is shared.
"""
from __future__ import annotations

import argparse
import pathlib
import shutil
import sys

from qfbench2_common.dataset import AnswerLeak, split_unit

TRACK = "simulation"


def build(units: pathlib.Path, out: pathlib.Path) -> int:
    ing_root = out / "ingestion" / "input" / "ref"
    sco_root = out / "scoring" / "input" / "ref"
    for r in (ing_root, sco_root):
        if r.exists():
            shutil.rmtree(r)
        r.mkdir(parents=True)

    unit_dirs = sorted(p for p in units.iterdir() if p.is_dir())
    if not unit_dirs:
        sys.exit(f"no units under {units}")

    total = 0
    for u in unit_dirs:
        try:
            n = split_unit(u, ing_root, sco_root, TRACK)
        except AnswerLeak as exc:
            sys.exit(f"LEAK GATE: {exc}")
        total += n
        print(f"  {u.name:<40} answers stripped: {n:>2}")

    print(f"\ningestion tree: {ing_root.parent}   ({len(unit_dirs)} units, submission-facing)")
    print(f"scoring tree:   {sco_root}   ({len(unit_dirs)} units, grader only)")
    print(f"answer paths stripped: {total}")
    print("\nUpload the INGESTION tree as the phase input_data and the SCORING tree as its")
    print("reference_data. Swapping them hands every participant the answers.")
    print("This splitter does not create the signed evaluation plan or its verification trust store;")
    print("prepare that required reference-dataset metadata before uploading.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    here = pathlib.Path(__file__).resolve().parents[1]
    ap.add_argument("--units", default=str(here / "units"))
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    return build(pathlib.Path(a.units), pathlib.Path(a.out))


if __name__ == "__main__":
    sys.exit(main())
