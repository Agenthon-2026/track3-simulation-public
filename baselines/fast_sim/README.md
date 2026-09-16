# fast_sim — Track 3 participant simulator

`fast_sim` accelerates the pinned ABIDES simulation while preserving event order,
random draws, fills and message causality. Supported agents use a native Cython
event loop; other configurations use the hybrid ABIDES path. The implementation
uses the CPU. Candidate qualification is defined in
[Submission Readiness](../../docs/SUBMISSION-READINESS.md).

## Execution

`engine.py` builds a fresh config and resets ABIDES counters for each run.
`native.py` snapshots parameters and independent RNG streams into `_native.pyx`;
`FAST_SIM_NATIVE=0` selects the hybrid path for diagnostics. A native exception
restarts the hybrid path from clean state. Storage and allocation failures propagate
because repeating the same workload cannot repair them.

The native loop preserves `(deliver_at, sender_id, recipient_id, message_id)`
ordering, insertion order of working orders, nanosecond integer timestamps and
ABIDES random draw order. Fill labels refer to the last delivered execution of
an order, including reordered fill/cancel deliveries.

Large workloads classify executions first, then replay into bounded Parquet row
groups. `streaming.py` owns compression and schema metadata; ledger nullable integers
must survive pandas reload without conversion to float. The workload dispatch is
parameter-based, never based on scenario IDs.

Single throughput-scale scenarios may omit the optional ledger while retaining the
complete transaction trace and actual message count. Batch and unknown families
retain ledgers; `--require-message-ledger` forces one for diagnostics. The participant
receives scenario JSON, so a hidden card override must be conveyed by the launch
interface. Local validation still rejects a missing ledger when its card requires it.

## Build and check

Build the root `Dockerfile` for the desired architecture. Rebuild a challenge on an
immutable parent with `baselines/Dockerfile.native-candidate`; commands and pinned
image handling are in [CANDIDATE-RUNBOOK.md](../../docs/CANDIDATE-RUNBOOK.md).

```bash
docker run --rm --network none --cpus 4 --memory 4g \
  -v "$PWD:/workspace:ro" "$IMAGE" \
  python /workspace/tests/integration/native_streaming_check.py
```

The integration suite checks buffered versus streamed output, reordering, optional
ledgers, repeat bytes and failure recovery. Independent parameter comparisons use
the pinned ABIDES reference adapter, not the candidate's own native implementation.
Only complete image-bound G1/G2/G3 evidence establishes B0/B1. Historical best-run
rates and implementation stages remain in Git history, not a current performance claim.
