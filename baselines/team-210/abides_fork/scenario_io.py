"""Scenario-file resolution for the ``simulate`` verb.

Deliberately stdlib-only and free of any ABIDES, numpy or pandas import, so the dispatch
contract it encodes can be tested in CI without building the baseline image.

The contract: Track 3 has **two** verbs, and which one a unit takes is a property of the unit.

* a **single-scenario** unit has a top-level ``scenario.json`` and takes
  ``simulate --config /input/scenario.json --out /output/trace.parquet``;
* a **batched** unit (family GB, the six ``t3-gbatch-*`` public dev units) has ``batch.json`` +
  ``scenarios/`` and **no** ``scenario.json``, and takes
  ``simulate-batch --batch-dir /input/scenarios --out-dir /output``.

Running the wrong verb on a batched unit used to surface as a bare ``FileNotFoundError`` on
``/input/scenario.json`` — a path nobody chose, which reads as a corrupt unit rather than as a
harness dispatching the wrong verb.
"""

from __future__ import annotations

import pathlib


class ScenarioNotFoundError(FileNotFoundError):
    """``--config`` does not exist; the message names the likely cause."""


def is_batch_unit(unit_dir: str | pathlib.Path) -> bool:
    """True for a batched (family GB) unit.

    Detection is by unit CONTENT — ``batch.json`` **and** ``scenarios/`` — never by the
    ``t3-gbatch-`` name prefix, so a renamed or newly authored batch unit is classified correctly
    and a single-scenario unit never can be. The ingestion program uses the same rule.
    """
    unit = pathlib.Path(unit_dir)
    return (unit / "batch.json").is_file() and (unit / "scenarios").is_dir()


def read_scenario(config_path: str | pathlib.Path) -> str:
    """Return the text of ``--config``, or raise with the cause named."""
    path = pathlib.Path(config_path)
    if path.is_file():
        return path.read_text()

    unit = path.parent
    if is_batch_unit(unit):
        raise ScenarioNotFoundError(
            f"{path} does not exist, and {unit} is a BATCHED unit (it has batch.json + "
            f"scenarios/). Batched units carry no scenario.json: run them with the batch verb, "
            f"`simulate-batch --batch-dir {unit / 'scenarios'} --out-dir <out>`. If the harness "
            f"issued `simulate` for this unit, the harness is dispatching the wrong verb."
        )
    listing = (
        sorted(p.name for p in unit.iterdir())
        if unit.is_dir()
        else "nothing (no such directory)"
    )
    raise ScenarioNotFoundError(
        f"{path} does not exist. `simulate --config` expects a single scenario JSON file; "
        f"{unit} contains: {listing}."
    )
