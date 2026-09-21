## Executive summary (read this first)

This is the candidate consumer contract for official repeated Track 3 measurements. Honest
`events.json` timing values may change between repeats while trace and message-ledger bytes must
reproduce. The consumer checks signed stable-file evidence against the retained sanitized output.
It still requires the final repeat's full output-tree digest to equal the retained C2/C3 binding.
These code changes do not activate a production timing service or change provisional Development.

### Producer and consumer call the same policy

Both use `limits.stable_repeat_policy_for(organizer_unit_dir)` and pass its result to the shared
`qfbench2_common.contracts.stable_output_binding(sanitized_output_dir, **policy)` helper.
The policy identifier is `t3-stable-output-v1`. Its members are the existing `STABLE_OUTPUT_FILES`.

- Single market: `trace.parquet` is required. `message_trace.parquet` follows the existing card
  declaration and family default. When optional, its presence still changes the content digest.
- Batch: every sub declared in the organizer's `batch.json` requires both stable files. A caller
  cannot supply a shorter sub list, and a participant-created directory cannot enlarge it.
- Card/batch disagreement, missing organizer configuration or invalid sub paths abort as organizer
  faults. The policy is never read from participant output or accepted as a context override.

### Every repeat, including warmups

The producer places the binding in each C2 repeat before signing. It also calls
`telemetry.validate_repeat_sidecars(unit_dir, output_dir, output_row_counts)` for each immutable
sanitized repeat. This checks the volatile sidecars' structure, scenario, seed, count, trace hash
and rate arithmetic. Batch checks include every sub's sidecar and the aggregate's exact coverage.
`output_row_counts` must come from Runner-read parquet footers. Full semantic regression remains
the scorer's responsibility; equal stable bytes carry that retained result to the other repeats.

The consumer checks stable content and counts for all repeats under `every_repeat_must_pass=true`.
Discarded warmups do not enter the median, but their content, counts and positive host timing are
still required. The last repeat is the retained one in this candidate protocol. Earlier full-tree
digests may differ because their timing sidecars differ; the last must equal the retained C2
`sanitized_tree_digest`. Neither whole-tree field is repurposed or removed.

Missing bindings, wrong or mixed policy digests, invalid host evidence and a broken retention
anchor are organizer faults. Divergent stable content/counts and missing participant artifacts
are participant failures. The shared driver still verifies C1/C2 signatures and production trust.
The track additionally checks that new bindings belong to the attested payload; a matching payload
digest by itself is not cryptographic signature verification.

### Release and compatibility

Legacy C2 records remain parseable. They cannot enter this candidate official consumer without
the new evidence. The separately named developer factory remains unrankable and does not require
these repeat bindings. Release requires the compatible hub reader, producer and consumer together,
plus actual host rehearsal. The local synthetic tests use the published development signing key
and real temporary parquet files; they provide no production timing or deployment evidence.

## Publication dependency

Private staging CI tests the exact compatible private Hub revision. The public workflow
keeps its anonymous installation path. Before this candidate is published, release the
matching toolkit, update the public toolkit tag in CI and participant installation docs,
and rerun the checks against that public tag. A private staging merge is not public
package compatibility evidence or permission to change an active evaluation.
