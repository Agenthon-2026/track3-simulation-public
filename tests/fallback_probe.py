import copy
import json
import os
import sys
import numpy as np

sys.path.insert(0, "/opt")
from fast_sim import engine, native
from abides_core.message import Message
from abides_markets.orders import Order

scenario = json.load(open("/scenario.json"))
os.environ["FAST_SIM_NATIVE"] = "hybrid"
a, b, _ = engine.run_scenario(copy.deepcopy(scenario))
original = native.run_native


def fail(config):
    # Failure after the native path has consumed state and allocated IDs.
    config["agents"][1].random_state.rand(100)
    config["agent_latency_model"].random_state.rand(100)
    np.random.rand(100)
    Order._order_id_counter = 10000
    setattr(Message, "_Message__message_id_counter", 10000)
    raise RuntimeError("injected mid-run failure")


native.run_native = fail
os.environ["FAST_SIM_NATIVE"] = "native"
c, d, _ = engine.run_scenario(copy.deepcopy(scenario))
print(json.dumps({"trace_equal": a.equals(c), "ledger_equal": b.equals(d)}))
assert a.equals(c) and b.equals(d), "fallback reused mutated state"
