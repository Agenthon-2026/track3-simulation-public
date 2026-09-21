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
import json
import pathlib
import re
import shutil
import stat
import sys
import tempfile
import uuid
from collections.abc import Sequence

from qfbench2_common.dataset import AnswerLeak, split_unit

TRACK = "simulation"
HANDLE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def _location(path: pathlib.Path) -> pathlib.Path:
    """Reject links before resolving aliases such as '..'."""
    path = path.expanduser().absolute()
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError(f"symlink path is not allowed: {part}")
    return path.resolve()


def _regular_tree(root: pathlib.Path) -> None:
    """Metadata-only preflight; split_unit still owns copying and the answer leak gate."""
    pending = [root]
    while pending:
        path = pending.pop()
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            pending.extend(path.iterdir())
        elif not stat.S_ISREG(mode):
            raise ValueError(f"symlink or special node is not allowed: {path}")


def _selection(units: pathlib.Path, roster: Sequence[str] | None) -> list[pathlib.Path]:
    children = {p.name: p for p in units.iterdir()}
    handles = (sorted(name for name, p in children.items() if p.is_dir())
               if roster is None else list(roster))
    if not handles:
        raise ValueError(f"no units selected under {units}")
    seen: set[str] = set()
    selected = []
    for handle in handles:
        if not isinstance(handle, str) or not HANDLE.fullmatch(handle) or handle in (".", ".."):
            raise ValueError(f"invalid immediate unit directory handle: {handle!r}")
        if handle in seen:
            raise ValueError(f"duplicate unit directory handle: {handle}")
        seen.add(handle)
        unit = children.get(handle)
        if unit is None or unit.is_symlink() or not unit.is_dir():
            raise ValueError(f"selected unit is not an immediate real directory: {handle}")
        _regular_tree(unit)
        if not (unit / "card.toml").is_file():
            raise ValueError(f"selected unit has no card.toml: {handle}")
        selected.append(unit)
    return selected


def read_roster(path: pathlib.Path) -> list[str]:
    """One handle per line, with blank lines and full-line comments permitted."""
    path = _location(path)
    if not path.is_file():
        raise ValueError(f"roster is not a regular file: {path}")
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


def _publish(staging: pathlib.Path, out: pathlib.Path) -> None:
    """Keep the prior whole artifact until promotion succeeds; restore on ordinary failure."""
    backup = out.with_name(f".{out.name}.previous-{uuid.uuid4().hex}")
    if out.exists():
        out.rename(backup)
    try:
        staging.rename(out)
    except BaseException:
        if backup.exists():
            try:
                backup.rename(out)
            except OSError as exc:
                raise RuntimeError(f"promotion failed; prior artifact retained at {backup}") from exc
        raise
    if backup.exists():
        try:
            shutil.rmtree(backup)
        except OSError:
            print(f"previous artifact retained for cleanup: {backup}", file=sys.stderr)


def build(units: pathlib.Path, out: pathlib.Path, roster: Sequence[str] | None = None) -> int:
    units, out = _location(units), _location(out)
    if not units.is_dir():
        raise ValueError(f"units root is not a directory: {units}")
    if units == out or units in out.parents or out in units.parents:
        raise ValueError("units and output paths must not overlap")
    unit_dirs = _selection(units, roster)
    if out.exists():
        if not out.is_dir():
            raise ValueError(f"output is not a directory: {out}")
        _regular_tree(out)

    # Everything above is read-only. Preserve unrelated existing output metadata, then replace
    # both generated trees inside a sibling candidate. A late leak cannot leave a partial build.
    out.parent.mkdir(parents=True, exist_ok=True)
    staging = pathlib.Path(tempfile.mkdtemp(prefix=f".{out.name}.build-", dir=out.parent))
    total = 0
    try:
        if out.exists():
            shutil.copytree(out, staging, dirs_exist_ok=True, symlinks=True)
        ing_root = staging / "ingestion" / "input" / "ref"
        sco_root = staging / "scoring" / "input" / "ref"
        for root in (ing_root, sco_root):
            if root.exists():
                shutil.rmtree(root)
            root.mkdir(parents=True)
        for unit in unit_dirs:
            n = split_unit(unit, ing_root, sco_root, TRACK)
            total += n
            print(f"  {unit.name:<40} answers stripped: {n:>2}")
        if total == 0:
            raise ValueError(
                "zero answer paths stripped over the selected units; "
                "refusing a dataset with no grader answers"
            )
        (staging / "build-roster.json").write_text(
            json.dumps({"track": TRACK, "unit_handles": [u.name for u in unit_dirs]}, indent=2)
            + "\n", encoding="utf-8",
        )
        _regular_tree(staging)
        _publish(staging, out)
    finally:
        if staging.exists():
            shutil.rmtree(staging)

    ing_root = out / "ingestion" / "input" / "ref"
    sco_root = out / "scoring" / "input" / "ref"

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
    ap.add_argument("--roster", type=pathlib.Path,
                    help="ordered file of immediate unit directory handles, one per line")
    a = ap.parse_args()
    try:
        roster = read_roster(a.roster) if a.roster is not None else None
        return build(pathlib.Path(a.units), pathlib.Path(a.out), roster)
    except AnswerLeak as exc:
        ap.exit(1, f"LEAK GATE: {exc}\n")
    except (OSError, ValueError) as exc:
        ap.exit(1, f"BUILD REFUSED: {exc}\n")


if __name__ == "__main__":
    sys.exit(main())
