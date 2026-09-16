"""
Track 3 throughput measurement protocol -- the LOCAL DEVELOPER HARNESS. It cannot rank.

## Executive summary (read this first)

**This module is not the official timing path and never produces an official number.** The Runner
owns participant launch, lifecycle, timing, C2 and C3 on the platform; Track 3 owns the semantic
rules and the telemetry requirements the Runner's evidence must satisfy. Every record this harness
emits carries ``rankable = False`` and ``profile = "developer"``, and the production scorer factory
(`qfbench2_track_simulation.scoring.build_verifier`) has no path that reads it.

Three bounds make the local harness safe to point at an untrusted image, because a practice run
still executes participant code on somebody's workstation:

* **a hard deadline** on every container invocation (`DEFAULT_RUN_TIMEOUT_SEC`), enforced by
  killing the container through its cidfile and then the client process. Previously
  `subprocess.run(cmd, stdout=PIPE, stderr=PIPE)` was called with no `timeout=`, so an image that
  never exits hung the harness forever;
* **capped log capture**: output is drained continuously -- so the container never blocks on a full
  pipe -- but only the last `LOG_TAIL_BYTES` of each stream is retained. Previously `PIPE` buffered
  the container's entire stdout in harness memory, which a stdout flood turns into an OOM;
* **no re-invocation of the participant image**, for cleanup or anything else. Cleanup is host-side
  and best-effort.

Runs a candidate Docker image N times (default 5) with different seeds drawn from a
seed family derived from the scenario's base seed, discards the first run as warm-up,
and reports the median events/sec alongside the full distribution.

Warm-up rationale
-----------------
The first container invocation in a sequence typically runs slower than subsequent
ones due to several cold-start effects that are not intrinsic to the simulator's
algorithmic performance:

  1. JIT compilation caches (Numba, JAX XLA, etc.): the first run compiles kernels
     and writes LLVM IR / cubin artefacts to ``/tmp`` or ``~/.cache``; subsequent
     runs reuse these caches via mmap.  On a fresh container the cache is cold.
  2. OS page cache: the first run reads the scenario JSON and any supporting data
     files from NVMe; subsequent runs read from the OS buffer cache (RAM).
  3. Python import overhead: even though Docker images contain ``.pyc`` files,
     the first ``import`` statement for each module causes a ``stat(2)`` cascade
     that warms the dentry cache.

Discarding the first run (``--discard-warmup``, enabled by default) therefore gives
a **steady-state** measurement that is a fairer proxy for production throughput.  If
you are benchmarking a pure-C binary with no JIT, you can pass ``--no-discard-warmup``
to include all runs; the first run is then identical in cost to the rest.

Usage (CLI)
-----------
::

    python timer.py \\
        --image ghcr.io/my-org/my-sim:latest \\
        --scenario /path/to/scenario.json \\
        --runs 5 \\
        --discard-warmup \\
        --output results.json

Usage (Python API)
------------------
::

    from timer import measure_throughput, ThroughputResult
    from pathlib import Path

    result = measure_throughput(
        image="ghcr.io/my-org/my-sim:latest",
        scenario_path=Path("/path/to/scenario.json"),
        runs=5,
        discard_warmup=True,
    )
    print(f"Median: {result.median_events_per_sec:,.0f} events/sec")
    print(f"Std:    {result.std_events_per_sec:,.0f} events/sec")
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import threading
import time
import hashlib
import statistics
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Sequence


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class ThroughputResult:
    """
    Aggregate throughput statistics for a single (image, scenario) pair.

    Attributes
    ----------
    image : str
        Docker image reference used for the measurement.
    scenario_id : str
        The ``scenario_id`` field read from the scenario JSON, used for
        traceability in result archives.
    seed_family : list[int]
        The full list of seeds passed to each run (including the warm-up
        seed at index 0, even if that run was discarded from statistics).
    raw_events_per_sec : list[float]
        events/sec for each run in seed-family order.  If ``warmup_discarded``
        is True, the first entry corresponds to the discarded warm-up run and
        is included here for auditing purposes but is **not** used in any of
        the aggregate statistics below.
    warmup_discarded : bool
        Whether the first run was excluded from the aggregate statistics.
    median_events_per_sec : float
        Median over the scored runs (i.e. all runs if warmup_discarded is
        False, runs[1:] otherwise).
    mean_events_per_sec : float
        Arithmetic mean over the scored runs.
    std_events_per_sec : float
        Population standard deviation over the scored runs.  Returns 0.0 if
        only one run was scored.
    min_events_per_sec : float
        Minimum over the scored runs.
    max_events_per_sec : float
        Maximum over the scored runs.
    wall_clock_seconds : list[float]
        Wall-clock elapsed time (seconds) for each container invocation, in
        the same order as ``raw_events_per_sec``.
    host_gpu_seconds : list[float | None]
        Host-measured GPU busy-time (utilization-weighted seconds, sampled via NVML) for each run,
        or ``None`` for a run where no GPU / NVML was present. This is the INDEPENDENT measurement
        that the awards pipeline consumes instead of a submission's self-reported ``gpu_seconds``.
    median_host_gpu_seconds : float | None
        Median host GPU-seconds over the scored runs, or ``None`` if none were measured.
    host_peak_memory_bytes : list[int | None]
        Host-measured peak container resident memory (cgroup) for each run, or ``None`` where the
        cgroup was not readable. The independent counterpart to self-reported ``peak_memory_bytes``.
    median_host_peak_memory_bytes : int | None
        Median host peak memory over the scored runs, or ``None`` if none were measured.
    """

    image: str
    scenario_id: str
    seed_family: list[int]
    raw_events_per_sec: list[float]
    warmup_discarded: bool
    median_events_per_sec: float
    mean_events_per_sec: float
    std_events_per_sec: float
    min_events_per_sec: float
    max_events_per_sec: float
    wall_clock_seconds: list[float]
    host_gpu_seconds: list[float | None] = field(default_factory=list)
    median_host_gpu_seconds: float | None = None
    host_peak_memory_bytes: list[int | None] = field(default_factory=list)
    median_host_peak_memory_bytes: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable, explicitly non-rankable developer record."""
        return {**asdict(self), "profile": "developer", "rankable": False}

    def __str__(self) -> str:
        scored_runs = (
            len(self.raw_events_per_sec) - 1
            if self.warmup_discarded
            else len(self.raw_events_per_sec)
        )
        return (
            f"ThroughputResult(image={self.image!r}, scenario={self.scenario_id!r}, "
            f"runs={scored_runs}, "
            f"median={self.median_events_per_sec:,.0f} ev/s, "
            f"mean={self.mean_events_per_sec:,.0f} ev/s, "
            f"std={self.std_events_per_sec:,.0f} ev/s)"
        )


# ---------------------------------------------------------------------------
# Host-side telemetry (independent of the submission's self-report)
# ---------------------------------------------------------------------------


@dataclass
class RunMetrics:
    """One container invocation's host-measured numbers (WS-1 ``host_*`` telemetry).

    All three are measured by the harness, independently of anything the submission writes into its
    own ``events.json``: ``host_wall_clock_sec`` by this timer, ``host_gpu_seconds`` by sampling NVML
    GPU utilization, ``host_peak_memory_bytes`` by reading the container's cgroup. The GPU and memory
    fields are ``None`` when the box cannot supply them (no GPU/NVML, unreadable cgroup), in which
    case the diagnostics fall back to the self-report and flag it.
    """

    events_per_sec: float
    host_wall_clock_sec: float
    host_gpu_seconds: float | None
    host_peak_memory_bytes: int | None = None

    def as_host_metrics(self) -> dict[str, Any]:
        """The per-unit ``host_metrics`` mapping consumed by ``throughput.report.build_report``."""
        return {
            "host_wall_clock_sec": self.host_wall_clock_sec,
            "host_gpu_seconds": self.host_gpu_seconds,
            "host_peak_memory_bytes": self.host_peak_memory_bytes,
        }


class _GpuSampler:
    """Background NVML poller. Integrates device utilization × dt into GPU busy-seconds while a run
    is in flight, and exposes the total via :meth:`result`. It degrades to ``None`` whenever NVML is
    unavailable (import fails, no driver, no GPU) or no sample landed, so the timer runs unchanged on
    a CPU-only box. Nothing here touches the wall-clock → events/sec path, which stays the
    authoritative ranked measurement; ``pynvml`` is a host-side dev dependency and is never in the
    ``network=none`` submission image."""

    def __init__(self, interval_s: float = 0.05) -> None:
        self._interval = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._busy_seconds = 0.0
        self._samples = 0
        self._ok = False

    def __enter__(self) -> _GpuSampler:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        try:
            import pynvml

            pynvml.nvmlInit()
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        except Exception:
            return  # no GPU / NVML — result() stays None
        self._ok = True
        last = time.monotonic()
        try:
            while not self._stop.wait(self._interval):
                now = time.monotonic()
                dt = now - last
                last = now
                try:
                    util = pynvml.nvmlDeviceGetUtilizationRates(handle).gpu  # 0..100
                    self._busy_seconds += (float(util) / 100.0) * dt
                    self._samples += 1
                except Exception:
                    pass
        finally:
            try:
                pynvml.nvmlShutdown()
            except Exception:
                pass

    def result(self) -> float | None:
        """GPU busy-seconds, or ``None`` if NVML was unavailable or produced no sample."""
        return self._busy_seconds if (self._ok and self._samples) else None


class _NullGpuSampler:
    """Stand-in used when no GPU was attached to the container. Reports ``None`` rather than 0.0, so
    a run with no GPU is treated as *unmeasured* (and falls back to the self-report, flagged) rather
    than as an authoritative zero."""

    def __enter__(self) -> _NullGpuSampler:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def result(self) -> float | None:
        return None


class _CgroupMemorySampler:
    """Background poller for the container's PEAK resident memory, read from its cgroup.

    The container is started with ``--cidfile`` so its id is known as soon as it exists; from the id
    we locate the cgroup directory and track the peak. cgroup v2 exposes ``memory.peak`` directly
    (kernel 5.19+); otherwise we keep the running maximum of ``memory.current`` (v2) or read
    ``memory.max_usage_in_bytes`` (v1). Sampling happens DURING the run because ``docker run --rm``
    removes the cgroup on exit.

    Degrades to ``None`` whenever the cgroup is unreadable (most notably Docker Desktop on macOS /
    Windows, where container cgroups live inside a VM), so the timer behaves identically off-box.
    """

    _CANDIDATE_FILES = ("memory.peak", "memory.current", "memory.max_usage_in_bytes")

    def __init__(self, cidfile: Path, interval_s: float = 0.05) -> None:
        self._cidfile = cidfile
        self._interval = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._peak: int | None = None
        self._dir: Path | None = None

    def __enter__(self) -> _CgroupMemorySampler:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    @staticmethod
    def _cgroup_dir(cid: str) -> Path | None:
        """Locate a container's cgroup directory across the common v2/v1 and systemd/cgroupfs layouts."""
        # In order: cgroup v2 + systemd driver, v2 + cgroupfs driver, v1 + cgroupfs, v1 + systemd.
        candidates = [
            Path(f"/sys/fs/cgroup/system.slice/docker-{cid}.scope"),
            Path(f"/sys/fs/cgroup/docker/{cid}"),
            Path(f"/sys/fs/cgroup/memory/docker/{cid}"),
            Path(f"/sys/fs/cgroup/memory/system.slice/docker-{cid}.scope"),
        ]
        for path in candidates:
            if path.is_dir():
                return path
        return None

    def _read_peak(self, cgroup_dir: Path) -> int | None:
        for name in self._CANDIDATE_FILES:
            try:
                return int((cgroup_dir / name).read_text().strip())
            except (OSError, ValueError):
                continue
        return None

    def _run(self) -> None:
        cid = ""
        while not self._stop.wait(self._interval):
            if not cid:
                try:  # the cidfile appears as soon as the container is created
                    cid = self._cidfile.read_text().strip()
                except OSError:
                    continue
                if not cid:
                    continue
            if self._dir is None:
                # Docker writes the cidfile when the container is CREATED, but runc only publishes
                # the cgroup at START, so an early miss is normally that race rather than an
                # unreadable cgroup. Keep probing for the life of the run; the loop still ends when
                # __exit__ sets the stop event, and result() is still None where the cgroup is
                # genuinely invisible (Docker Desktop's VM).
                self._dir = self._cgroup_dir(cid)
                if self._dir is None:
                    continue
            current = self._read_peak(self._dir)
            if current is not None and (self._peak is None or current > self._peak):
                self._peak = current

    def result(self) -> int | None:
        """Peak container resident bytes, or ``None`` if the cgroup was not readable."""
        return self._peak


# gVisor will not start a GPU sandbox unless the driver capabilities are declared, and docker's
# default set is not enough. Measured by NVIDIA on a B200: under `runsc-gpu`, `--gpus all` alone
# fails with **exit 125 -- the sandbox never starts**; the same run with this variable exits 0.
#
# `compute` deliberately, not `utility` or `all`. `utility` injects the nvidia-persistenced socket,
# which a participant container has no business holding. A CuPy workload JIT-compiling through
# nvrtc was verified to work under `compute` alone on the same hardware.
#
# Track 3 is the track this matters most for: EVERY T3 unit card sets `gpu = true`, so on a gVisor
# worker without it, 100% of Track 3 units fail to launch -- and this launcher, not the hub's
# ingestion program, is what produces the ranked events/sec number.
DRIVER_CAPABILITIES = (
    os.environ.get("QFBENCH_DRIVER_CAPABILITIES", "").strip() or "compute"
)


#: Hard deadline for one local container invocation, in seconds. Generous relative to the published
#: scenario horizons (a public unit runs in single-digit seconds) and finite, which is the property
#: that matters: the previous call had no `timeout=` at all, so an image with an infinite loop hung
#: the harness until somebody noticed.
DEFAULT_RUN_TIMEOUT_SEC = float(os.environ.get("QFB2_T3_RUN_TIMEOUT_SEC", "") or 1800.0)

#: Bytes retained per stream. Output is fully DRAINED so the container never blocks on a full pipe;
#: only the tail is kept, because the tail is where the error is. Previously the whole of stdout was
#: buffered in harness memory, which a `yes`-style flood turns into an OOM on the measuring host.
LOG_TAIL_BYTES = 64 * 1024


def _drain_tail(stream: Any, cap: int, sink: list[bytes]) -> None:
    """Read a pipe to EOF, keeping only its last ``cap`` bytes."""
    buffered = bytearray()
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            buffered.extend(chunk)
            if len(buffered) > cap:
                del buffered[:-cap]
    except (OSError, ValueError):
        pass
    finally:
        sink.append(bytes(buffered))


def bounded_container_run(
    cmd: list[str],
    *,
    cidfile: Path,
    timeout_sec: float = DEFAULT_RUN_TIMEOUT_SEC,
    log_tail_bytes: int = LOG_TAIL_BYTES,
) -> subprocess.CompletedProcess[bytes]:
    """`docker run` with a hard deadline and bounded log capture.

    On timeout the CONTAINER is killed first, through the id `--cidfile` wrote, and only then the
    docker client process. Killing the client alone would leave a detached container holding the
    GPU and the CPU quota, which would corrupt the next measurement as well as leaking the run.

    Returns a `CompletedProcess` whose `returncode` is negative when the deadline fired, so a caller
    cannot mistake a killed run for a clean exit.
    """
    stdout_tail: list[bytes] = []
    stderr_tail: list[bytes] = []
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    readers = [
        threading.Thread(
            target=_drain_tail,
            args=(proc.stdout, log_tail_bytes, stdout_tail),
            daemon=True,
        ),
        threading.Thread(
            target=_drain_tail,
            args=(proc.stderr, log_tail_bytes, stderr_tail),
            daemon=True,
        ),
    ]
    for reader in readers:
        reader.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_container(cidfile)
        proc.kill()
        proc.wait(timeout=30)
    for reader in readers:
        reader.join(timeout=10)
    for pipe in (proc.stdout, proc.stderr):
        if pipe is not None:
            pipe.close()
    returncode = proc.returncode if not timed_out else -abs(proc.returncode or 1)
    return subprocess.CompletedProcess(
        cmd,
        returncode,
        stdout_tail[0] if stdout_tail else b"",
        stderr_tail[0] if stderr_tail else b"",
    )


def kill_container(cidfile: Path) -> None:
    """Best-effort `docker kill` of the container named by ``cidfile``. Never raises.

    Bounded itself: a `docker kill` that hangs must not turn the deadline into a second hang.
    """
    try:
        container_id = Path(cidfile).read_text().strip()
    except OSError:
        return
    if not container_id:
        return
    try:
        subprocess.run(
            ["docker", "kill", container_id],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def gpu_docker_args(gpus: str | None) -> list[str]:
    """`docker run` GPU flags, including the capability declaration gVisor requires.

    Both throughput launchers must use this. They build their own argv independently and neither
    inherits from the hub's ingestion program, so a fix applied to one of the three is a fix
    applied to none of the runs the other two drive."""
    if not gpus:
        return []
    return ["--gpus", gpus, "-e", f"NVIDIA_DRIVER_CAPABILITIES={DRIVER_CAPABILITIES}"]


def timed_container_run(
    cmd: list[str],
    *,
    cidfile: Path,
    gpus: str | None,
    timeout_sec: float = DEFAULT_RUN_TIMEOUT_SEC,
) -> tuple[subprocess.CompletedProcess[bytes], float, float | None, int | None]:
    """Run one container invocation under the host samplers and return
    ``(completed_process, wall_clock_sec, host_gpu_seconds, host_peak_memory_bytes)``.

    This is the single place that implements the measurement discipline, so every caller (the
    single-scenario timer and the worker-side unit runner) gets it identically:

    * the GPU is sampled ONLY when one was actually attached, otherwise NVML would report whatever
      else is using device 0 and a spurious ``0.0`` would look authoritative;
    * both timestamps sit INSIDE the sampler context, so thread start-up and especially the join in
      ``__exit__`` (an NVML call plus ``nvmlShutdown``, bounded by a 2 s timeout) are never charged
      to the ranked wall clock. Charging them would make an identical image score worse on a
      GPU-equipped box than on a CPU-only one.

    ``cmd`` must already contain ``--cidfile <cidfile>``; the file has to live outside the mounted
    ``/input`` and ``/output`` trees so the submission never sees it.

    ``timeout_sec`` is a HARD deadline. A run that reaches it is killed through the cidfile and its
    result carries a negative return code, so an image that never exits costs one timeout rather
    than the harness.
    """
    gpu_sampler: _GpuSampler | _NullGpuSampler = (
        _GpuSampler() if gpus else _NullGpuSampler()
    )
    mem_sampler = _CgroupMemorySampler(cidfile)
    with gpu_sampler, mem_sampler:
        t_start = time.monotonic()
        proc = bounded_container_run(cmd, cidfile=cidfile, timeout_sec=timeout_sec)
        wall_clock = time.monotonic() - t_start
    return proc, wall_clock, gpu_sampler.result(), mem_sampler.result()


# ---------------------------------------------------------------------------
# Seed derivation
# ---------------------------------------------------------------------------


def derive_seed_family(base_seed: int, n: int) -> list[int]:
    """
    Deterministically derive *n* independent seeds from *base_seed*.

    The derivation uses SHA-256 hash chaining so that seeds are unpredictable
    from the base seed without computing the hashes, and so that seeds for
    different positions *i* are independent even when base seeds differ by 1.

    Algorithm
    ---------
    For each ``i`` in ``range(n)``::

        raw = hashlib.sha256(f"{base_seed}:{i}".encode()).hexdigest()
        seed_i = int(raw, 16) & 0x7FFF_FFFF   # truncate to 31 bits

    The 31-bit mask ensures compatibility with simulators that use
    ``numpy.random.default_rng(seed)`` or ``random.seed(seed)`` with
    platform-specific int width constraints.

    Parameters
    ----------
    base_seed : int
        The scenario's ``base_seed`` as read from ``scenario.json``.
    n : int
        Number of seeds to generate.  Must be >= 1.

    Returns
    -------
    list[int]
        A list of *n* non-negative integers, each < 2**31.

    Examples
    --------
    >>> derive_seed_family(42, 3)
    [1152824280, 1045830604, 891234017]   # values are illustrative
    """
    if n < 1:
        raise ValueError(f"n must be >= 1, got {n}")
    seeds: list[int] = []
    for i in range(n):
        digest = hashlib.sha256(f"{base_seed}:{i}".encode()).hexdigest()
        seed_i = int(digest, 16) & 0x7FFF_FFFF
        seeds.append(seed_i)
    return seeds


# ---------------------------------------------------------------------------
# Single-run execution
# ---------------------------------------------------------------------------


def run_single(
    image: str,
    scenario_path: Path,
    seed: int,
    cpus: str = "4",
    memory: str = "16g",
    output_dir: Path | None = None,
    gpus: str | None = None,
) -> RunMetrics:
    """
    Run one container invocation and return its :class:`RunMetrics`.

    The function:

    1. Creates a temporary directory for ``/input`` and writes a seed-patched copy
       of *scenario_path* there (the original file is never modified).
    2. Optionally uses the caller-supplied *output_dir* for ``/output``, or creates
       its own temporary directory.
    3. Invokes::

           docker run --rm --network=none \\
               --cpus=<cpus> --memory=<memory> \\
               -v <input_dir>:/input:ro \\
               -v <output_dir>:/output \\
               <image> simulate --config /input/scenario.json --out /output/trace.parquet

    4. Times the subprocess with ``time.monotonic``.
    5. Reads ``/output/events.json`` and extracts ``n_events``.
    6. Returns a :class:`RunMetrics` with ``events_per_sec`` and the host-measured fields.

    Parameters
    ----------
    image : str
        Docker image reference (e.g. ``"ghcr.io/my-org/my-sim:sha-abc1234"``).
    scenario_path : Path
        Path to the scenario JSON on the host filesystem.
    seed : int
        Seed to inject into the scenario JSON under the key ``"seed"``.
    cpus : str
        Value for Docker ``--cpus`` flag.  Default ``"4"`` matches the official
        benchmark hardware allocation.
    memory : str
        Value for Docker ``--memory`` flag.  Default ``"16g"`` matches the
        official benchmark hardware allocation.
    output_dir : Path | None
        If provided, use this directory for container output (must be writable).
        If None, a temporary directory is created and cleaned up automatically.

    Returns
    -------
    RunMetrics
        ``events_per_sec`` plus the harness-measured ``host_wall_clock_sec``,
        ``host_gpu_seconds`` (``None`` on a CPU-only box), and ``host_peak_memory_bytes``
        (``None`` when the container cgroup is not readable from this host).

    Raises
    ------
    RuntimeError
        If the container exits with a non-zero status code, or if
        ``/output/events.json`` is missing or does not contain ``"n_events"``.
    FileNotFoundError
        If *scenario_path* does not exist.
    """
    if not scenario_path.exists():
        raise FileNotFoundError(f"Scenario not found: {scenario_path}")

    with open(scenario_path) as fh:
        scenario_config: dict[str, Any] = json.load(fh)

    # Patch the seed in a copy — never mutate the original file.
    scenario_config["seed"] = seed

    with (
        tempfile.TemporaryDirectory(prefix="t3_input_") as input_tmp,
        tempfile.TemporaryDirectory(prefix="t3_cid_") as cid_tmp,
    ):
        input_dir = Path(input_tmp)
        patched_scenario = input_dir / "scenario.json"
        with open(patched_scenario, "w") as fh:
            json.dump(scenario_config, fh, indent=2)

        if output_dir is None:
            output_tmp_ctx = tempfile.TemporaryDirectory(prefix="t3_output_")
            output_tmp_obj = output_tmp_ctx.__enter__()
            out_dir = Path(output_tmp_obj)
        else:
            output_tmp_ctx = None
            out_dir = output_dir
            out_dir.mkdir(parents=True, exist_ok=True)

        # --cidfile lets the memory sampler find the container's cgroup while it runs. Docker
        # requires the path not to exist yet, and it must live OUTSIDE the mounted /input and
        # /output directories so the container never sees it.
        cidfile = Path(cid_tmp) / "container.cid"

        try:
            cmd = [
                "docker",
                "run",
                "--rm",
                "--cidfile",
                str(cidfile),
                "--network=none",
                f"--cpus={cpus}",
                *gpu_docker_args(gpus),
                f"--memory={memory}",
                "-v",
                f"{input_dir}:/input:ro",
                "-v",
                f"{out_dir}:/output",
                image,
                "simulate",
                "--config",
                "/input/scenario.json",
                "--out",
                "/output/trace.parquet",
            ]

            proc, wall_clock, host_gpu_seconds, host_peak_memory_bytes = (
                timed_container_run(cmd, cidfile=cidfile, gpus=gpus)
            )

            if proc.returncode != 0:
                stderr_snippet = proc.stderr[-2000:].decode("utf-8", errors="replace")
                raise RuntimeError(
                    f"Container exited with code {proc.returncode}.\n"
                    f"Image: {image}\nSeed: {seed}\n"
                    f"Stderr (last 2000 bytes):\n{stderr_snippet}"
                )

            events_json_path = out_dir / "events.json"
            if not events_json_path.exists():
                raise RuntimeError(
                    f"events.json not found in output directory after container exit.\n"
                    f"Image: {image}, seed: {seed}.\n"
                    f"Output directory contents: {list(out_dir.iterdir())}"
                )

            with open(events_json_path) as fh:
                events_data: dict[str, Any] = json.load(fh)

            if "n_events" not in events_data:
                raise RuntimeError(
                    f"events.json is missing required key 'n_events'.\n"
                    f"Got keys: {list(events_data.keys())}"
                )

            n_events: int = int(events_data["n_events"])
            if wall_clock <= 0:
                raise RuntimeError(
                    f"Wall-clock time was non-positive ({wall_clock}); "
                    "something went wrong with timing."
                )

            events_per_sec = n_events / wall_clock
            return RunMetrics(
                events_per_sec, wall_clock, host_gpu_seconds, host_peak_memory_bytes
            )

        finally:
            if output_tmp_ctx is not None:
                output_tmp_ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def measure_throughput(
    image: str,
    scenario_path: Path,
    runs: int = 5,
    discard_warmup: bool = True,
    cpus: str = "4",
    memory: str = "16g",
    gpus: str | None = None,
) -> ThroughputResult:
    """
    Measure steady-state throughput of a Docker image on a given scenario.

    Runs the container *runs* times using seeds from a deterministic seed family
    derived from the scenario's ``base_seed``.  If *discard_warmup* is True,
    the first run's statistics are excluded from aggregate calculations (but
    retained in ``raw_events_per_sec`` at index 0 for audit purposes).

    Parameters
    ----------
    image : str
        Docker image reference.
    scenario_path : Path
        Path to the scenario JSON on the host filesystem.
    runs : int
        Total number of container invocations, including the warm-up run.
        Must be >= 2 when *discard_warmup* is True (so that at least one
        scored run remains).  Must be >= 1 when *discard_warmup* is False.
    discard_warmup : bool
        If True, exclude the first run from aggregate statistics.
    cpus : str
        Value for Docker ``--cpus`` flag.
    memory : str
        Value for Docker ``--memory`` flag.

    Returns
    -------
    ThroughputResult
        Full results including per-run measurements and aggregate statistics.

    Raises
    ------
    ValueError
        If *runs* is too small given *discard_warmup*.
    RuntimeError
        Propagated from :func:`run_single` if any container invocation fails.
    """
    if discard_warmup and runs < 2:
        raise ValueError(
            f"runs must be >= 2 when discard_warmup=True (got runs={runs}). "
            "Need at least one warm-up run and one scored run."
        )
    if not discard_warmup and runs < 1:
        raise ValueError(f"runs must be >= 1, got {runs}.")

    # Read scenario metadata (base_seed, scenario_id) before spawning containers.
    with open(scenario_path) as fh:
        scenario_config: dict[str, Any] = json.load(fh)

    base_seed: int = int(
        scenario_config.get("base_seed", scenario_config.get("seed", 0))
    )
    scenario_id: str = str(scenario_config.get("scenario_id", scenario_path.stem))

    seed_family = derive_seed_family(base_seed, runs)

    raw_eps: list[float] = []
    wall_clocks: list[float] = []
    host_gpu: list[float | None] = []
    host_mem: list[int | None] = []

    for run_idx, seed in enumerate(seed_family):
        print(
            f"  Run {run_idx + 1}/{runs} "
            f"({'warm-up' if run_idx == 0 and discard_warmup else 'scored'})"
            f"  seed={seed}",
            flush=True,
        )
        m = run_single(
            image=image,
            scenario_path=scenario_path,
            seed=seed,
            cpus=cpus,
            memory=memory,
            gpus=gpus,
        )
        raw_eps.append(m.events_per_sec)
        wall_clocks.append(m.host_wall_clock_sec)
        host_gpu.append(m.host_gpu_seconds)
        host_mem.append(m.host_peak_memory_bytes)
        gpu_note = (
            f", gpu={m.host_gpu_seconds:.2f}s" if m.host_gpu_seconds is not None else ""
        )
        mem_note = (
            f", peak={m.host_peak_memory_bytes / 1e6:,.0f}MB"
            if m.host_peak_memory_bytes is not None
            else ""
        )
        print(
            f"    -> {m.events_per_sec:,.0f} events/sec  "
            f"({m.host_wall_clock_sec:.2f}s wall clock{gpu_note}{mem_note})",
            flush=True,
        )

    # Determine which runs feed into aggregate statistics.
    scored_eps = raw_eps[1:] if discard_warmup else raw_eps
    scored_gpu = [
        g for g in (host_gpu[1:] if discard_warmup else host_gpu) if g is not None
    ]
    scored_mem = [
        m for m in (host_mem[1:] if discard_warmup else host_mem) if m is not None
    ]

    if len(scored_eps) == 1:
        std = 0.0
    else:
        std = statistics.pstdev(scored_eps)

    return ThroughputResult(
        image=image,
        scenario_id=scenario_id,
        seed_family=seed_family,
        raw_events_per_sec=raw_eps,
        warmup_discarded=discard_warmup,
        median_events_per_sec=statistics.median(scored_eps),
        mean_events_per_sec=statistics.mean(scored_eps),
        std_events_per_sec=std,
        min_events_per_sec=min(scored_eps),
        max_events_per_sec=max(scored_eps),
        wall_clock_seconds=wall_clocks,
        host_gpu_seconds=host_gpu,
        median_host_gpu_seconds=(statistics.median(scored_gpu) if scored_gpu else None),
        host_peak_memory_bytes=host_mem,
        median_host_peak_memory_bytes=(
            int(statistics.median(scored_mem)) if scored_mem else None
        ),
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> None:
    """
    Command-line entry point for the Track 3 throughput timer.

    Parses arguments, calls :func:`measure_throughput`, prints a JSON summary
    to stdout, and optionally writes the full result to a file.

    Exit codes
    ----------
    0
        All runs completed successfully.
    1
        One or more container invocations failed (RuntimeError propagated).
    2
        Argument parsing error.
    """
    parser = argparse.ArgumentParser(
        prog="timer.py",
        description="Measure events/sec of a Track 3 simulation Docker image.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python timer.py --image ghcr.io/my-org/my-sim:latest --scenario scenario.json
  python timer.py --image my-sim:dev --scenario sc.json --runs 7 --no-discard-warmup
  python timer.py --image my-sim:dev --scenario sc.json --output results.json
""",
    )
    parser.add_argument(
        "--image",
        required=True,
        help="Docker image reference (e.g. ghcr.io/my-org/my-sim:latest).",
    )
    parser.add_argument(
        "--scenario",
        required=True,
        type=Path,
        help="Path to the scenario JSON configuration file.",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=5,
        help="Total number of container invocations (including warm-up). Default: 5.",
    )
    parser.add_argument(
        "--discard-warmup",
        dest="discard_warmup",
        action="store_true",
        default=True,
        help="Discard the first run from aggregate statistics (default: on).",
    )
    parser.add_argument(
        "--no-discard-warmup",
        dest="discard_warmup",
        action="store_false",
        help="Include all runs in aggregate statistics.",
    )
    parser.add_argument(
        "--cpus",
        default="4",
        help="Docker --cpus value. Default: 4 (matches benchmark hardware).",
    )
    parser.add_argument(
        "--memory",
        default="16g",
        help="Docker --memory value. Default: 16g (matches benchmark hardware).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="If provided, write the JSON result to this file in addition to stdout.",
    )

    args = parser.parse_args(argv)

    print("Track 3 Throughput Timer", flush=True)
    print(f"  Image:    {args.image}", flush=True)
    print(f"  Scenario: {args.scenario}", flush=True)
    print(
        f"  Runs:     {args.runs} ({'warmup discarded' if args.discard_warmup else 'all scored'})",
        flush=True,
    )
    print(flush=True)

    result = measure_throughput(
        image=args.image,
        scenario_path=args.scenario,
        runs=args.runs,
        discard_warmup=args.discard_warmup,
        cpus=args.cpus,
        memory=args.memory,
    )

    print(flush=True)
    print("=" * 60, flush=True)
    print(result, flush=True)
    print("=" * 60, flush=True)

    result_dict = result.to_dict()
    result_json = json.dumps(result_dict, indent=2)
    print(result_json, flush=True)

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w") as fh:
            fh.write(result_json)
            fh.write("\n")
        print(f"\nResult written to: {args.output}", flush=True)


if __name__ == "__main__":
    main()
