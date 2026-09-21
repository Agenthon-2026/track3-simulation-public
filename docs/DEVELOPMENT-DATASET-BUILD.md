# Building a selected Development dataset

## Executive summary (read this first)

The organizer can build an explicit list of public Development units with `--roster`. The list
uses directory names, one per line, in the intended order. The builder checks the entire list
before writing a candidate. It replaces the existing output only after every selected unit passes
the shared toolkit's splitting and leak checks. Omitting `--roster` keeps the existing behavior:
select every immediate unit directory in alphabetical order.

For example, an organizer's proposed roster could contain:

```text
# Immediate directory names under --units; blank lines are allowed.
t3-unit-a
t3-unit-b
```

Build it with:

```bash
python scripts/build_dev_dataset.py \
  --units ./units --roster ./proposed-roster.txt --out ./build/development
```

These example names are placeholders. Selecting a roster does not approve it, change the
competition's active dataset or publish anything. Use the approved roster for an actual release.

## Selection and validation

Each handle must name one existing immediate directory under `--units`. Handles may contain
ASCII letters, digits, dots, underscores and hyphens and must begin with a letter or digit.
Paths, duplicate handles, empty selections and selected units without `card.toml` are refused.
Only selected directories are inspected and copied when an explicit roster is supplied.

Selected trees must contain only regular files and directories. Links and special files are
refused. Input and output paths must not overlap, and their path components may not be symlinks.
An existing output must also be a regular directory tree. On systems where `/tmp` is a symlink,
use its real path or a directory under the checkout.

## Output and failure behavior

The generated output contains:

- `ingestion/input/ref/`: selected units with declared answer paths removed, for participant input.
- `scoring/input/ref/`: the complete selected units, for the grader.
- `build-roster.json`: the exact ordered directory handles used by this build.

The builder uses `qfbench2_common.dataset.split_unit` for answer removal and the per-unit leak
gate. The roster file does not change those shared rules or establish scoring compatibility. It
also does not create a signed evaluation plan or verify a whole-roster cross-unit leak audit.
The builder refuses a selection with zero stripped answer paths across the whole set; such a
build cannot replace a previous artifact or create a new output. This guard does not validate
the reference format or prove that each selected unit has every reference its scorer needs.

Builds use a temporary sibling of `--out`. Existing unrelated output metadata is copied into
that candidate, while both old generated unit trees are replaced. A leak, copy failure or other
build error discards the candidate and leaves the previous output intact. Promotion uses two
directory renames with rollback on ordinary errors. There is a brief interval with the old tree
at a `.previous-*` sibling; do not build concurrently or serve the directory during promotion.
An uncatchable process or host failure in that interval can require restoring that retained
sibling manually. This is not a crash-atomic filesystem exchange.

Review and regenerate any copied signed-plan or trust metadata for the new roster before
uploading. `build-roster.json` is build evidence, not a signed plan. Upload the ingestion tree as
input data and the scoring tree as reference data; the scoring tree contains the answers.
