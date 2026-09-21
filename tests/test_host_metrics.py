"""The developer profile cannot rank, and the rankable fallback is gone.

This file used to pin the OPPOSITE behaviour: that ``host_metrics.resolve`` returns the
submission's self-reported events/sec when the harness recorded no measurement, "which preserves
today's behaviour on a harness that does not yet emit the file". As measured, the harness never
emitted the file on the production path, so that branch was taken on every unit and the
leaderboard ranked numbers the submissions chose.

C2 now carries host-measured timing, so the fallback is removed rather than flagged. What is left
in :mod:`qfbench2_track_simulation.host_metrics` is a *developer profile*: a local practice reader
whose every result is ``rankable = False``. These tests assert the removal (the regression), the
developer profile's honesty about what it is, and the plausibility bound that still applies to a
self-report on that profile.

Docker-free and dependency-free: no trace is read, only the score-selection logic.

``host_metrics`` imports nothing outside the stdlib, but its PACKAGE does — importing
``qfbench2_track_simulation`` runs an ``__init__`` that pulls in ``.scoring`` and therefore pandas.
The secret-free CI job installs pyarrow only, so the module is loaded straight from its file to
keep the test honest about its dependencies rather than widening the job to accommodate it.

    python tests/test_host_metrics.py
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import sys
import tempfile
from pathlib import Path

_PATH = (
    Path(__file__).resolve().parent.parent
    / "qfbench2_track_simulation"
    / "host_metrics.py"
)
_spec = importlib.util.spec_from_file_location("_t3_host_metrics", _PATH)
assert _spec is not None and _spec.loader is not None
H = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = H
_spec.loader.exec_module(H)

MEASURED = 48.7
FABRICATED = 604_000_000.0

#: The single definition lives in `qfbench2_track_simulation.domain`; repeated here as a literal
#: because this file deliberately loads one module without its package. `test_domain_ceiling.py`
#: asserts the two agree, so a drift is caught by a test rather than by a wrong refusal.
CEILING_PER_MARKET = 1e9


def _map(**over: object) -> dict[str, object]:
    entry = {
        "host_events_per_sec": MEASURED,
        "host_wall_clock_sec": 12.4,
        "host_n_events": 604,
        "host_gpu_seconds": 0.0,
        "host_peak_memory_bytes": 1 << 30,
    }
    entry.update(over)
    return {"t3-unit": entry}


# --------------------------------------------------------------------------- the regression
def test_the_rankable_self_report_fallback_no_longer_exists() -> None:
    """`resolve()` was the fallback. Its absence is the fix, so its absence is the assertion.

    Fails on the pre-fix module, which exported `resolve`, `strict_mode`, `strict_violation` and
    the `QFB2_T3_REQUIRE_HOST_TELEMETRY` flag that was supposed to make the fallback fatal one day.
    """
    for gone in ("resolve", "strict_mode", "strict_violation", "REQUIRE_ENV"):
        assert not hasattr(H, gone), (
            f"host_metrics.{gone} is back. The rankable path must have no participant-rate "
            "fallback and no environment flag that decides whether it has one."
        )


def test_no_function_here_can_be_mistaken_for_the_official_path() -> None:
    """Every public entry point names itself a developer-profile or a plain reader."""
    assert "developer" in H.developer_events_per_sec.__name__
    doc = inspect.getdoc(H.developer_events_per_sec) or ""
    assert "NON-RANKABLE" in doc, "the developer scorer must say so in its own docstring"
    assert H.PROFILE_DEVELOPER == "developer"


# --------------------------------------------------------------------------- developer profile
def test_developer_profile_prefers_the_local_measurement() -> None:
    score, source = H.developer_events_per_sec(_map(), "t3-unit", FABRICATED)
    assert score == MEASURED
    assert source == H.SOURCE_HOST


def test_developer_profile_falls_back_and_says_so() -> None:
    score, source = H.developer_events_per_sec(None, "t3-unit", 1234.5)
    assert score == 1234.5
    assert source == H.SOURCE_SELF, "a self-reported developer score must be labelled as one"


def test_ranked_median_is_preferred_over_the_ratio_of_medians() -> None:
    # host_events_per_sec is the median of per-run rates; host_n_events / host_wall_clock_sec is a
    # ratio of medians. They are close but not equal, and the first is the documented statistic.
    entry = _map()["t3-unit"]
    assert isinstance(entry, dict)
    ratio = float(entry["host_n_events"]) / float(entry["host_wall_clock_sec"])  # type: ignore[arg-type]
    assert abs(ratio - MEASURED) > 1e-9
    assert H.entry_events_per_sec(entry) == MEASURED


def test_entry_rejects_junk_without_raising() -> None:
    for junk in (None, [], "48.7", {}, {"host_events_per_sec": True},
                 {"host_events_per_sec": -1}, {"host_n_events": 10, "host_wall_clock_sec": 0}):
        assert H.entry_events_per_sec(junk) is None, junk


def test_a_corrupt_handoff_raises_rather_than_downgrading() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "host_metrics.json").write_text("[1, 2, 3]")
        try:
            H.load(root)
        except ValueError as exc:
            assert "JSON object" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("a non-object handoff must raise, not be treated as absent")
        (root / "host_metrics.json").write_text("{not json")
        try:
            H.load(root)
        except ValueError:
            pass
        else:  # pragma: no cover
            raise AssertionError("unreadable JSON must raise")


def test_absent_handoff_is_none_not_an_error() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        assert H.load(Path(tmp)) is None


# --------------------------------------------------------------------------- plausibility bound
def test_plausibility_ceiling_refuses_the_reported_exploit() -> None:
    assert (
        H.implausible_self_report(
            None, "t3-unit", 1e11, ceiling_per_market=CEILING_PER_MARKET
        )
        is not None
    )


def test_plausibility_ceiling_is_inert_when_the_harness_measured() -> None:
    assert (
        H.implausible_self_report(
            _map(), "t3-unit", 1e11, ceiling_per_market=CEILING_PER_MARKET
        )
        is None
    )


def test_plausibility_ceiling_scales_with_batch_width() -> None:
    """A batch ranks on AGGREGATE events/sec, so a flat bound would refuse an honest wide batch."""
    just_over = CEILING_PER_MARKET * 8 * 1.01
    assert (
        H.implausible_self_report(
            None, "t3-unit", just_over, 8, ceiling_per_market=CEILING_PER_MARKET
        )
        is not None
    )
    assert (
        H.implausible_self_report(
            None, "t3-unit", CEILING_PER_MARKET * 7, 8, ceiling_per_market=CEILING_PER_MARKET
        )
        is None
    )


def test_published_competitive_rates_are_never_refused() -> None:
    """Nothing in the documented performance band may be refused (baselines/README.md)."""
    for rate in (65_000.0, 400_000.0, 600_000.0, 1_000_000.0):
        assert (
            H.implausible_self_report(
                None, "t3-unit", rate, ceiling_per_market=CEILING_PER_MARKET
            )
            is None
        ), rate


def test_self_reported_rates_from_issue_19_are_never_refused() -> None:
    """track3-simulation-public#19. Four units of a real submission SELF-REPORTED these
    single-market rates in their own events.json, and at the old 1e7 ceiling every one was refused
    as schema_invalid and scored zero.

    They are not organizer measurements and nothing here treats them as one: the Development path
    has no organizer clock, which is the very reason this ceiling exists. What the organizers
    confirmed on #19 is which gate refused the units, not that the rates were real. They are pinned
    here as the shape of self-report a cancel-heavy unit plausibly produces, so that a ceiling
    regression that would refuse them again fails this test instead of a participant's board."""
    for rate in (12_040_000.0, 10_470_000.0, 10_870_000.0, 10_880_000.0):
        assert (
            H.implausible_self_report(
                None, "t3-unit", rate, ceiling_per_market=CEILING_PER_MARKET
            )
            is None
        ), rate


def test_plausibility_ceiling_tolerates_a_degenerate_market_count() -> None:
    # Never raises, per the gate contract: a zero/negative width falls back to one market.
    assert (
        H.implausible_self_report(
            None, "t3-unit", 1e11, 0, ceiling_per_market=CEILING_PER_MARKET
        )
        is not None
    )
    assert (
        H.implausible_self_report(
            None, "t3-unit", 50.0, 0, ceiling_per_market=CEILING_PER_MARKET
        )
        is None
    )


def test_load_reads_a_well_formed_map() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "host_metrics.json").write_text(json.dumps(_map()))
        loaded = H.load(root)
        assert loaded is not None and "t3-unit" in loaded


def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
