#!/usr/bin/env python3
"""Build the local reference-trace cache the regression suite reads.

The reference traces ship inside each unit, at ``units/<slug>/``. ``run_regression.py``
looks them up by ``scenario_id`` under ``regression_suite/reference_traces/<uuid>/``.
This script bridges the two, so a fresh clone can run the documented quick start.

    python regression_suite/build_reference_cache.py

The cache is a build artifact, not source: it is gitignored, and it is derived entirely
from ``units/``. Regenerate it rather than editing it. Symlinks are used by default so
the cache costs no disk and cannot drift from the units it points at; ``--copy`` makes
real copies if your tooling cannot follow links.

Only the 65 single-scenario units are bridged. The six ``t3-gbatch-*`` units have a
different shape (``batch.json`` + ``checks/reference_data/sub_XX/``) and ``run_regression.py``
never resolves them through this path, so bridging them would be wrong as well as
useless. ``examples/t3-EXAMPLE-vectorized-matching`` has no regression scenario by design and is
not a roster unit.

Exit codes: 0 built (or already current), 1 refused, 2 inconsistent inputs.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
SCENARIOS = HERE / "scenarios"
UNITS = REPO / "units"
DEFAULT_CACHE = HERE / "reference_traces"

# Per-scenario reference payload. events.json is optional — not every scenario emits one.
# README.md:128 — scenarios exercising none of the ledger checks may omit message_trace.parquet.
REQUIRED = ("trace.parquet",)
OPTIONAL = ("message_trace.parquet", "events.json")

PARQUET_MAGIC = b"PAR1"


def scenario_index() -> dict[str, pathlib.Path]:
    """scenario_id -> its config file, over the real scenario configs only."""
    out: dict[str, pathlib.Path] = {}
    for cfg in sorted(SCENARIOS.glob("*.json")):
        if cfg.name == "index.json":
            continue
        try:
            data = json.loads(cfg.read_text())
        except json.JSONDecodeError as exc:
            sys.exit(f"error: {cfg} is not valid JSON ({exc})")
        sid = data.get("scenario_id") or data.get("id")
        if not sid:
            sys.exit(f"error: {cfg} declares no scenario_id")
        if sid in out:
            sys.exit(
                f"error: scenario_id {sid} declared by both {out[sid].name} and {cfg.name}"
            )
        out[sid] = cfg
    return out


def unit_index() -> dict[str, pathlib.Path]:
    """scenario_id -> unit dir, skipping batch units, which this path never resolves."""
    out: dict[str, pathlib.Path] = {}
    for unit in sorted(p for p in UNITS.iterdir() if p.is_dir()):
        if (unit / "batch.json").exists():
            continue  # t3-gbatch-*: different shape, resolved elsewhere
        for name in ("scenario.json", "config.json"):
            cfg = unit / name
            if not cfg.exists():
                continue
            try:
                data = json.loads(cfg.read_text())
            except json.JSONDecodeError:
                continue
            sid = data.get("scenario_id") or data.get("id")
            if sid:
                if sid in out:
                    sys.exit(
                        f"error: scenario_id {sid} claimed by both "
                        f"{out[sid].name} and {unit.name}"
                    )
                out[sid] = unit
            break
    return out


def is_lfs_stub(path: pathlib.Path) -> bool:
    """A .parquet that is really a 130-byte LFS pointer, i.e. `git lfs pull` was never run."""
    try:
        return path.stat().st_size < 1024 and path.open("rb").read(4) != PARQUET_MAGIC
    except OSError:
        return True


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--out",
        type=pathlib.Path,
        default=DEFAULT_CACHE,
        help=f"cache directory (default: {DEFAULT_CACHE.relative_to(REPO)})",
    )
    ap.add_argument(
        "--copy", action="store_true", help="copy files instead of symlinking"
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="rebuild even if the target exists and was not built by this script",
    )
    ap.add_argument(
        "--check",
        action="store_true",
        help="report what would be built and exit; write nothing",
    )
    ap.add_argument(
        "--reindex",
        action="store_true",
        help="regenerate scenarios/index.json from the scenario configs and exit",
    )
    args = ap.parse_args()

    scenarios = scenario_index()
    units = unit_index()

    if args.reindex:
        entries = []
        for sid, cfg in sorted(scenarios.items(), key=lambda kv: kv[1].name):
            data = json.loads(cfg.read_text())
            entries.append(
                {
                    "scenario_id": sid,
                    "config": cfg.name,
                    "name": data.get("name") or data.get("title") or cfg.stem,
                }
            )
        (SCENARIOS / "index.json").write_text(
            json.dumps(
                {
                    "_comment": (
                        "Generated from the scenario configs in this directory by "
                        "regression_suite/build_reference_cache.py --reindex. "
                        "run_regression.py does not read this file; it enumerates *.json and "
                        "skips index.json."
                    ),
                    "count": len(entries),
                    "scenarios": entries,
                },
                indent=2,
            )
            + "\n"
        )
        print(f"reindexed: {len(entries)} scenario(s) written to scenarios/index.json")
        return 0

    missing = sorted(set(scenarios) - set(units))
    if missing:
        print(
            f"error: {len(missing)} scenario_id(s) resolve to no unit; the cache would be "
            f"incomplete and run_regression would fail on them:",
            file=sys.stderr,
        )
        for sid in missing[:10]:
            print(f"  {sid}  ({scenarios[sid].name})", file=sys.stderr)
        return 2

    # Refuse to touch a directory this script did not create — scripts/build_public_units.py
    # reads this path as its own source, and clobbering it would couple unit generation to a
    # derived artifact.
    stamp = args.out / ".built-by-build_reference_cache"
    if (
        args.out.exists()
        and any(args.out.iterdir())
        and not stamp.exists()
        and not args.force
    ):
        print(
            f"refusing: {args.out} already exists and was not built by this script.\n"
            f"  Another tool may depend on its current contents "
            f"(scripts/build_public_units.py reads this path).\n"
            f"  Move it aside, or re-run with --force if you are certain.",
            file=sys.stderr,
        )
        return 1

    stubs, built = [], 0
    plan: list[tuple[str, pathlib.Path, list[pathlib.Path]]] = []
    for sid, unit in sorted(units.items()):
        if sid not in scenarios:
            continue  # a unit with no regression scenario (the EXAMPLE unit)
        payload = []
        for name in REQUIRED:
            src = unit / name
            if not src.exists():
                print(f"error: {unit.name} is missing {name}", file=sys.stderr)
                return 2
            payload.append(src)
        payload += [unit / n for n in OPTIONAL if (unit / n).exists()]
        # Stub-check every parquet we are about to link, required or not. Tying this to
        # REQUIRED would let a partial `git lfs pull` put a 131-byte pointer in the cache
        # for an optional ledger, which the g3.5 gate would then try to read as parquet.
        for src in payload:
            if src.suffix == ".parquet" and is_lfs_stub(src):
                stubs.append(f"{unit.name}/{src.name}")
        plan.append((sid, unit, payload))

    if stubs:
        print(
            f"error: {len(stubs)} reference file(s) are unfetched Git LFS pointers, not data.\n"
            f"  Run `git lfs pull` first, or the cache would contain 130-byte stubs.\n"
            f"  First few: {', '.join(stubs[:3])}",
            file=sys.stderr,
        )
        return 2

    if args.check:
        print(f"would build {len(plan)} scenario(s) into {args.out}")
        print(
            f"  {len(scenarios)} scenario configs, {len(units)} non-batch units, 0 unmapped"
        )
        return 0

    args.out.mkdir(parents=True, exist_ok=True)
    stamp.write_text(
        "Generated by regression_suite/build_reference_cache.py. Safe to delete.\n"
    )
    for sid, unit, payload in plan:
        dest = args.out / sid
        dest.mkdir(parents=True, exist_ok=True)
        for src in payload:
            target = dest / src.name
            if target.exists() or target.is_symlink():
                target.unlink()
            if args.copy:
                shutil.copy2(src, target)
            else:
                target.symlink_to(os.path.relpath(src, dest))
        built += 1

    n_batch = sum(
        1 for p in UNITS.iterdir() if p.is_dir() and (p / "batch.json").exists()
    )
    how = "copied" if args.copy else "symlinked"
    # relative_to() raises when --out is outside the repo, which is a legitimate use
    # (CI scratch, tmpfs, a cache shared across worktrees). Show a repo-relative path when
    # there is one, else the absolute path — never a ../../.. ladder.
    shown = (
        args.out
        if REPO not in args.out.resolve().parents
        else args.out.resolve().relative_to(REPO)
    )
    print(f"built {built} scenario reference(s) into {shown} ({how})")
    print(f"  {n_batch} batch unit(s) intentionally not bridged (different shape)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
