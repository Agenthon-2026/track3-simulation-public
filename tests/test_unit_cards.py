"""Card invariants that the generators must keep reproducing.

Two of these are drift guards. A unit's ``card.toml`` is generated (``scripts/build_public_units.py``
for flat units, ``scripts/build_batch_units.py`` for batch ones), so a value edited in the committed
cards but not in the generator survives only until the next regeneration. That happened: every card
carried ``[environment] gpu = true`` while both generators still emitted ``false``, so regenerating
the suite would have silently reverted the only ``gpu = true`` cards in the competition.

  * ``test_every_card_requests_a_gpu`` pins the value in the committed cards.
  * ``test_generators_emit_the_committed_environment_block`` pins the generators to it, which is the
    half that actually prevents the revert.
  * ``test_every_card_carries_a_scenario_family`` pins the input ``cluster_key`` reads. A card
    without one falls back to a singleton cluster in the hub's bootstrap, silently, and the
    published confidence interval comes out too narrow.
  * ``test_every_card_declares_whether_a_message_ledger_is_required`` and its generator half pin
    the newest card field. The ledger gate used to trigger on whether a reference
    ``message_trace.parquet`` happened to exist, so an unshipped reference file switched the
    latency-causality check off silently. It is a declaration now, and a declaration that only the
    committed cards carry is one regeneration away from vanishing.

Stdlib-only by default so this runs in the firewall CI job, which does not install the shared
toolkit and therefore still runs when that install breaks. The one toolkit-backed check is named
in the output when it is skipped: a partial pass is not a full validation and must not be read as
one.

    python tests/test_unit_cards.py
"""

from __future__ import annotations

import os
import pathlib
import re
import sys
import tomllib

ROOT = pathlib.Path(__file__).resolve().parent.parent
UNITS = ROOT / "units"

#: The frozen Track 3 container contract. Every unit gets identical hardware, because the track is
#: ranked on throughput and a per-unit-optimal allocation would make the ranking incomparable.
EXPECTED_ENVIRONMENT = {
    "cpus": 4,
    "memory": "16G",
    "gpu": True,
    "network": "none",
    "disk": "10G",
}

_GENERATORS = ("scripts/build_public_units.py", "scripts/build_batch_units.py")

#: Scenario families whose single-market units ship no message ledger. Everything else declares
#: one. Kept here as data so the card check and the generator check read the same rule.
LEDGER_OPTIONAL_FAMILIES = frozenset({"throughput-scale"})

#: Set by the CI job that installs the shared toolkit. A check that reports SKIP is not a check
#: that passed, and this file is one of the guards the private repo's skipped-green defect taught
#: us not to trust: where the dependency IS available, a skip is a failure.
REQUIRE_TOOLKIT_ENV = "QFB2_T3_REQUIRE_TOOLKIT"


class Skipped(Exception):
    """Raised by a check that could not run. Tallied separately, never as a pass."""


def _skip(reason: str) -> None:
    if os.environ.get(REQUIRE_TOOLKIT_ENV, "").strip().lower() in {"1", "true", "yes", "on"}:
        raise AssertionError(
            f"{reason} -- and {REQUIRE_TOOLKIT_ENV} is set, so this check was required to run"
        )
    raise Skipped(reason)
#: The [environment] table as the generator emits it: the header, then only key = value lines.
#: Anchored this tightly because the block sits inside a Python string literal, so anything
#: looser runs on into the surrounding source.
_ENV_BLOCK = re.compile(r"^\[environment\]\n((?:[ \t]*\w+[ \t]*=[^\n]*\n)+)", re.MULTILINE)


def _cards() -> list[pathlib.Path]:
    cards = sorted(UNITS.glob("*/card.toml"))
    assert cards, f"no unit cards under {UNITS}"
    return cards


def test_every_card_requests_a_gpu() -> None:
    bad = []
    for c in _cards():
        env = tomllib.loads(c.read_text()).get("environment", {})
        if env.get("gpu") is not True:
            bad.append(f"{c.parent.name}: gpu={env.get('gpu')!r}")
    assert not bad, "cards not requesting a GPU: " + ", ".join(bad)


def test_every_card_matches_the_frozen_container_contract() -> None:
    bad = []
    for c in _cards():
        env = tomllib.loads(c.read_text()).get("environment", {})
        for key, want in EXPECTED_ENVIRONMENT.items():
            if env.get(key) != want:
                bad.append(f"{c.parent.name}.{key}={env.get(key)!r} (want {want!r})")
    assert not bad, "cards off the container contract: " + ", ".join(bad)


def test_generators_emit_the_committed_environment_block() -> None:
    """The half that stops a regeneration reverting the committed cards."""
    for gen in _GENERATORS:
        src = (ROOT / gen).read_text()
        blocks = _ENV_BLOCK.findall(src)
        assert len(blocks) == 1, f"{gen}: expected one [environment] block, found {len(blocks)}"
        parsed = tomllib.loads("[environment]\n" + blocks[0])["environment"]
        assert parsed == EXPECTED_ENVIRONMENT, (
            f"{gen} emits {parsed!r}, committed cards carry {EXPECTED_ENVIRONMENT!r}; "
            f"regenerating would overwrite them"
        )


def test_every_card_declares_whether_a_message_ledger_is_required() -> None:
    """Every card states it, and the value matches the family rule."""
    bad = []
    for c in _cards():
        card = tomllib.loads(c.read_text())
        params = card.get("scoring", {}).get("params", {})
        declared = params.get("requires_message_ledger")
        family = card.get("task", {}).get("scenario_family", "")
        want = family not in LEDGER_OPTIONAL_FAMILIES
        if declared is not want:
            bad.append(f"{c.parent.name}: requires_message_ledger={declared!r} (want {want!r})")
    assert not bad, "cards with a wrong ledger declaration: " + ", ".join(bad)


def test_generators_emit_the_ledger_declaration() -> None:
    """The half that stops a regeneration dropping the field back to nothing."""
    for gen in _GENERATORS:
        src = (ROOT / gen).read_text()
        assert "requires_message_ledger" in src, (
            f"{gen} does not emit requires_message_ledger; regenerating would drop the "
            f"declaration and the ledger gate would fall back to file existence"
        )


def test_every_card_carries_a_scenario_family() -> None:
    """``cluster_key`` reads this. Empty means a singleton cluster and a too-narrow published CI."""
    bad = []
    for c in _cards():
        fam = tomllib.loads(c.read_text()).get("task", {}).get("scenario_family")
        if not isinstance(fam, str) or not fam:
            bad.append(f"{c.parent.name}: scenario_family={fam!r}")
    assert not bad, "cards without a scenario family: " + ", ".join(bad)


def test_cluster_key_groups_every_unit() -> None:
    """The function the hub actually calls. Needs the toolkit; skipped loudly without it."""
    sys.path.insert(0, str(ROOT))
    try:
        from qfbench2_track_simulation import cluster_key
    except ModuleNotFoundError as exc:
        _skip(f"cluster_key needs qfbench2-common, which is not installed here ({exc})")
    units = sorted(p for p in UNITS.iterdir() if p.is_dir())
    keys = [cluster_key(u) for u in units]
    unkeyed = [u.name for u, k in zip(units, keys) if k is None]
    assert not unkeyed, f"units with no cluster key: {unkeyed}"
    clusters = set(keys)
    # Both degenerate ends defeat the cluster bootstrap: one cluster per unit is arithmetically
    # identical to assuming independence, and a single cluster makes the CI uninformative.
    assert 1 < len(clusters) < len(units), (
        f"{len(clusters)} cluster(s) over {len(units)} units is degenerate"
    )
    # Never raises on a bad input: the hub swallows exceptions here and falls back to per-unit
    # clusters, which is the silent degradation this function exists to remove.
    assert cluster_key(UNITS / "does-not-exist") is None


#: Participant-facing sentences that state the size of the Development roster, or a census taken
#: over it. Each entry is (path, template); ``{n}`` is the number of directories under ``units/``,
#: ``{single}`` / ``{batch}`` the two arities, ``{ledger}`` the cards declaring a required ledger.
#:
#: This list exists because a roster change leaves these behind. The 72 -> 71 withdrawal edited
#: nine of them and missed ``regression_suite/README.md`` ("all 72 public cards do"), which nothing
#: asserted: the suite was green with the file stating 71 in one paragraph and 72 in another.
ROSTER_PROSE = (
    ("README.md", "over the {n} public units"),
    ("README.md", "**{ledger} of the {n}** public units"),
    ("README.md", "{n} of {n} cards put it there"),
    ("README.md", "{n} public units: {single} single-scenario, {batch} batch"),
    ("AGENTS.md", "({single} single-scenario of {n} public units"),
    ("AGENTS.md", "all {n} cards under `units/`"),
    ("docs/CONCEPTS.md", "the {n} public units ({single} single-scenario and {batch} batch)"),
    ("regression_suite/README.md", "the {n} units in `../units/`"),
    ("regression_suite/README.md", "all {n} cards under `../units/`"),
)


def _roster_census() -> dict:
    """The numbers the prose is allowed to state, read from the cards and the tree."""
    units = sorted(p for p in UNITS.iterdir() if p.is_dir())
    batch = [u for u in units if (u / "batch.json").exists()]
    ledger = 0
    for unit in units:
        card = tomllib.loads((unit / "card.toml").read_text(encoding="utf-8"))
        if card.get("scoring", {}).get("params", {}).get("requires_message_ledger") is True:
            ledger += 1
    return {"n": len(units), "batch": len(batch), "single": len(units) - len(batch),
            "ledger": ledger}


def test_published_prose_states_the_real_roster_size():
    """Every pinned sentence must state the measured numbers.

    A stale roster count is not cosmetic here: a team that counts 71 directories against a
    sentence saying 72 cannot tell whether one card is missing the key the sentence is about.
    """
    census = _roster_census()
    missing = []
    for rel, template in ROSTER_PROSE:
        want = template.format(**census)
        body = (ROOT / rel).read_text(encoding="utf-8")
        if want not in body:
            missing.append(f"{rel}: expected {want!r}")
    assert not missing, (
        "roster prose does not match the measured census "
        + repr(census) + "; " + "; ".join(missing)
        + " -- re-measure the sentence, or update ROSTER_PROSE with it if it was reworded"
    )


def test_the_roster_prose_pin_is_not_vacuous():
    """Control: the pin must fail on a roster size that is not this repository's.

    Substring pins fail open when the sentence is reworded and nobody notices, so prove here that
    a wrong number is actually rejected rather than silently matched.
    """
    census = _roster_census()
    wrong = dict(census, n=census["n"] + 1)
    matched = [
        rel for rel, template in ROSTER_PROSE
        if template.format(**wrong) in (ROOT / rel).read_text(encoding="utf-8")
    ]
    assert not matched, (
        f"a roster size of {wrong['n']} still matches in {matched}; the pin cannot detect drift"
    )
    assert census["single"] + census["batch"] == census["n"]

    # Coverage floor: the pin must keep covering every file that states the roster size, so a
    # reworded sentence cannot be "fixed" by dropping its entry.
    assert len(ROSTER_PROSE) >= 9, f"ROSTER_PROSE shrank to {len(ROSTER_PROSE)} entries"
    assert {rel for rel, _ in ROSTER_PROSE} == {
        "README.md", "AGENTS.md", "docs/CONCEPTS.md", "regression_suite/README.md"
    }, "ROSTER_PROSE no longer covers every roster-stating file"


def _run_all() -> int:
    """Script runner. SKIP is tallied separately from PASS.

    A harness that prints SKIP and counts it as a pass is the private repo's skipped-green defect
    in miniature: the summary line says everything ran when nothing did.
    """
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = skipped = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Skipped as e:
            skipped += 1
            print(f"SKIP {t.__name__}: {e}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    passed = len(tests) - failed - skipped
    print(f"\n{passed}/{len(tests)} passed, {skipped} skipped, {failed} failed")
    return 1 if failed else 0


def test_the_readme_names_the_toml_table_the_cards_actually_use():
    """The README told participants to read the wrong table, twice in one day.

    NVIDIA reported (issue #48) that the docs named families while the cards decided, and #49 fixed
    that prose. The fix then said `[scoring].requires_message_ledger` — measured, 0 of 72 cards put
    it there and 72 of 72 put it under `[scoring.params]`. A lookup on the wrong table returns
    nothing, reads as "not required", and lands a participant back in the exact failure the section
    exists to prevent: 59 units refused at g3.

    Prose that instructs a lookup is executable documentation, so this executes it.
    """
    import pathlib
    import tomllib

    repo = pathlib.Path(__file__).resolve().parent.parent
    readme = (repo / "README.md").read_text(encoding="utf-8")

    documented = [t for t in ("[scoring.params].requires_message_ledger",
                              "[scoring].requires_message_ledger") if t in readme]
    assert documented, "the README no longer documents where to read requires_message_ledger"
    assert documented[0] == "[scoring.params].requires_message_ledger", (
        f"the README documents {documented[0]}, which resolves on no card")

    resolves = missing = 0
    for unit in sorted((repo / "units").iterdir()):
        card = unit / "card.toml"
        if not card.exists():
            continue
        data = tomllib.loads(card.read_text(encoding="utf-8"))
        if data.get("scoring", {}).get("params", {}).get("requires_message_ledger") is None:
            missing += 1
        else:
            resolves += 1
    assert missing == 0, (
        f"{missing} card(s) do not carry requires_message_ledger under [scoring.params], so the "
        f"documented lookup returns nothing for them")
    assert resolves > 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
