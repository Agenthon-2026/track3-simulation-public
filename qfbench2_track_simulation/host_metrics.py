"""The DEVELOPER-profile throughput reader. Nothing here can produce a ranked score.

## Executive summary (read this first)

This module used to be the read side of a worker->scorer handoff that decided the leaderboard. It
is not that any more, and the change is deliberate rather than cosmetic.

The old contract was: prefer a harness measurement from ``host_metrics.json``, and **fall back to
the submission's self-reported rate** when none was present. The fallback was load-bearing in the
worst way — the common CodaBench ingestion path never wrote the file, so no harness measurement
ever existed and every production unit ranked on a number the submission chose. The consistency
checks around it pinned ``events_per_sec == n_events / wall_clock_sec`` and pinned ``n_events`` to
the real trace rows, but ``wall_clock_sec`` was compared against nothing, so a self-consistent
triple with a shrunken clock passed all of them. The only remaining bound was a plausibility
ceiling that this file's own comment already described as "a plausibility bound, NOT an
anti-cheat", roughly seventeen times the top of the honest competitive band.

The producer now exists: C2 carries host-measured timing, per-repeat records and Runner-measured
parquet-footer row counts. :mod:`qfbench2_track_simulation.telemetry` consumes it and is the only
source of a ranked rate. **The participant-rate fallback is removed from every rankable path**, and
what remains here is a *separately named developer profile* for local practice runs against a
harness that cannot produce trusted timing.

Everything this module returns is stamped ``rankable = False``. There is no environment flag that
promotes it. A caller that wants an official number calls :mod:`telemetry` and, when the evidence
is absent, gets an organizer fault — which is the honest outcome, because Track 3's score *is* a
measurement and a missing measurement is missing evidence rather than a low score.

File layout, unchanged, for the local harness that still writes it::

    {"t3-fastlob-core": {"host_events_per_sec": 118402.5, "host_wall_clock_sec": 12.4,
                         "host_n_events": 1468190, "host_gpu_seconds": 3.1,
                         "host_peak_memory_bytes": 2147483648,
                         "node_fingerprint": {...}}}

Stdlib-only on purpose: the CI job that guards it installs no scientific stack, and the guard is
more honest for being loadable without one.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

__all__ = [
    "PROFILE_DEVELOPER",
    "SOURCE_HOST",
    "SOURCE_SELF",
    "developer_events_per_sec",
    "entry_events_per_sec",
    "implausible_self_report",
    "load",
]

#: Provenance values recorded alongside a developer-profile score, so an offline result set stays
#: auditable. Neither value ever appears on an official board: the official path has no
#: `self_reported` branch at all, and a `host_measured` value read from this file is a LOCAL
#: harness measurement, not a Runner-signed C2 one.
SOURCE_HOST = "host_measured"
SOURCE_SELF = "self_reported"

#: Stamped on every developer-profile result. Kept as a literal rather than imported so this
#: module stays loadable straight from its file by the secret-free CI guard.
PROFILE_DEVELOPER = "developer"

_FILENAME = "host_metrics.json"


def load(output_root: pathlib.Path) -> dict[str, Any] | None:
    """Load the unit -> measurement map from ``output_root/host_metrics.json``.

    Returns ``None`` when the file is absent. Raises :class:`ValueError` when it exists but is not
    a JSON object, so a corrupt local handoff is a loud failure rather than a silent downgrade.
    """
    path = pathlib.Path(output_root) / _FILENAME
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{_FILENAME} is present but unreadable: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ValueError(
            f"{_FILENAME} must be a JSON object mapping unit -> measurement"
        )
    return loaded


def entry_events_per_sec(entry: Any) -> float | None:
    """The locally measured events/sec for one unit, or ``None`` if the entry cannot supply it.

    Prefers ``host_events_per_sec`` — the median of the per-run rates over the scored runs. Falls
    back to ``host_n_events / host_wall_clock_sec`` for maps written before that field existed;
    that fallback is a ratio of medians rather than a median of ratios, so it is close to but not
    identical with the local harness's own statistic.
    """
    if not isinstance(entry, dict):
        return None
    direct = entry.get("host_events_per_sec")
    if isinstance(direct, (int, float)) and not isinstance(direct, bool) and direct > 0:
        return float(direct)
    n, wall = entry.get("host_n_events"), entry.get("host_wall_clock_sec")
    if (
        isinstance(n, (int, float))
        and isinstance(wall, (int, float))
        and not isinstance(n, bool)
        and not isinstance(wall, bool)
        and n > 0
        and wall > 0
    ):
        return float(n) / float(wall)
    return None


def developer_events_per_sec(
    host_map: dict[str, Any] | None, unit: str, self_reported: float
) -> tuple[float, str]:
    """A NON-RANKABLE events/sec for local practice, with its provenance.

    Returns ``(events_per_sec, source)``. The local harness measurement wins when there is one;
    otherwise the submission's own number is used, **and the caller is required to mark the result
    ``rankable = False``**. That is the whole reason this function has a name that cannot be
    mistaken for the official path: the previous name was ``resolve``, it was called from the
    production factory, and its self-reported branch was the number the leaderboard published.

    Never raises. Nothing here is authoritative, so a caller must not be able to abort an
    evaluation through it.
    """
    measured = entry_events_per_sec((host_map or {}).get(unit))
    if measured is not None:
        return measured, SOURCE_HOST
    return float(self_reported), SOURCE_SELF


def implausible_self_report(
    host_map: dict[str, Any] | None,
    unit: str,
    self_reported: float,
    n_markets: int = 1,
    *,
    ceiling_per_market: float,
) -> str | None:
    """Why this unit's self-reported rate is not physically possible; ``None`` when it may stand.

    Applies **only** on the self-reported developer branch: when the local harness measured the
    unit, its number is used and the submission's claim is not load-bearing.

    ``ceiling_per_market`` is passed in rather than defined here so exactly one definition of the
    ceiling exists in the package — :data:`qfbench2_track_simulation.domain.
    DEV_PLAUSIBILITY_CEILING_PER_MARKET`. This module stays stdlib-only and file-loadable as a result.

    ``n_markets`` scales the ceiling for batch units, which rank on the AGGREGATE events/sec across
    the whole batch: a legitimate wide-batch submission running N markets in parallel can report
    close to N times a single market's rate, and a flat per-unit ceiling would refuse it.

    Never raises, so a caller can turn a refusal into an ordinary inadmissible verdict.
    """
    if entry_events_per_sec((host_map or {}).get(unit)) is not None:
        return None
    ceiling = float(ceiling_per_market) * max(1, int(n_markets))
    if self_reported <= ceiling:
        return None
    return (
        f"self-reported events_per_sec {self_reported:.6g} for unit {unit!r} exceeds the "
        f"plausibility ceiling {ceiling:.6g} ({float(ceiling_per_market):.6g} events/sec x "
        f"{max(1, int(n_markets))} market(s)); refusing to rank an unmeasured rate no simulator "
        "can achieve"
    )
