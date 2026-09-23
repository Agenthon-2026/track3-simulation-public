# Examples — documentation only, not part of the roster

Nothing in this directory is a scored unit. `scripts/build_dev_dataset.py` reads `units/`
only, so anything here is invisible to the Development dataset, to the semantic regression
run and to the leaderboard.

## `t3-EXAMPLE-vectorized-matching/`

The worked exemplar. It shows what a Track 3 unit looks like — card, scenario and a long
README explaining the throughput-scale family — and it ships no reference trace, so there
has never been anything to check a submission against.

It used to sit in `units/`, which put it on the Development roster. It was withdrawn from
that roster; the scored Development roster is the 71 units under `units/`. Read it, copy
its shape when you author or profile your own scenarios, and do not run it as part of a
sweep: its declared horizon is far beyond what its own `[environment]` limits allow.
