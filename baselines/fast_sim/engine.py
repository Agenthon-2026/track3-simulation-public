"""Run one scenario through patched ABIDES and return traces + kernel end_state."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


Kernel = None


def reset_abides_counters() -> None:
    """Reset ABIDES class-level id counters so a run is deterministic in-process."""
    from abides_core.message import Message
    from abides_markets.orders import Order

    Order._order_id_counter = 0
    setattr(Message, "_Message__message_id_counter", 1)


def run_scenario(scenario: dict[str, Any], output_paths=None) -> tuple[Any, Any, dict[str, Any]]:
    """Execute ``scenario`` and return ``(trace_df, message_trace_df, end_state)``.

    The kernel, matching engine, agents, oracle and latency model are the pinned
    ABIDES stack. The event queue is a compact C min-heap (Python heapq fallback)
    with the same comparison key ABIDES uses:
    ``(deliver_at, (sender_id, recipient_id, message))`` / ``Message.__lt__``
    by ``message_id``. Delivery order is unchanged.

    Default native rosters take a light boot (numpy-only spec) so cold ``simulate``
    does not pay pandas / ABIDES agent construction. The light boot is a
    streaming/file-output path only: in-memory callers (``output_paths=None``),
    hybrid mode and unsupported agents still go through ``build_config``, which
    keeps ``native.run_native`` as the single interception point for native
    failure injection and fallback.
    """
    from fast_sim.native import (
        NATIVE_IS_DEFAULT,
        native_flag,
        run_native_from_spec,
    )
    from fast_sim.native_boot import scenario_supports_native, spec_from_scenario

    # Light path: rebuild the native spec with numpy alone (no pandas / ABIDES
    # agent construction). Same RNG draw order as ``build_config`` + oracle init.
    flag = native_flag()
    light_ok = (
        output_paths is not None
        and scenario_supports_native(scenario)
        and flag not in ("0", "false", "hybrid", "off")
        and (flag in ("1", "true", "native", "on") or NATIVE_IS_DEFAULT)
    )
    if light_ok:
        try:
            spec = spec_from_scenario(deepcopy(scenario))
            return run_native_from_spec(spec, output_paths)
        except (OSError, MemoryError):
            raise
        except Exception as exc:
            import logging

            logging.getLogger("fast_sim").warning(
                "Native light boot failed (%s); falling back to build_config path",
                exc,
            )

    return _run_scenario_via_build_config(scenario, output_paths)


def _run_scenario_via_build_config(
    scenario: dict[str, Any], output_paths=None
) -> tuple[Any, Any, dict[str, Any]]:
    """Heavy path: ABIDES ``build_config`` + native or hybrid kernel."""
    global Kernel
    if Kernel is None:
        from abides_core.kernel import Kernel as _Kernel

        Kernel = _Kernel
    import numpy as np
    from abides_core.utils import subdict
    from abides_fork.config import build_config

    from fast_sim.columns import ColumnLedger, ColumnTrace
    from fast_sim.extract import extract_message_trace_from_state, extract_trace_from_agents
    from fast_sim.native import run_native, should_use_native
    from fast_sim.optimize import HeapPQueue, apply_runtime_patches, slim_agents, slim_exchange

    try:
        from fast_sim._hotpath import CLedger, CTrace, EventQueue
    except ImportError:
        try:
            from fast_sim.hotpath import EventQueue
        except ImportError:
            EventQueue = HeapPQueue
        CLedger = ColumnLedger
        CTrace = ColumnTrace

    apply_runtime_patches()
    reset_abides_counters()

    # build_config and the native path may mutate nested oracle parameters.
    # Keep the caller input pristine so exception recovery starts from the same seed.
    config = build_config(deepcopy(scenario))
    agents = config["agents"]
    slim_exchange(agents[0])
    slim_agents(agents)

    if should_use_native(agents):
        try:
            return run_native(config) if output_paths is None else run_native(config, output_paths)
        except (OSError, MemoryError):
            # Resource/storage failures cannot be repaired by rerunning the same
            # workload through the more memory-intensive hybrid implementation.
            raise
        except Exception as exc:
            import logging

            logging.getLogger("fast_sim").warning(
                "Native C kernel failed on scenario (%s); safely falling back to hybrid path",
                exc,
            )
            reset_abides_counters()
            config = build_config(deepcopy(scenario))
            agents = config["agents"]
            slim_exchange(agents[0])
            slim_agents(agents)

    # abides_core.abides.run ignores config["random_state_kernel"] and constructs
    # Kernel(random_state=RandomState(seed=0)). Match that exactly so any latent
    # kernel RNG use (legacy latency-noise path) stays aligned with the references.
    kernel = Kernel(
        random_state=np.random.RandomState(seed=0),
        log_dir="",
        skip_log=True,
        **subdict(
            config,
            [
                "start_time",
                "stop_time",
                "agents",
                "agent_latency_model",
                "default_computation_delay",
                "custom_properties",
            ],
        ),
    )
    kernel.messages = EventQueue()
    kernel.show_trace_messages = False
    kernel._col_ledger = CLedger()
    kernel._col_trace = CTrace()
    kernel._pending_ledger = {}
    kernel._delivered = []

    end_state = kernel.run()
    if "message_ledger" not in end_state and hasattr(kernel, "_msg_ledger"):
        end_state["message_ledger"] = kernel._msg_ledger
        end_state["deliver_seq_by_key"] = kernel._deliver_seq_by_key
    if hasattr(kernel, "_delivered"):
        end_state["delivered_ledger"] = kernel._delivered
    end_state["col_ledger"] = kernel._col_ledger
    end_state["col_trace"] = kernel._col_trace
    if "agents" not in end_state:
        end_state["agents"] = agents

    trace = (
        kernel._col_trace.to_dataframe()
        if kernel._col_trace
        else extract_trace_from_agents(agents)
    )
    message_trace = extract_message_trace_from_state(end_state)
    return trace, message_trace, end_state
