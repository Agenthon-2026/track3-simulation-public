"""Track 3's contribution to the C3 sanitation policy: the exact output allowlist and the bounds.

C3 says the participant's output directory and the scoring program's input directory are not the
same directory, and that each track contributes the exact set of relative paths its submissions may
write. This module is Track 3's contribution, in one place, so the production Runner, the local
developer harness and the tests all bound the same set.

Track 3 outputs, and nothing else:

| Unit shape | Allowed relative paths |
|---|---|
| single-market | ``trace.parquet``, ``events.json``, ``message_trace.parquet``, ``profile.json`` |
| batch | ``batch_events.json`` plus, per declared sub, ``<sub>/{trace,message_trace}.parquet``, ``<sub>/events.json`` and ``<sub>/profile.json`` |

``profile.json`` is the OPTIONAL Best Systems Diagnosis sidecar. ``docs/PROFILING.md`` tells a
participant to drop it next to ``trace.parquet`` in ``/output`` and ``throughput/simprofile.py``
calls it "a submission output sidecar"; the cross-submission assembler that consumes it lives
outside this repository (``throughput/awards.py`` scores the resulting diagnosis, it does not read
the file). Until 2026-09-02 the name was in none of these lists, so a submission that followed the
guide had its WHOLE output tree refused (``path_not_allowed``) and the award had no compliant path
(track3-simulation-private#28, found by NVIDIA). It is admitted by name and bounded like every
other member; the ranked scorer never reads it, so admitting it widens no ranking surface.

Maximum depth 2. The sub list comes from the ORGANIZER's ``batch.json``, never from a directory
listing of what the submission produced — otherwise a submission could widen its own allowlist by
creating directories.

**Why a row bound and not only a byte bound.** Track 3's ranked numerator is the emitted trace's
row count. A padded trace is cheap in bytes (a repeated event compresses extremely well in parquet)
and expensive in rank, so a byte cap is not the bound that matters here. `max_rows_for` expresses
the row bound relative to the organizer's deterministic reference count, enforceable from the
parquet footer before any parser runs.
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Sequence

from qfbench2_common.contracts import ContractError, OrganizerFault, normalize_tree_path

__all__ = [
    "BATCH_ROOT_FILES",
    "MAX_DEPTH",
    "PROFILE_SIDECAR",
    "SINGLE_UNIT_FILES",
    "STABLE_OUTPUT_FILES",
    "STABLE_REPEAT_POLICY_ID",
    "SUB_FILES",
    "VOLATILE_EVENTS_FIELDS",
    "VOLATILE_OUTPUT_FILES",
    "allowed_paths_for",
    "max_rows_for",
    "requires_message_ledger",
    "stable_paths_for",
    "stable_repeat_policy_for",
    "volatile_paths_for",
]

#: The optional profiling sidecar the Best Systems Diagnosis award reads (see the module note).
PROFILE_SIDECAR = "profile.json"

SINGLE_UNIT_FILES: tuple[str, ...] = (
    "trace.parquet",
    "events.json",
    "message_trace.parquet",
    PROFILE_SIDECAR,
)
BATCH_ROOT_FILES: tuple[str, ...] = ("batch_events.json",)
SUB_FILES: tuple[str, ...] = (
    "trace.parquet",
    "events.json",
    "message_trace.parquet",
    PROFILE_SIDECAR,
)
MAX_DEPTH = 2

#: Output members whose BYTES are reproducible across repeats of the same unit.
#:
#: A Track 3 scenario is deterministic given its seed, so a faithful submission emits the same
#: parquet bytes every time. These are the members a cross-repeat byte comparison can be made
#: against.
STABLE_OUTPUT_FILES: tuple[str, ...] = ("trace.parquet", "message_trace.parquet")
STABLE_REPEAT_POLICY_ID = "t3-stable-output-v1"

#: Output members whose bytes CANNOT be reproducible, because they report measurements of the run.
#:
#: This list did not exist until now, and its absence is the whole of
#: `track3-simulation-public#5` / `Agenthon2026#116`: the repeat check compares a byte digest of
#: the WHOLE output tree across repeats, `events.json` is inside that tree, and `events.json` must
#: carry a real `wall_clock_sec`. An honest submission therefore diverges on every repeat and is
#: refused. It is invisible in Development, which ranks through `build_developer_verifier` on the
#: self-reported branch, and first bites in the Final phase.
#:
#: Naming the set here, beside the allowlist that already owns "what may appear in /output", is the
#: prerequisite for every candidate repair: excluding these from the digest, canonicalising their
#: volatile fields before hashing, or comparing them semantically all need this list to exist.
VOLATILE_OUTPUT_FILES: tuple[str, ...] = (
    "events.json",
    "batch_events.json",
    PROFILE_SIDECAR,
)

#: The `events.json` fields that measure the run and so cannot be byte-stable.
#:
#: Three prose lists of this set exist and no two agree: the starter pack names three fields and
#: omits `gpu_seconds`, then twelve lines later treats `gpu_seconds` as a legitimate extra, and
#: `Agenthon2026#116` names a third set omitting `events_per_sec`. This is the machine-readable
#: one. Everything NOT in here is reproducible from the scenario and the seed: `scenario_id`,
#: `n_events`, `seed`, `trace_sha256`.
VOLATILE_EVENTS_FIELDS: frozenset[str] = frozenset(
    {
        "wall_clock_sec",
        "events_per_sec",
        "peak_memory_bytes",
        "gpu_seconds",
    }
)


def allowed_paths_for(unit_dir: str | Path) -> tuple[str, ...]:
    """The exact relative paths a submission for this unit may write.

    Reads ``batch.json`` from the organizer's unit directory to learn the sub list. A unit with no
    ``batch.json`` is single-market.
    """
    path = Path(unit_dir) / "batch.json"
    if not path.exists():
        return SINGLE_UNIT_FILES
    document = json.loads(path.read_text(encoding="utf-8"))
    subs: Sequence[dict[str, object]] = document["subs"]
    paths: list[str] = list(BATCH_ROOT_FILES)
    for entry in subs:
        sub = str(entry["sub"])
        paths.extend(f"{sub}/{name}" for name in SUB_FILES)
    return tuple(paths)


def stable_paths_for(unit_dir: str | Path) -> tuple[str, ...]:
    """The allowed paths for this unit whose bytes a faithful submission reproduces exactly."""
    return tuple(
        p
        for p in allowed_paths_for(unit_dir)
        if p.rsplit("/", 1)[-1] in STABLE_OUTPUT_FILES
    )


def requires_message_ledger(card: Mapping[str, Any]) -> bool:
    """The existing semantic gate's declaration/default, also used by repeat producers."""
    declared = card.get("scoring", {}).get("params", {}).get("requires_message_ledger")
    if isinstance(declared, bool):
        return declared
    return bool(card.get("task", {}).get("scenario_family") != "throughput-scale")


def stable_repeat_policy_for(unit_dir: str | Path) -> dict[str, Any]:
    """Candidate repeat policy derived only from the organizer's card and batch manifest.

    Producer and consumer pass this dictionary to the shared ``stable_output_binding`` helper.
    A single-market trace is required; its ledger follows the existing card semantic rule and
    still affects the digest whenever optional and present. Every declared batch sub requires
    both files. No caller-supplied member list or participant directory listing sets this policy.
    """
    root = Path(unit_dir)
    try:
        card = tomllib.loads((root / "card.toml").read_text(encoding="utf-8"))
        batch_path = root / "batch.json"
        is_batch = bool(card.get("batch", {}).get("batch"))
        if batch_path.exists() != is_batch:
            raise OrganizerFault(
                "organizer card and batch manifest disagree on unit shape"
            )
        if is_batch:
            subs = json.loads(batch_path.read_text(encoding="utf-8"))["subs"]
            if not isinstance(subs, list) or not subs:
                raise OrganizerFault(
                    "organizer batch manifest must declare nonempty subs"
                )
            names = []
            for entry in subs:
                name = normalize_tree_path(entry["sub"], field="organizer batch sub")
                if "/" in name:
                    raise OrganizerFault(
                        "organizer batch sub must be one directory component"
                    )
                names.append(name)
            if len({name.casefold() for name in names}) != len(names):
                raise OrganizerFault("organizer batch manifest repeats a sub")
            members = tuple(
                f"{sub}/{file}" for sub in names for file in STABLE_OUTPUT_FILES
            )
            required = members
        else:
            members = STABLE_OUTPUT_FILES
            required = members if requires_message_ledger(card) else ("trace.parquet",)
    except OrganizerFault:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ContractError):
        raise OrganizerFault(
            "organizer stable-output policy is unreadable or malformed"
        ) from None
    return {
        "policy_id": STABLE_REPEAT_POLICY_ID,
        "members": members,
        "required_members": required,
    }


def volatile_paths_for(unit_dir: str | Path) -> tuple[str, ...]:
    """The allowed paths for this unit that report measurements and so cannot be byte-stable."""
    return tuple(
        p
        for p in allowed_paths_for(unit_dir)
        if p.rsplit("/", 1)[-1] in VOLATILE_OUTPUT_FILES
    )


def max_rows_for(reference_rows: int, *, slack: float = 0.0) -> int:
    """The largest trace row count admissible for a unit whose reference has ``reference_rows``.

    ``slack`` is zero by design: a Track 3 scenario is deterministic given its seed, so a faithful
    run emits exactly as many rows as the reference. The parameter exists so a future family with a
    documented tolerance can express it rather than reaching for an inequality somewhere else.
    """
    if reference_rows < 0:
        raise ValueError("reference_rows must be non-negative")
    return int(reference_rows * (1.0 + slack))
