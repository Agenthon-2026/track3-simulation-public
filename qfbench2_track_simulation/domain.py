"""The Track 3 metric domain: what the C1 clip ceiling must be, and why.

## Executive summary (read this first)

Track 3 ranks on events/sec, which has no natural upper bound, but frozen ruling R-2 requires a
finite ``metric.domain.max`` so that real scores can be clipped into the same domain the
participant-failure value ``W = 0.0`` lives in. Somebody therefore has to choose a number. The
shipped C1 simulation fixture chose **2,000,000 events/sec**, and the agent that wrote it recorded
that it had invented the figure because the freeze demanded a finite maximum and supplied none.

**That number is refused here, and it is refused on measurement rather than on taste.** A batch
unit ranks on the *aggregate* rate across its markets, so the attainable ceiling scales with batch
width; 2e6 sits *below* the competitive band this package has long cited for a batch only eight
markets wide (a band since withdrawn as never measured — see the table below, which is why the
ceiling is derived from the physical bound rather than from that band), and two orders of
magnitude below the widest batch the track ships. A ceiling below an attainable honest
score does not bound an exploit — it silently deletes the top of the leaderboard and ties every
strong submission at the same clipped value.

The replacement is not another constant. It is a **rule evaluated against the roster being
scored**, :func:`required_domain_max`, plus :func:`assert_domain_max_covers_roster`, which the
production scorer calls before it ranks anything. An invented ceiling cannot silently govern the
top again, because a plan whose ceiling would clip an honest submission is refused as an organizer
fault instead of quietly compressing the board.

## The derivation, from published figures only

**None of the events/sec figures below was measured on the evaluation fleet.** They were written
2026-06-23 against the hardware this repository described at the time — not the B200 hosts, and not
the gVisor sandbox every ranked run executes under. They are used here only to derive a ceiling
that must sit ABOVE every attainable honest score, which is the one direction in which being wrong
is free (see the asymmetry below). Do not read any of them as a target.

| Quantity | Value | Provenance |
|---|---|---|
| Unmodified ABIDES baseline | ~65,000 events/sec | No measurement recorded anywhere in this repository; WITHDRAWN as a target in `baselines/README.md` §1/§3. Retained here only as the historical input the ceiling was derived from |
| Unmodified ABIDES baseline, measured | 13,793 events/sec geomean | The 65 shipped ``units/*/events.json``; hardware not recorded, so not a fleet figure either. This is what participants are now told |
| Internal vectorized reference | ~400,000 events/sec | NOT measured on the evaluation fleet; same section |
| Competitive band, per market | 150,000-600,000 events/sec | Never measured, and WITHDRAWN from `baselines/README.md` §3 as fabricated. Retained here only as the historical input the ceiling was derived from |
| Physical per-market ceiling | 1e7 events/sec | :data:`MAX_PER_MARKET_EVENTS_PER_SEC`, below — a chosen bound, not a measurement |
| Widest batch unit | roster-dependent | ``<unit>/batch.json`` on the organizer side |

**65,000 is WITHDRAWN as a target, and the repository's own shipped data is why.** The 65 public
``units/*/events.json``, all written by the same pinned baseline, record a geometric mean of
**13,793 events/sec** (range 3,471-18,046, median 14,302) — about **4.7x below** 65,000. The
hardware behind those runs is not recorded either, and their ``wall_clock_sec`` covers the
simulation loop rather than the whole container, so they do not replace a fleet measurement; no
fleet-measured baseline exists yet and one is coming. What they do establish is that 65,000 is the
figure least likely to be right, so `baselines/README.md` now publishes 13,793 with its provenance
and gives 65,000 no role. The ranking floor is UNCHANGED by this note, and neither number is read
by the code here: see :data:`ABIDES_BASELINE_EVENTS_PER_SEC` and `baselines/README.md` §3 for what
the floor is actually compared against.

:data:`MAX_PER_MARKET_EVENTS_PER_SEC` is **not new**. It is the plausibility ceiling this package
has published since the harness-measurement handoff was written, chosen then as "more than an order
of magnitude above the top of the competitive band, so no honest submission can reach it". What
changed is its job. It used to be the last line of defence against a fabricated wall clock, and its
own docstring conceded it was "a plausibility bound, NOT an anti-cheat". The wall clock is now
host-measured in C2 and the event count is the Runner's parquet-footer count checked against the
reference, so neither half of the fraction is the participant's any more. The constant is therefore
free to do the one job a clip ceiling actually has under R-2: sit **above every attainable honest
score**, so clipping is inert for real submissions and only ever binds a value that could not have
been produced.

That is the asymmetry that decides the number. `W = 0.0` is the domain *minimum*, so the exploit
R-2 exists to close — a failure scoring better than a real result — is closed by the floor and is
untouched by the ceiling. A ceiling that is too high costs nothing; a ceiling that is too low
deletes real ranking information. So the ceiling is chosen generously and by derivation, never
tightly and by guess.

## What this means for the shipped fixture

`qfbench2_common/contracts/fixtures/c1/simulation_final.expanded.json` currently carries
``domain.max = 2000000.0``. For a roster of single-market units the required value is
:data:`MAX_PER_MARKET_EVENTS_PER_SEC` (1e7); for a roster containing batch units it is that figure
times the widest batch in the roster. The Hub owns the fixture, so this module supplies the rule
and the scorer enforces it.
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path
from typing import Any

__all__ = [
    "ABIDES_BASELINE_EVENTS_PER_SEC",
    "COMPETITIVE_BAND_EVENTS_PER_SEC",
    "DEV_PLAUSIBILITY_CEILING_PER_MARKET",
    "MAX_PER_MARKET_EVENTS_PER_SEC",
    "PARTICIPANT_FAILURE_SCORE",
    "VECTORIZED_REFERENCE_EVENTS_PER_SEC",
    "assert_domain_max_covers_roster",
    "batch_width",
    "required_domain_max",
]

#: The pre-committed worst value `W` for Track 3 (frozen: 0.0 events/sec, the domain minimum).
PARTICIPANT_FAILURE_SCORE = 0.0

#: Historical performance reference points, recorded so the ceiling derivation above cites stated
#: figures rather than numbers chosen to make it come out somewhere.
#:
#: **NOT MEASURED ON THE EVALUATION FLEET.** All three date to 2026-06-23 and describe the hardware
#: this repository described then — not the B200 hosts and not the gVisor sandbox. They are
#: DOCUMENTATION ONLY: nothing in this package or in the scorer reads them, so their values do not
#: move any score. Only :data:`MAX_PER_MARKET_EVENTS_PER_SEC` below feeds the clip ceiling.

#: WITHDRAWN AS A TARGET, and it was never the ranking floor. It was published as the floor's
#: nominal figure until ``baselines/README.md`` withdrew it; the repository's own 65 shipped
#: ``units/*/events.json``, written by this same pinned baseline, give a geometric mean of
#: 13,793 events/sec (range 3,471-18,046, median 14,302) — about 4.7x lower, and that measured
#: figure is what participants are now told. The floor itself is UNCHANGED and is not this
#: constant: per ``baselines/README.md`` §3 it compares the median of your throughput units
#: against the median recorded in those same units' reference ``events.json``.
#:
#: The value is kept, at its original number, for exactly one reason: the ceiling derivation above
#: and ``tests/test_domain_ceiling.py`` are pinned to it as a HISTORICAL INPUT. Nothing reads it to
#: score, rank or gate anything. Do not publish it as a figure to beat.
ABIDES_BASELINE_EVENTS_PER_SEC = 65_000.0

#: Internal reference point; gates nothing. Not measured on the evaluation fleet.
VECTORIZED_REFERENCE_EVENTS_PER_SEC = 400_000.0

#: NEVER MEASURED, and WITHDRAWN from ``baselines/README.md`` §3, which now records that no such
#: range was ever observed and that there were no submissions to observe it from. Kept at its
#: original value because the ceiling derivation above and ``tests/test_domain_ceiling.py`` are
#: pinned to it as a historical input; it is not a target and must not be published as one.
COMPETITIVE_BAND_EVENTS_PER_SEC = (150_000.0, 600_000.0)

#: Physical ceiling on a SINGLE market's events/sec, more than an order of magnitude above the top
#: of the (withdrawn) competitive band. A CHOSEN bound, not a measurement. Unchanged in value from
#: the plausibility bound this package has always published; see the module docstring for why its
#: role changed. This is the one constant here that feeds a scored quantity.
MAX_PER_MARKET_EVENTS_PER_SEC = 1e7

#: The DEVELOPMENT plausibility ceiling on a single market's SELF-REPORTED events/sec. Read only by
#: `scoring._developer_plausibility`, where the submission's own number is the score input and no
#: organizer clock exists, so a self-report above it is refused as physically implausible.
#:
#: Separate from :data:`MAX_PER_MARKET_EVENTS_PER_SEC` because the two do different jobs. That one
#: derives the Final clip domain, which `assert_domain_max_covers_roster` ties to the Hub's C1 plan,
#: so raising it would force the plan up in lockstep or abort Final. This one only decides what an
#: unofficial Development board believes.
#:
#: 1e7 used to serve both, derived as more than 10x the top of a competitive band that was later
#: withdrawn as never measured. It is NOT derived from any measurement of a submission: the
#: Development path has no organizer clock, so nothing on it can be measured by us, and the
#: single-market rates reported in track3-simulation-public#19 are the submission's OWN
#: ``events.json`` self-reports. The organizers confirmed which gate refused those units, not that
#: their rates were real. Do not restate them here or anywhere else as an organizer measurement.
#:
#: What this ceiling is for, then, is not a measured top but the shape of the work: cancel-heavy
#: units retire enormous numbers of order-book events per unit of simulated time and are naturally
#: fast, so a self-report one to two orders of magnitude above the withdrawn band is not by itself
#: evidence of anything. On this profile the number it bounds is stamped ``rankable = False`` and
#: ``trust_profile = development``, so believing a fast self-report costs no ranking integrity,
#: while refusing an honest one deletes the top of a practice board. 1e9 is the generous round
#: value, per the asymmetry above, and an unbounded fabrication (1e11) is still refused by 100x.
DEV_PLAUSIBILITY_CEILING_PER_MARKET = 1e9


def batch_width(unit_dir: str | Path) -> int:
    """How many markets this unit ranks over: its batch width, or 1 for a single-market unit.

    Read from the ORGANIZER-side ``batch.json`` (``input/ref/<handle>/``), never from anything the
    submission wrote. A batch unit ranks on the aggregate rate across its markets, so this is the
    factor by which its attainable ceiling exceeds a single market's.

    A malformed or unreadable ``batch.json`` raises: the width is an input to the clip ceiling, and
    silently treating an unreadable batch unit as one market would reinstate exactly the too-low
    ceiling this module exists to refuse.
    """
    path = Path(unit_dir) / "batch.json"
    if not path.exists():
        return 1
    document: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"{path} must be a JSON object")
    subs = document.get("subs")
    if not isinstance(subs, list) or not subs:
        raise ValueError(
            f"{path} declares no sub-scenarios; a batch unit must list at least one"
        )
    return len(subs)


@lru_cache(maxsize=32)
def _widest_batch(root: str, handles: tuple[str, ...]) -> int:
    widest = 1
    for handle in handles:
        widest = max(widest, batch_width(Path(root) / handle))
    return widest


def required_domain_max(
    reference_root: str | Path, unit_handles: Iterable[str]
) -> float:
    """The smallest ``metric.domain.max`` that cannot clip an honest submission on this roster.

    ``reference_root`` is the organizer's ``input/ref``; ``unit_handles`` is the C1 roster order.
    Returns :data:`MAX_PER_MARKET_EVENTS_PER_SEC` times the widest batch in the roster.

    Memoized on ``(root, handles)``: the production gate checks the ceiling once per unit, and
    without a cache a 38-unit roster would re-read every ``batch.json`` 38 times. The cache is keyed
    on the arguments and holds only integers, so it cannot carry state between evaluations.
    """
    return MAX_PER_MARKET_EVENTS_PER_SEC * _widest_batch(
        str(reference_root), tuple(str(h) for h in unit_handles)
    )


def assert_domain_max_covers_roster(
    domain_max: float, reference_root: str | Path, unit_handles: Iterable[str]
) -> None:
    """Refuse a plan whose clip ceiling sits below an attainable honest score.

    Raises :class:`ValueError` with the derivation in the message. The production gate turns that
    into an organizer fault, which aborts the evaluation rather than publishing a board whose top
    has been silently flattened.
    """
    required = required_domain_max(reference_root, unit_handles)
    if domain_max < required:
        raise ValueError(
            f"the C1 metric.domain.max is {domain_max:.6g} events/sec but this roster can "
            f"legitimately reach {required:.6g} "
            f"({MAX_PER_MARKET_EVENTS_PER_SEC:.6g} events/sec per market x "
            f"{int(required / MAX_PER_MARKET_EVENTS_PER_SEC)} market(s) in the widest batch unit). "
            "Clipping a real score into a domain that stops below it does not bound an exploit — "
            "W is the domain MINIMUM, so the R-2 property is carried entirely by the floor — it "
            "deletes ranking information at the top of the board and ties every strong submission "
            "at the ceiling. Raise metric.domain.max; see qfbench2_track_simulation.domain."
        )


def declared_scoring_params(unit_dir: str | Path) -> dict[str, Any]:
    """The ``[scoring.params]`` table from a unit's organizer-side card, or ``{}``.

    Lives here rather than in the gate so the batch path, the single-unit path and the private
    oracle all read a card the same way.
    """
    path = Path(unit_dir) / "card.toml"
    if not path.exists():
        return {}
    card = tomllib.loads(path.read_text(encoding="utf-8"))
    params = card.get("scoring", {}).get("params", {})
    return dict(params) if isinstance(params, dict) else {}
