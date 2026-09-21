"""The C1 clip ceiling is derived from the roster, and 2,000,000 events/sec is refused.

The shipped C1 simulation fixture carries ``metric.domain.max = 2000000.0``, and the agent that
wrote it recorded that it had **invented** the figure because frozen ruling R-2 demanded a finite
maximum and supplied none. This file is the ratification decision, expressed as executable
assertions rather than as prose:

* 2e6 sits **below** the published competitive band for a batch only eight markets wide
  (8 x 600,000 = 4,800,000), and two orders of magnitude below the widest batch the track ships.
  A ceiling below an attainable honest score does not bound an exploit — ``W`` is the domain
  *minimum*, so R-2's property is carried entirely by the floor — it deletes ranking information at
  the top and ties every strong submission at the ceiling.
* The replacement is a rule evaluated against the roster being scored, not another constant, and
  the production gate refuses a plan that fails it. An invented ceiling cannot silently govern the
  top of the board again.

    python -m pytest tests/test_domain_ceiling.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qfbench2_track_simulation import domain as D  # noqa: E402

#: What the shipped fixture carries today, and what this file refuses for a batch-bearing roster.
INVENTED_FIXTURE_MAX = 2_000_000.0


def _unit(root: Path, handle: str, *, width: int | None = None) -> None:
    unit = root / handle
    unit.mkdir(parents=True, exist_ok=True)
    if width is not None:
        (unit / "batch.json").write_text(
            json.dumps(
                {
                    "n": width,
                    "subs": [{"sub": f"sub_{i:03d}", "n_events": 100} for i in range(width)],
                }
            )
        )


# --------------------------------------------------------------------------- the derivation
def test_the_published_band_is_recorded_not_invented() -> None:
    """Every input to the derivation is a figure already in participant-facing documentation."""
    assert D.ABIDES_BASELINE_EVENTS_PER_SEC == 65_000.0
    assert D.VECTORIZED_REFERENCE_EVENTS_PER_SEC == 400_000.0
    assert D.COMPETITIVE_BAND_EVENTS_PER_SEC == (150_000.0, 600_000.0)
    assert D.MAX_PER_MARKET_EVENTS_PER_SEC == 1e7
    assert D.PARTICIPANT_FAILURE_SCORE == 0.0


def test_the_ceiling_sits_above_the_whole_competitive_band() -> None:
    top = D.COMPETITIVE_BAND_EVENTS_PER_SEC[1]
    assert D.MAX_PER_MARKET_EVENTS_PER_SEC > 10 * top, (
        "the per-market ceiling must be more than an order of magnitude above the top of the "
        "published band, so clipping is inert for any real submission"
    )


def test_a_single_market_roster_requires_the_per_market_ceiling(tmp_path: Path) -> None:
    _unit(tmp_path, "u-aaaaaaaaaaaaaaaa")
    assert D.required_domain_max(tmp_path, ["u-aaaaaaaaaaaaaaaa"]) == 1e7


def test_the_ceiling_scales_with_the_widest_batch_in_the_roster(tmp_path: Path) -> None:
    _unit(tmp_path, "u-aaaaaaaaaaaaaaaa")
    _unit(tmp_path, "u-bbbbbbbbbbbbbbbb", width=8)
    _unit(tmp_path, "u-cccccccccccccccc", width=3)
    handles = ["u-aaaaaaaaaaaaaaaa", "u-bbbbbbbbbbbbbbbb", "u-cccccccccccccccc"]
    assert D.required_domain_max(tmp_path, handles) == 8e7


def test_batch_width_reads_the_organizer_side_declaration(tmp_path: Path) -> None:
    _unit(tmp_path, "u-single")
    _unit(tmp_path, "u-wide", width=256)
    assert D.batch_width(tmp_path / "u-single") == 1
    assert D.batch_width(tmp_path / "u-wide") == 256


def test_a_malformed_batch_declaration_raises_rather_than_counting_as_one_market(
    tmp_path: Path,
) -> None:
    """Silently treating an unreadable batch unit as one market reinstates the too-low ceiling."""
    unit = tmp_path / "u-broken"
    unit.mkdir(parents=True)
    (unit / "batch.json").write_text(json.dumps({"n": 4, "subs": []}))
    try:
        D.batch_width(unit)
    except ValueError as exc:
        assert "at least one" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("an empty sub list must raise")


# --------------------------------------------------------------------------- the ratification
def test_the_invented_two_million_ceiling_is_refused_for_a_batch_roster(tmp_path: Path) -> None:
    """THE decision. Verified against the value the shipped fixture carries today."""
    _unit(tmp_path, "u-batch8", width=8)
    try:
        D.assert_domain_max_covers_roster(
            INVENTED_FIXTURE_MAX, tmp_path, ["u-batch8"]
        )
    except ValueError as exc:
        assert "8 market(s)" in str(exc), exc
        assert "8e+07" in str(exc) or "80000000" in str(exc), exc
    else:  # pragma: no cover
        raise AssertionError("2e6 must be refused for a roster containing an 8-market batch")


def test_two_million_is_below_an_honest_eight_market_batch() -> None:
    """The arithmetic that makes the refusal a measurement rather than an opinion."""
    honest_top = D.COMPETITIVE_BAND_EVENTS_PER_SEC[1] * 8
    assert honest_top == 4_800_000.0
    assert INVENTED_FIXTURE_MAX < honest_top, (
        "a clip ceiling below an attainable honest score flattens the top of the leaderboard"
    )


def test_the_invented_ceiling_is_also_refused_for_a_single_market_roster(tmp_path: Path) -> None:
    _unit(tmp_path, "u-single")
    try:
        D.assert_domain_max_covers_roster(INVENTED_FIXTURE_MAX, tmp_path, ["u-single"])
    except ValueError:
        return
    raise AssertionError("2e6 is below the per-market physical ceiling and must be refused")


def test_a_correct_ceiling_is_accepted(tmp_path: Path) -> None:
    """Positive control: the derived value, and anything above it, must pass."""
    _unit(tmp_path, "u-single")
    _unit(tmp_path, "u-batch8", width=8)
    handles = ["u-single", "u-batch8"]
    required = D.required_domain_max(tmp_path, handles)
    D.assert_domain_max_covers_roster(required, tmp_path, handles)
    D.assert_domain_max_covers_roster(required * 10, tmp_path, handles)


def test_the_host_metrics_ceiling_literal_agrees_with_the_single_definition() -> None:
    """`tests/test_host_metrics.py` loads one module without its package, so it carries the
    ceiling as a literal. This is the test that keeps the two from drifting."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import test_host_metrics as HM  # noqa: PLC0415

    assert HM.CEILING_PER_MARKET == D.DEV_PLAUSIBILITY_CEILING_PER_MARKET


def test_the_dev_ceiling_sits_an_order_of_magnitude_above_the_highest_self_report() -> None:
    """The Development ceiling clears the fastest self-report anyone has actually submitted.

    The figure below is a SELF-REPORT from track3-simulation-public#19, not a measurement: the
    Development path has no organizer clock, so no rate on it is measured by us. It is used only as
    an existence proof that real submissions report rates of this order, which is why a 1e7 ceiling
    refused them; the ceiling itself is set by the asymmetry in `domain.py` (too high costs nothing
    on a board stamped rankable=False, too low deletes real ranking information).

    Kept separate from the Final constant on purpose: raising that one would move the Hub's C1 clip
    requirement and abort Final unless the plan moved in lockstep.
    """
    highest_self_report = 12_040_000.0  # track3-simulation-public#19, the submission's events.json
    # Margin at the shipped value: 1e9 / 1.204e7 = 83x. The floor asserted here is 10x.
    assert D.DEV_PLAUSIBILITY_CEILING_PER_MARKET > 10 * highest_self_report
    assert D.DEV_PLAUSIBILITY_CEILING_PER_MARKET < 1e11  # still refuses the pinned fabrication
    assert D.MAX_PER_MARKET_EVENTS_PER_SEC == 1e7  # the Final clip derivation is untouched


def _run_all() -> int:
    import tempfile

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            if "tmp_path" in t.__code__.co_varnames[: t.__code__.co_argcount]:
                with tempfile.TemporaryDirectory() as tmp:
                    t(Path(tmp))
            else:
                t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
