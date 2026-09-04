"""The Tier-A draw-sequence contract, and the numbers the documentation states about it.

Why this file exists. Tier A demands an exact trace match. 65 of the 66 public scenarios draw
message latency from a distribution, and those draws share ONE seeded NumPy global stream with
the agents, consumed in a fixed call order (``baselines/abides_fork/config.py``: oracle, then one
draw per agent, then the latency model, then the kernel). So the latency stream's seed depends on
how many agents were constructed before it, and a candidate that changes the generator, the number
of draws, or their order gets a different event calendar from the same seed -- and fails Tier A
however correct its market logic is.

Two published statements were on opposite sides of this, describing the same simulator:

    docs/CATEGORIES.md   (Tier B) "a slightly different internal RNG sequence will produce
                                  slightly different prices even with the same seed"
    regression_suite/    (Tier A) "The scenario is deterministic given its seed"

The first is true. The second is true only of the reference implementation. These tests keep the
documentation on the true side of that, and keep its stated census matching the shipped units.
"""
from __future__ import annotations

import json
import pathlib
import re

import numpy as np

REPO = pathlib.Path(__file__).resolve().parent.parent
STOCHASTIC = {"uniform", "log_normal", "lognormal", "pareto"}


def _units():
    for scenario in sorted((REPO / "units").glob("*/scenario.json")):
        card = scenario.parent / "card.toml"
        tier = None
        if card.exists():
            m = re.search(r'tier\s*=\s*["\']?([A-Za-z]+)', card.read_text(encoding="utf-8"))
            tier = m.group(1) if m else None
        yield scenario.parent.name, json.loads(scenario.read_text(encoding="utf-8")), tier


def test_the_documented_census_matches_the_shipped_units():
    """README states 65-of-66 and 42 Tier A. If a unit is added or a latency model changes,
    the prose goes stale silently -- so the prose is asserted against the units themselves."""
    rows = list(_units())
    stochastic = [u for u, sc, _ in rows
                  if (sc.get("latency_config") or {}).get("model") in STOCHASTIC]
    tier_a_stochastic = [u for u, sc, tier in rows
                         if (sc.get("latency_config") or {}).get("model") in STOCHASTIC
                         and (tier or "").upper().startswith("A")]
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert f"{len(stochastic)} of the {len(rows)} public scenarios" in readme, (
        f"README census is stale: measured {len(stochastic)} stochastic of {len(rows)} units"
    )
    assert f"{len(tier_a_stochastic)} of those are Tier A" in readme, (
        f"README census is stale: measured {len(tier_a_stochastic)} stochastic Tier-A units"
    )


def test_stochastic_latency_is_the_norm_not_the_exception():
    """The premise of the whole Tier-A note. If this ever inverts, the note needs rewriting
    rather than merely renumbering."""
    rows = list(_units())
    stochastic = sum(1 for _, sc, _ in rows
                     if (sc.get("latency_config") or {}).get("model") in STOCHASTIC)
    assert stochastic > len(rows) / 2, (stochastic, len(rows))


def test_the_latency_seed_depends_on_the_agent_count():
    """The mechanism the note describes, reproduced from config.py's own draw order.

    ``_draw_random_state`` consumes from the ONE global stream seeded by ``np.random.seed(seed)``:
    oracle (config.py:207), one per agent inside the count loop (:262), the latency model (:274),
    then the kernel (:278). Change the agent count and the latency model's seed moves.
    """
    def first_latency_draw(n_agents: int, seed: int = 42) -> int:
        np.random.seed(seed)
        def draw():
            return np.random.RandomState(
                seed=np.random.randint(low=0, high=2**32, dtype="uint64"))
        draw()                          # oracle
        for _ in range(n_agents):
            draw()                      # one per agent
        return int(draw().randint(0, 2**32))   # the latency model's stream

    base = first_latency_draw(50)
    assert first_latency_draw(50) == base, "same agent count must reproduce"
    for n in (49, 51, 100):
        assert first_latency_draw(n) != base, (
            f"{n} agents produced the same latency stream as 50 -- the coupling this note "
            "warns about would not exist, and the note should be removed"
        )


def test_the_participant_docs_do_not_claim_seed_alone_gives_determinism():
    """The corrected claim. `regression_suite/README.md` told participants 'The scenario is
    deterministic given its seed', which is true of the reference implementation and false of
    theirs -- the exact misreading that makes a correct simulator fail Tier A."""
    body = (REPO / "regression_suite" / "README.md").read_text(encoding="utf-8")
    assert "The scenario is deterministic given its seed" not in body
    assert "not across implementations" in body


def test_the_tier_a_note_tells_participants_what_they_may_change():
    """A warning that does not say what to do instead is not a framework."""
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "preserve the reference's random-number draw sequence" in readme
    assert "one draw per agent" in readme
    # ...and it must say what optimization REMAINS legal, or it reads as "do not optimize".
    assert "Vectorize the matching engine" in readme
