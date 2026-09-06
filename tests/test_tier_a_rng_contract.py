"""Execute baseline seed assignment behind the public Tier-A guidance.

Only ABIDES constructors are lightweight stand-ins. The actual build_config,
_draw_random_state and ScenarioLatencyModel code executes; no simulator is run.
All scenario fixtures and the census are public or synthetic.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tomllib
import types

import numpy as np
import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
STOCHASTIC = {"uniform", "log_normal", "pareto"}


@pytest.fixture
def baseline_config(monkeypatch):
    """Import the shipped configuration with only external ABIDES objects replaced."""
    saved_state = np.random.get_state()

    def construct(*args, **kwargs):
        return types.SimpleNamespace(args=args, **kwargs)

    class LatencyBase:
        def __init__(self, *, random_state, **kwargs):
            self.random_state = random_state

    def unused_default_latency(*args, **kwargs):
        raise AssertionError("the synthetic scenario supplies its latency configuration")

    modules = {
        "abides_core": {},
        "abides_core.latency_model": {"LatencyModel": LatencyBase},
        "abides_core.utils": {"str_to_ns": lambda value: 0},
        "abides_markets": {},
        "abides_markets.agents": {"ExchangeAgent": construct},
        "abides_markets.oracles": {"SparseMeanRevertingOracle": construct},
        "abides_markets.utils": {"generate_latency_model": unused_default_latency},
        "abides_fork": {},
        "abides_fork.agents": {"AGENT_REGISTRY": {"NoiseTrader": construct}},
    }
    for name, members in modules.items():
        module = types.ModuleType(name)
        module.__dict__.update(members)
        monkeypatch.setitem(sys.modules, name, module)
    source = REPO / "baselines" / "abides_fork" / "config.py"
    spec = importlib.util.spec_from_file_location("_rng_contract_config", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert pathlib.Path(module.__file__).resolve() == source.resolve()
    try:
        yield module
    finally:
        np.random.set_state(saved_state)


def scenario(n_agents):
    return {
        "seed": 42,
        "exchange_config": {},
        "oracle_config": {"params": {}},
        "horizon_ns": 1_000_000_000,
        "agent_configs": [{"agent_type": "NoiseTrader", "count": n_agents}],
        "latency_config": {
            "model": "uniform",
            "params": {"min_ns": 1_000, "max_ns": 1_000_000},
        },
    }


def initial_seed(random_state):
    return int(random_state.get_state()[1][0])


def test_stochastic_latency_is_the_norm_in_public_single_scenarios():
    rows = []
    for path in sorted((REPO / "units").glob("*/scenario.json")):
        card = tomllib.loads((path.parent / "card.toml").read_text())
        tier = card["scoring"]["params"]["semantic_tier"]
        assert tier in {"A", "B"}
        rows.append((json.loads(path.read_text()), tier))
    assert rows
    stochastic = [sc for sc, _ in rows if sc.get("latency_config", {}).get("model") in STOCHASTIC]
    assert len(stochastic) > len(rows) / 2
    assert any(tier == "A" and sc.get("latency_config", {}).get("model") in STOCHASTIC
               for sc, tier in rows)


def test_actual_config_assigns_oracle_exchange_agents_latency_then_kernel(baseline_config):
    count = 50
    config = baseline_config.build_config(scenario(count))
    oracle_state = config["custom_properties"]["oracle"].args[2]["ABM"]["random_state"]
    agent_states = [agent.random_state for agent in config["agents"]]
    assert len(agent_states) == count + 1  # the exchange is also an agent
    observed = [initial_seed(state) for state in [
        oracle_state, *agent_states, config["agent_latency_model"].random_state,
        config["random_state_kernel"],
    ]]
    expected = np.random.RandomState(42).randint(0, 2**32, size=count + 4, dtype="uint64")
    assert observed == [int(value) for value in expected]


def test_actual_latency_repeats_but_changes_with_agent_seed_assignment(baseline_config):
    def first_latency(count):
        config = baseline_config.build_config(scenario(count))
        return config["agent_latency_model"].get_latency(0, 1)

    original = first_latency(50)
    assert first_latency(50) == original
    assert first_latency(49) != original
    assert first_latency(51) != original


def test_actual_config_clock_remains_nanoseconds(baseline_config):
    config = baseline_config.build_config(scenario(1))
    # 2021-02-05 midnight UTC, the baseline epoch; independent integer unit check.
    assert config["start_time"] == 1_612_483_200_000_000_000
    assert config["stop_time"] == config["start_time"] + 1_000_000_000


def test_participant_guidance_distinguishes_seed_from_trace():
    readme = (REPO / "README.md").read_text()
    assert "A shared seed alone does not" in readme
    assert "separate random states" in readme
    assert "exchange agent" in readme
    regression = (REPO / "regression_suite" / "README.md").read_text()
    assert "The scenario is deterministic given its seed" not in regression
    assert "checks inspect the emitted trace" in regression


def test_guidance_keeps_output_preserving_rng_optimizations_eligible():
    readme = (REPO / "README.md").read_text()
    assert "different RNG implementation are allowed" in readme
    assert "Those checks\n> inspect outputs" in readme
    assert "Do not change the RNG" not in readme
    concepts = (REPO / "docs" / "CONCEPTS.md").read_text()
    assert "does not inspect or mandate a\nparticular RNG implementation" in concepts
