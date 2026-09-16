"""LOCAL DEVELOPER harness for timing one Track 3 unit. It cannot produce an official number.

## Executive summary (read this first)

This module used to describe itself as the "worker-side authoritative runner", and the description
was the problem: a Track 3 launcher standing in for the production timing path. The Runner owns
participant launch, lifecycle, timing, C2 and C3 on the platform (global rule 3); Track 3 owns the
semantic rules, the telemetry requirements, and this clearly non-rankable developer harness.

**Everything this module writes is stamped ``rankable = False`` and ``profile = "developer"``.** The
production scorer factory reads the C2 run record and has no path that reads a
``host_metrics.json`` written here. Use it to time your own image locally; it will not tell you
where you would rank.

Three bounds, because a practice run still executes an untrusted image on somebody's workstation:
a hard deadline and capped log capture (see ``throughput.timer.bounded_container_run``), a
C3-compatible **no-follow sanitizing** copy for retained output, and no re-invocation of the
participant image for any reason.

What it measures, when it can:

* **events/sec is host-measured on both sides of the fraction**: the event count is the row count of
  the emitted ``trace.parquet`` (read from the parquet footer), and the wall clock is this process's.
  Neither number comes from the submission. The count the submission declares in its own
  ``events.json`` is read only to cross-check, and a mismatch fails the run. Repeated ``--runs``
  times with the first discarded as warm-up, reported as the median.
* **host telemetry** (`host_gpu_seconds` via NVML, `host_peak_memory_bytes` via the container
  cgroup) rides along, so the telemetry-dependent awards rank on measured values (WS-1).
* **the node fingerprint** is recorded with every timed run, which the T3 fairness rule requires
  (hub ``docs/30-EVAL-INFRA.md`` §4): a fingerprint change invalidates cross-run comparability.
* **both verbs** are supported. A batch unit (``batch.json`` + ``scenarios/``) is run with
  ``simulate-batch``; the shared ingestion program only knows the single-scenario ``simulate``.

Only the scenario inputs are mounted, never a unit's reference answers, matching what the sealed
ingestion tree contains.

Usage::

    python -m throughput.run_unit --image <img> --unit units/t3-fastlob-core --out record.json
    python -m throughput.run_unit --image <img> --unit units/t3-gbatch-dense-3 --runs 3
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import threading
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import node_fingerprint
from .timer import DEFAULT_RUN_TIMEOUT_SEC, gpu_docker_args, timed_container_run


# Local reception adds no byte ceiling below the bounded scratch filesystem.
# The official Track 3 output cap is unpublished; this is a developer assumption,
# not an override of the production Runner's C3 policy.
LOCAL_DISK_BYTES = 10 * 1024**3


def retention_byte_limits(byte_limit: int | None = None) -> dict[str, int]:
    """Allow any output that fits the declared local runtime disk to be retained."""
    cap = LOCAL_DISK_BYTES if byte_limit is None else byte_limit
    if not isinstance(cap, int) or cap <= 0:
        raise ValueError("local output byte limit must be positive")
    return {"max_file_bytes": cap, "max_total_bytes": cap}


@dataclass
class UnitRun:
    """One timed invocation of a unit."""

    events_per_sec: float
    n_events: int  # host-counted from the emitted trace; numerator of the local rate
    reported_n_events: int  # what the submission declared; kept for the audit trail
    host_wall_clock_sec: float
    host_gpu_seconds: float | None
    host_peak_memory_bytes: int | None
    returncode: int
    host_peak_disk_bytes: int | None = None
    disk_capacity_bytes: int | None = None


class UnitExecutionError(RuntimeError):
    """A failed invocation with its host timing and resource evidence preserved."""

    def __init__(self, message: str, measurement: UnitRun) -> None:
        super().__init__(message)
        self.measurement = measurement


@dataclass
class UnitRecord:
    """A local developer measurement; never consumed by the production scorer."""

    unit: str
    verb: str
    image: str
    runs: list[UnitRun] = field(default_factory=list)
    warmup_discarded: bool = True
    median_events_per_sec: float | None = None
    median_host_gpu_seconds: float | None = None
    median_host_peak_memory_bytes: int | None = None
    node_fingerprint: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit": self.unit,
            "verb": self.verb,
            "image": self.image,
            "warmup_discarded": self.warmup_discarded,
            "median_events_per_sec": self.median_events_per_sec,
            "median_host_gpu_seconds": self.median_host_gpu_seconds,
            "median_host_peak_memory_bytes": self.median_host_peak_memory_bytes,
            "runs": [r.__dict__ for r in self.runs],
            "node_fingerprint": self.node_fingerprint,
            # The LOCAL harness measured these. They are not an official timing: no signature, no
            # C7 binding, no UUID-resolved GPU attribution, no coverage accounting, and one
            # retained output tree rather than one per repeat. Stamped rather than implied, because
            # the previous version of this record called itself "authoritative" and the scorer
            # believed it.
            "telemetry_source": "local_harness",
            "retention_byte_limits": retention_byte_limits(),
            "profile": "developer",
            "rankable": False,
        }


def is_batch_unit(unit_dir: Path) -> bool:
    """A batch unit ships ``batch.json`` plus a ``scenarios/`` directory of sub-scenarios."""
    return (unit_dir / "batch.json").exists() and (unit_dir / "scenarios").is_dir()


def _stage_input(unit_dir: Path, staging: Path, batch: bool) -> None:
    """Copy ONLY solver-facing inputs into the staging dir. Reference answers are never mounted."""
    if batch:
        shutil.copytree(unit_dir / "scenarios", staging / "scenarios")
    else:
        shutil.copy(unit_dir / "scenario.json", staging / "scenario.json")


def _reported_n_events(out_dir: Path, batch: bool) -> int:
    """The event count the SUBMISSION declares, from its own sidecar. Recorded for the audit trail
    and cross-checked against the host count; never used as the numerator of the ranked rate."""
    if batch:
        data = json.loads((out_dir / "batch_events.json").read_text())
        return int(data.get("total_events", data.get("n_events", 0)))
    return int(json.loads((out_dir / "events.json").read_text())["n_events"])


def _host_n_events(out_dir: Path, batch: bool) -> int:
    """The event count measured HOST-side, as the row count of the emitted trace parquet.

    This is the numerator of the ranked rate. Reading it from the submission's ``events.json`` would
    let a submission inflate events/sec simply by over-declaring ``n_events``: the wall clock would be
    ours but the numerator would be theirs. The trace is the artifact the semantic gates score, so its
    row count is the only event count that cannot be inflated without also failing those gates.

    Row counts come from the parquet footer, so nothing is deserialized.
    """
    import pyarrow.parquet as pq  # lazy: only the worker path needs it

    if batch:
        total = 0
        for sub in sorted(p for p in out_dir.iterdir() if p.is_dir()):
            trace = sub / "trace.parquet"
            if trace.exists():
                total += int(pq.ParquetFile(trace).metadata.num_rows)
        return total
    return int(pq.ParquetFile(out_dir / "trace.parquet").metadata.num_rows)


def retain_output(
    out_dir: Path, destination: Path, unit_dir: Path, *, byte_limit: int | None = None
) -> None:
    """Copy the run's output into ``destination`` through the shared C3 no-follow primitives.

    Two measured defects are closed here.

    ``shutil.copytree(out_dir, keep_output)`` used the default ``symlinks=False``, so a participant
    symlink was **flattened and its target's bytes copied into the retained tree** -- verified
    locally to walk into ``/etc`` and reach ``master.passwd`` before permission errors stopped it.
    A symlink to a directory made copytree recurse into it; a dangling one made copytree raise,
    turning a successful timed run into a harness exception. That is raw participant output copied
    with no sanitation at all.

    The replacement is ``qfbench2_common.sanitize``: a directory-FD-relative no-follow walk that
    refuses symlinks, hard links, FIFOs, sockets, devices and every other non-regular node,
    enforces Track 3's exact allowed relative paths (``qfbench2_track_simulation.limits``), copies
    validated bytes into a fresh directory and hashes the COPIES, then promotes atomically.

    ``materialize_tree`` rather than ``sanitize_participant_tree``, deliberately: the latter also
    emits a C3 descriptor and therefore requires the destination's parent to contain nothing else.
    A local run keeps one directory per unit under a shared run root -- that is where the
    developer ``host_metrics.json`` sits -- so the parent is never exclusive and the descriptor
    would be a false claim. C3 is the Runner's to produce; what the local harness needs is the
    no-follow policy, and it takes it from the same implementation rather than a second one.

    Imported lazily so the argv and routing tests, which run in the secret-free CI job, do not need
    the toolkit.
    """
    from qfbench2_common.contracts.artifact_tree import TreeLimits
    from qfbench2_common.sanitize import (
        TreeRefused,
        materialize_tree,
        promote,
        staging_sibling,
        verify_destination,
    )

    from qfbench2_track_simulation.limits import allowed_paths_for

    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = staging_sibling(destination)
    limits = TreeLimits(**retention_byte_limits(byte_limit))
    try:
        result = materialize_tree(
            out_dir, staging, allowed_paths=allowed_paths_for(unit_dir), limits=limits
        )
        if result.unsafe_modes:
            raise TreeRefused(
                f"{result.unsafe_modes} output file(s) carry setuid/setgid/sticky mode bits"
            )
        if result.rejections:
            raise TreeRefused(
                "output tree refused: "
                + ", ".join(
                    f"{code.value}x{count}"
                    for code, count in sorted(result.rejections.items())
                ),
                result.rejections,
            )
        if not result.files:
            raise TreeRefused("the run produced no accepted output files")
        errors = verify_destination(staging, result.files, limits=limits)
        if errors:
            raise TreeRefused("the copied tree did not verify: " + "; ".join(errors))
        promote(staging, destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def run_once(
    image: str,
    unit_dir: Path,
    *,
    batch: bool,
    cpus: str = "4",
    memory: str = "16g",
    gpus: str | None = None,
    keep_output: Path | None = None,
    timeout_sec: float = DEFAULT_RUN_TIMEOUT_SEC,
    run_as_host_user: bool = False,
    scratch_root: Path | None = None,
    bounded_disk: bool = False,
    disk_limit_bytes: int = LOCAL_DISK_BYTES,
    log_dir: Path | None = None,
) -> UnitRun:
    """One timed, DEADLINE-BOUNDED container invocation of the unit.

    ``run_as_host_user`` adds ``--user <uid>:<gid>`` so the container's output is owned by the
    harness user and can simply be deleted afterwards. It is opt-in because some images expect
    root; when it is off, cleanup stays best-effort (``ignore_cleanup_errors=True``) and at worst
    leaves a root-owned temp directory behind. What it must never do -- and what the deleted
    ``_reclaim_output`` did -- is run the PARTICIPANT'S IMAGE again to chown the tree.
    """
    capacity = None
    if bounded_disk:
        if scratch_root is None or os.name != "posix":
            raise ValueError("bounded disk requires a dedicated Linux scratch mount")
        fs = os.statvfs(str(scratch_root))
        capacity = fs.f_blocks * fs.f_frsize
        if not 0 < capacity <= disk_limit_bytes:
            raise ValueError("scratch filesystem exceeds the declared disk byte limit")
    with (
        tempfile.TemporaryDirectory(prefix="t3_in_", dir=scratch_root) as in_tmp,
        tempfile.TemporaryDirectory(
            prefix="t3_out_", dir=scratch_root, ignore_cleanup_errors=True
        ) as out_tmp,
        tempfile.TemporaryDirectory(prefix="t3_cid_", dir=scratch_root) as cid_tmp,
    ):
        in_dir, out_dir = Path(in_tmp), Path(out_tmp)
        _stage_input(unit_dir, in_dir, batch)
        cidfile = Path(cid_tmp) / "container.cid"

        temp_dir = in_dir / "tmp"
        temp_dir.mkdir()

        verb_args = (
            [
                "simulate-batch",
                "--batch-dir",
                "/input/scenarios",
                "--out-dir",
                "/output",
            ]
            if batch
            else [
                "simulate",
                "--config",
                "/input/scenario.json",
                "--out",
                "/output/trace.parquet",
            ]
        )
        cmd = [
            "docker",
            "run",
            "--rm",
            "--cidfile",
            str(cidfile),
            "--network=none",
            f"--cpus={cpus}",
            f"--memory={memory}",
            *gpu_docker_args(gpus),
            "-v",
            f"{in_dir}:/input:ro",
            "-v",
            f"{out_dir}:/output",
        ]
        if bounded_disk:
            cmd += ["--read-only", "-v", f"{temp_dir}:/tmp"]
        if run_as_host_user and os.name == "posix":
            cmd += ["--user", f"{os.getuid()}:{os.getgid()}"]
        cmd += [image, *verb_args]

        disk_peak = [0]
        stop = threading.Event()

        def sample_disk() -> None:
            while not stop.is_set():
                fs = os.statvfs(str(scratch_root))
                disk_peak[0] = max(
                    disk_peak[0], (fs.f_blocks - fs.f_bfree) * fs.f_frsize
                )
                stop.wait(0.05)

        sampler = (
            threading.Thread(target=sample_disk, daemon=True) if bounded_disk else None
        )
        if sampler:
            sampler.start()
        try:
            proc, wall_clock, gpu_s, peak_mem = timed_container_run(
                cmd, cidfile=cidfile, gpus=gpus, timeout_sec=timeout_sec
            )
        finally:
            stop.set()
            if sampler:
                sampler.join(timeout=2)
        if bounded_disk:
            fs = os.statvfs(str(scratch_root))
            disk_peak[0] = max(disk_peak[0], (fs.f_blocks - fs.f_bfree) * fs.f_frsize)
        if log_dir is not None:
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / "stdout.log").write_bytes(proc.stdout)
            (log_dir / "stderr.log").write_bytes(proc.stderr)
            # Durable before retention or any Parquet parser: SIGKILL cannot be caught.
            with (log_dir / "execution.json").open("x") as handle:
                json.dump(
                    {
                        "image": image,
                        "unit": unit_dir.name,
                        "rankable": False,
                        "returncode": proc.returncode,
                        "host_wall_clock_sec": wall_clock,
                        "host_gpu_seconds": gpu_s,
                        "host_peak_memory_bytes": peak_mem,
                        "host_peak_disk_bytes": disk_peak[0] if bounded_disk else None,
                        "disk_capacity_bytes": capacity,
                    },
                    handle,
                    allow_nan=False,
                )
                handle.flush()
                os.fsync(handle.fileno())
        if proc.returncode != 0:
            from qfbench2_common.sanitize import TreeRefused

            tail = proc.stderr[-2000:].decode("utf-8", errors="replace")
            if keep_output is not None and any(out_dir.iterdir()):
                try:
                    retain_output(
                        out_dir, keep_output, unit_dir, byte_limit=disk_limit_bytes
                    )
                except (OSError, ValueError, TreeRefused) as exc:
                    if log_dir is not None:
                        (log_dir / "retention-error.log").write_text(str(exc))
            raise UnitExecutionError(
                f"Container exited with code {proc.returncode} on {unit_dir.name} "
                f"(a negative code means the {timeout_sec:g}s deadline fired and the container "
                f"was killed).\n{tail}",
                UnitRun(
                    0.0,
                    0,
                    0,
                    wall_clock,
                    gpu_s,
                    peak_mem,
                    proc.returncode,
                    disk_peak[0] if bounded_disk else None,
                    capacity,
                ),
            )

        # Both halves of the local rate are the harness's: the event count comes from the emitted
        # trace, the wall clock from this process. A submission that over-declares n_events in its
        # own events.json is rejected here rather than being rewarded with a higher rate. This is
        # still a DEVELOPER measurement -- the official one is the Runner's, in C2.
        n_events = _host_n_events(out_dir, batch)
        reported = _reported_n_events(out_dir, batch)
        if reported != n_events:
            raise RuntimeError(
                f"n_events mismatch on {unit_dir.name}: submission declared {reported}, "
                f"trace contains {n_events}. The declared count must match the emitted trace."
            )

        if keep_output is not None:
            retain_output(out_dir, keep_output, unit_dir, byte_limit=disk_limit_bytes)
        eps = n_events / wall_clock if wall_clock > 0 else 0.0
        return UnitRun(
            eps,
            n_events,
            reported,
            wall_clock,
            gpu_s,
            peak_mem,
            proc.returncode,
            disk_peak[0] if bounded_disk else None,
            capacity,
        )


def run_unit(
    image: str,
    unit_dir: Path,
    *,
    runs: int = 5,
    discard_warmup: bool = True,
    cpus: str = "4",
    memory: str = "16g",
    gpus: str | None = None,
    keep_output: Path | None = None,
    timeout_sec: float = DEFAULT_RUN_TIMEOUT_SEC,
    run_as_host_user: bool = False,
) -> UnitRecord:
    """Time one unit ``runs`` times and return its DEVELOPER record (``rankable = False``)."""
    if discard_warmup and runs < 2:
        raise ValueError("runs must be >= 2 when discard_warmup=True")
    batch = is_batch_unit(unit_dir)
    record = UnitRecord(
        unit=unit_dir.name,
        verb="simulate-batch" if batch else "simulate",
        image=image,
        warmup_discarded=discard_warmup,
        node_fingerprint=node_fingerprint.collect().to_dict(),
    )
    for i in range(runs):
        record.runs.append(
            run_once(
                image,
                unit_dir,
                batch=batch,
                cpus=cpus,
                memory=memory,
                gpus=gpus,
                timeout_sec=timeout_sec,
                run_as_host_user=run_as_host_user,
                # Keep the last run's output so a local gate run has something to read. On the
                # OFFICIAL path every repeat is evidenced: C2 carries a per-repeat record with its
                # own output_tree_digest and event_count, and the scorer refuses a submission whose
                # repeats disagree with the tree that was scored. That is what closes alternating
                # fast-invalid / slow-valid repeats, and it is not something a local harness that
                # keeps one tree can do.
                keep_output=keep_output if i == runs - 1 else None,
            )
        )

    scored = record.runs[1:] if discard_warmup else record.runs
    if scored:
        record.median_events_per_sec = statistics.median(
            r.events_per_sec for r in scored
        )
        gpu = [r.host_gpu_seconds for r in scored if r.host_gpu_seconds is not None]
        mem = [
            r.host_peak_memory_bytes
            for r in scored
            if r.host_peak_memory_bytes is not None
        ]
        record.median_host_gpu_seconds = statistics.median(gpu) if gpu else None
        record.median_host_peak_memory_bytes = (
            int(statistics.median(mem)) if mem else None
        )
    return record


#: Name of the worker -> scorer handoff file. Must match ``qfbench2_track_simulation.host_metrics``,
#: which reads it from the PARENT of each per-unit output directory. This module is deliberately
#: stdlib-only (importing the track package would pull in pandas), so the two agree by a test rather
#: than by a shared import: see ``tests/test_run_unit.py``.
HOST_METRICS_FILENAME = "host_metrics.json"


def merge_host_metrics(record: UnitRecord, path: Path) -> dict[str, Any]:
    """Merge one unit's measured telemetry into the shared ``host_metrics.json`` map and return it.

    This file is the handoff from the worker (which measures) to the scorer (which consumes): it maps
    unit name to the ``host_*`` fields `throughput.report.build_report(host_metrics=...)` expects, so
    the diagnostics and the telemetry-dependent awards rank on measured values instead of falling
    back to the submission's self-report. Merging (rather than overwriting) lets each unit be timed
    independently, in any order, and appended as it finishes.
    """
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            existing = {}
    # Every statistic here is built from the SCORED runs — the same population the ranked median
    # comes from. Mixing warm-up-inclusive and warm-up-exclusive numbers in one map would let the
    # diagnostics describe a different run set than the leaderboard does; at the minimum permitted
    # runs=2 with discard_warmup that divergence is the whole warm-up.
    scored = record.runs[1:] if record.warmup_discarded else record.runs
    existing[record.unit] = {
        # The RANKED quantity: the median of the per-run rates over the scored runs, each rate
        # being the harness's event count over the harness wall clock. The scorer reads this
        # directly rather than re-deriving it, so the number on the leaderboard is the number the
        # timing protocol defines (a median of ratios, which is not the ratio of the medians below).
        "host_events_per_sec": record.median_events_per_sec,
        "host_wall_clock_sec": (
            statistics.median(r.host_wall_clock_sec for r in scored) if scored else None
        ),
        # The harness's own event count, so the scorer recomputes the rate from a numerator it
        # measured rather than the one the submission declared. Runs are deterministic, so the
        # median is the count; a divergence would already have failed the run.
        "host_n_events": (
            int(statistics.median(r.n_events for r in scored)) if scored else None
        ),
        "host_gpu_seconds": record.median_host_gpu_seconds,
        "host_peak_memory_bytes": record.median_host_peak_memory_bytes,
        # Which box produced these numbers. The T3 fairness rule requires every timed run to happen
        # on the same pinned instance, and carrying the fingerprint into the file the scorer trusts
        # is what lets a wrong-box measurement be detected after the fact rather than silently
        # ranked alongside runs from a different machine.
        "node_fingerprint": record.node_fingerprint,
        # Carried into the file so an offline consumer that only reads the map still sees what it
        # is holding.
        "profile": "developer",
        "rankable": False,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n")
    return existing


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="throughput.run_unit")
    ap.add_argument("--image", required=True)
    ap.add_argument("--unit", required=True, type=Path)
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--no-discard-warmup", dest="discard_warmup", action="store_false")
    ap.add_argument("--cpus", default="4")
    ap.add_argument("--memory", default="16g")
    ap.add_argument(
        "--gpus",
        default=None,
        help="docker --gpus spec, e.g. 'all'. Pass only for units whose card sets gpu = true.",
    )
    ap.add_argument(
        "--keep-output",
        type=Path,
        default=None,
        help="directory to keep the final run's /output in, for the scoring gate",
    )
    ap.add_argument(
        "--timeout-sec",
        type=float,
        default=DEFAULT_RUN_TIMEOUT_SEC,
        help="hard deadline for each container invocation; the container is killed through its "
        "cidfile when it fires",
    )
    ap.add_argument(
        "--run-as-host-user",
        action="store_true",
        help="add --user <uid>:<gid> so the output is owned by you and can simply be deleted. "
        "Off by default because some images expect root; when off, cleanup is best-effort and may "
        "leave a root-owned temp directory behind. The harness NEVER re-runs the participant "
        "image to fix ownership.",
    )
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument(
        "--host-metrics-out",
        type=Path,
        default=None,
        help="merge this unit's measured host telemetry into a shared host_metrics.json map, "
        "which the scorer passes to throughput.report.build_report(host_metrics=...). "
        f"Defaults to {HOST_METRICS_FILENAME} beside the kept output tree whenever "
        "--keep-output is given, which is the path the scoring gate reads.",
    )
    args = ap.parse_args(argv)

    # The scorer reads the map from the PARENT of the per-unit output directory
    # (``scoring.py``: ``host_metrics.load(Path(ctx["output_dir"]).parent)``), and --keep-output
    # names exactly that per-unit directory. The two paths are therefore one fact, and deriving the
    # default from --keep-output is what stops a measured run from being silently unranked because
    # the flag was omitted: without this, the file is never written on any path that does not name
    # it by hand, so every unit falls back to the submission's self-reported rate.
    host_metrics_out = args.host_metrics_out
    if host_metrics_out is None and args.keep_output is not None:
        host_metrics_out = args.keep_output.resolve().parent / HOST_METRICS_FILENAME

    record = run_unit(
        args.image,
        args.unit,
        runs=args.runs,
        discard_warmup=args.discard_warmup,
        cpus=args.cpus,
        memory=args.memory,
        gpus=args.gpus,
        keep_output=args.keep_output,
        timeout_sec=args.timeout_sec,
        run_as_host_user=args.run_as_host_user,
    )
    if host_metrics_out is not None:
        merge_host_metrics(record, host_metrics_out)
    payload = json.dumps(record.to_dict(), indent=2)
    if args.out is not None:
        args.out.write_text(payload + "\n")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
