"""Malicious participant output is refused before a Track 3 parser sees it.

Two halves.

**The shared corpus.** ``qfbench2_common.contracts.fixtures.make_malicious_trees`` generates the
19-case adversarial set every workstream in this program shares, with each case's expected verdict
as data rather than prose. Track 3 runs it rather than inventing a twentieth corpus, because a case
one repo omits is exactly the case that ships.

**Track 3's own allowlist and retention path.** The local harness used to copy the run's output
with ``shutil.copytree(out_dir, keep_output)`` and the default ``symlinks=False``, which flattens a
participant symlink and copies its TARGET's bytes into the retained tree. Retention now goes
through the shared C3 sanitizer with Track 3's exact allowlist.

### Test-environment caveat, binding on acceptance

``case-collision`` and ``unicode-collision`` **cannot be constructed on macOS**: APFS folds case
and normalises Unicode, so the second name in each pair resolves to the first and there is no
collision to refuse. The generator reports them as unbuildable with that reason and this file skips
them with the reason attached. **They must be exercised in Linux CI**, and A06 may not be closed on
the strength of a local green run.

    python -m pytest tests/test_malicious_output.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qfbench2_common.contracts.fixtures import make_malicious_trees as CORPUS  # noqa: E402
from qfbench2_common.sanitize import TreeRefused, sanitize_participant_tree  # noqa: E402

from qfbench2_track_simulation.limits import (  # noqa: E402
    SINGLE_UNIT_FILES,
    allowed_paths_for,
)


#: Corpus cases whose payload is a WELL-FORMED FILE with bad CONTENT. The tree sanitizer accepts
#: them by design -- it validates node types, paths, sizes and links, and does not parse -- so their
#: REJECT verdict belongs to the parser, and the corpus rationale says so: "must be a deterministic
#: PARTICIPANT failure, not an organizer fault and not an uncaught exception". Track 3's parser-level
#: coverage of the same two conditions is in `test_scoring_gate.py` (corrupt parquet contained to one
#: unit) and `test_semantics_exactness.py` (NaN / infinity / non-physical values refused).
#: Listed explicitly rather than skipped by a broad `try`, so a case that starts passing the
#: sanitizer for a NEW reason still turns this file red.
CONTENT_LEVEL_CASES = frozenset({"malformed-json", "nonfinite-json"})


def _corpus(root: Path) -> list[dict[str, object]]:
    return CORPUS.build(root / "corpus")


def _sanitize(res_dir: Path, destination: Path, allowed: object = None) -> None:
    sanitize_participant_tree(
        res_dir,
        destination,
        allowed_paths=allowed,  # type: ignore[arg-type]
        require_nonempty=True,
    )


# --------------------------------------------------------------------------- shared corpus
def test_the_shared_adversarial_corpus_is_refused_case_by_case() -> None:
    """Every REJECT case is refused and the one ACCEPT case survives.

    The positive control is the load-bearing half: a corpus in which everything is rejected proves
    nothing, because a sanitizer that refuses all input would pass it.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        manifest = _corpus(root)
        assert manifest, "the shared corpus generator produced no cases"
        unbuildable: list[str] = []
        checked = 0
        for entry in manifest:
            case = str(entry["case"])
            if not entry["built"]:
                unbuildable.append(f"{case}: {entry['unbuildable_reason']}")
                continue
            res = root / "corpus" / str(entry["res_dir"])
            # An exclusive parent per case: `sanitize_participant_tree` emits a C3 descriptor,
            # and C3 requires the sanitized root's parent to hold nothing else.
            destination = root / "sanitized" / case / "tree"
            destination.parent.mkdir(parents=True, exist_ok=True)
            if case in CONTENT_LEVEL_CASES:
                # Accepted as a TREE and refused by the parser; see CONTENT_LEVEL_CASES.
                _sanitize(res, destination)
                checked += 1
                continue
            if entry["expect"] == CORPUS.REJECT:
                with pytest.raises((TreeRefused, Exception)) as caught:
                    _sanitize(res, destination)
                assert CORPUS.SENTINEL not in str(caught.value), (
                    f"{case}: the refusal message quoted the reference sentinel"
                )
                assert not destination.exists(), f"{case}: a refused tree was still promoted"
            else:
                _sanitize(res, destination)
                assert destination.is_dir(), f"{case}: the positive control was refused"
            checked += 1
        assert checked >= 17, f"too few corpus cases ran: {checked}"
        # The two APFS cases, named rather than silently passed.
        assert len(unbuildable) <= 2, unbuildable
        for reason in unbuildable:
            assert "case-insensitive" in reason or "normalises Unicode" in reason, reason


@pytest.mark.skipif(
    sys.platform == "darwin",
    reason="APFS folds case and normalises Unicode, so neither collision can be constructed on "
    "this host. These two cases must be exercised in Linux CI; a local green run does not close "
    "them.",
)
def test_case_and_unicode_collisions_are_refused_on_a_case_sensitive_filesystem() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        manifest = {str(e["case"]): e for e in _corpus(root)}
        for case in ("case-collision", "unicode-collision"):
            entry = manifest[case]
            assert entry["built"], f"{case} should be buildable on this filesystem"
            res = root / "corpus" / str(entry["res_dir"])
            destination = root / "sanitized" / case / "tree"
            destination.parent.mkdir(parents=True, exist_ok=True)
            with pytest.raises(Exception):
                _sanitize(res, destination)


def test_the_reference_sentinel_never_reaches_a_sanitized_tree() -> None:
    """The firewall property, asserted rather than assumed: nothing the sanitizer promotes may
    contain the reference tree's contents, whatever link the participant planted."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        manifest = _corpus(root)
        for entry in manifest:
            if not entry["built"] or entry["expect"] != CORPUS.REJECT:
                continue
            if str(entry["case"]) in CONTENT_LEVEL_CASES:
                continue
            res = root / "corpus" / str(entry["res_dir"])
            destination = root / "out" / str(entry["case"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                _sanitize(res, destination)
            except Exception:  # noqa: BLE001 - refusal is the expected outcome
                pass
        for path in (root / "out").rglob("*"):
            if path.is_file():
                assert CORPUS.SENTINEL not in path.read_bytes().decode(
                    "utf-8", errors="replace"
                ), path


# --------------------------------------------------------------------------- Track 3 allowlist
def test_the_track_allowlist_is_exactly_the_three_outputs_and_the_profile_sidecar(tmp_path: Path) -> None:
    assert allowed_paths_for(tmp_path / "no-batch-json-here") == SINGLE_UNIT_FILES
    assert set(SINGLE_UNIT_FILES) == {"trace.parquet", "events.json", "message_trace.parquet",
                                      "profile.json"}


def test_the_profiling_sidecar_survives_sanitation_in_both_unit_shapes(tmp_path: Path) -> None:
    """docs/PROFILING.md says "drop profile.json next to your trace.parquet in /output"; before
    2026-09-02 the allowlist did not name it and the sanitizer dropped it silently, so the Best
    Systems Diagnosis award had no compliant path (track3-simulation-private#28)."""
    import json
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "trace.parquet").write_bytes(b"PAR1")
    (raw / "profile.json").write_text('{"gpu_util": 0.5}')
    clean = tmp_path / "exclusive" / "clean"
    clean.parent.mkdir(parents=True)
    _sanitize(raw, clean, SINGLE_UNIT_FILES)
    assert sorted(p.name for p in clean.iterdir()) == ["profile.json", "trace.parquet"]
    unit = tmp_path / "unit"
    unit.mkdir()
    (unit / "batch.json").write_text(json.dumps({"n": 1, "subs": [{"sub": "sub_00"}]}))
    allowed = allowed_paths_for(unit)
    assert "sub_00/profile.json" in allowed
    assert "profile.json" not in allowed, "a batch root carries no sidecar; each sub does"


def test_a_batch_allowlist_comes_from_the_organizer_declaration(tmp_path: Path) -> None:
    import json

    unit = tmp_path / "unit"
    unit.mkdir()
    (unit / "batch.json").write_text(
        json.dumps({"n": 2, "subs": [{"sub": "sub_00"}, {"sub": "sub_01"}]})
    )
    allowed = allowed_paths_for(unit)
    assert "batch_events.json" in allowed
    assert "sub_00/trace.parquet" in allowed
    assert "sub_01/message_trace.parquet" in allowed
    # A directory the SUBMISSION invents is not in the allowlist, so it cannot widen its own set.
    assert not any(p.startswith("sub_99/") for p in allowed)


def test_an_output_file_outside_the_allowlist_is_refused(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "trace.parquet").write_bytes(b"PAR1")
    (raw / "surprise.sh").write_text("#!/bin/sh\n")
    with pytest.raises(TreeRefused):
        _sanitize(raw, tmp_path / "exclusive" / "clean", SINGLE_UNIT_FILES)


def test_the_allowed_set_alone_survives(tmp_path: Path) -> None:
    """Positive control for the allowlist."""
    raw = tmp_path / "raw"
    raw.mkdir()
    for name in SINGLE_UNIT_FILES:
        (raw / name).write_bytes(b"x")
    clean = tmp_path / "exclusive" / "clean"
    clean.parent.mkdir(parents=True)
    _sanitize(raw, clean, SINGLE_UNIT_FILES)
    assert sorted(p.name for p in clean.iterdir()) == sorted(SINGLE_UNIT_FILES)


# --------------------------------------------------------------------------- retention path
def test_retained_output_refuses_a_planted_symlink(tmp_path: Path) -> None:
    """Measured pre-fix: the symlink was flattened and the TARGET's bytes were copied into the
    retained tree. Verified locally to reach into ``/etc`` before permission errors stopped it."""
    from throughput.run_unit import retain_output

    secret = tmp_path / "organizer-secret.txt"
    secret.write_text("reference bytes the participant must not obtain\n")
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "events.json").write_text("{}")
    (raw / "trace.parquet").symlink_to(secret)
    unit = tmp_path / "unit"
    unit.mkdir()
    with pytest.raises(TreeRefused):
        retain_output(raw, tmp_path / "kept", unit)
    assert not (tmp_path / "kept").exists()


def test_a_dangling_symlink_does_not_turn_a_good_run_into_a_harness_crash(
    tmp_path: Path,
) -> None:
    """``shutil.copytree`` RAISED on a dangling link, converting a successful timed run into a
    harness exception. It is now an ordinary, typed refusal."""
    from throughput.run_unit import retain_output

    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "events.json").write_text("{}")
    (raw / "trace.parquet").symlink_to(tmp_path / "nowhere-at-all")
    unit = tmp_path / "unit"
    unit.mkdir()
    with pytest.raises(TreeRefused):
        retain_output(raw, tmp_path / "kept", unit)


def test_a_clean_run_is_retained(tmp_path: Path) -> None:
    """Positive control for retention."""
    from throughput.run_unit import retain_output

    raw = tmp_path / "raw"
    raw.mkdir()
    for name in SINGLE_UNIT_FILES:          # every allowed output, the sidecar included
        (raw / name).write_bytes(b"{}" if name.endswith(".json") else b"PAR1")
    unit = tmp_path / "unit"
    unit.mkdir()
    kept = tmp_path / "kept"
    retain_output(raw, kept, unit)
    assert sorted(p.name for p in kept.iterdir()) == sorted(SINGLE_UNIT_FILES)
    # Hashes are of the COPIES, and no link of any kind survived.
    assert not any(p.is_symlink() for p in kept.rglob("*"))


def test_a_file_over_the_shared_64_mib_default_is_retained(tmp_path: Path) -> None:
    """Track 3 allows 256 MiB per file, so a 65 MiB ledger is kept, not refused."""
    import os

    from throughput.run_unit import retain_output

    from qfbench2_track_simulation.limits import MAX_OUTPUT_BYTES

    assert MAX_OUTPUT_BYTES == 256 * 1024 * 1024
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "trace.parquet").write_bytes(b"PAR1")
    (raw / "events.json").write_bytes(b"{}")
    (raw / "message_trace.parquet").write_bytes(os.urandom(65 * 1024 * 1024))
    unit = tmp_path / "unit"
    unit.mkdir()
    kept = tmp_path / "kept"
    retain_output(raw, kept, unit)
    assert (kept / "message_trace.parquet").stat().st_size == 65 * 1024 * 1024


def test_the_local_harness_applies_track_3s_256_mib_limits(tmp_path: Path, monkeypatch) -> None:
    """Per file and in total, the same 256 MiB as the platform, not more and not the 64 MiB default."""
    import qfbench2_common.sanitize as sanitize

    from throughput.run_unit import retain_output

    seen = {}

    def spy(*args, **kwargs):
        seen["limits"] = kwargs.get("limits")
        raise RuntimeError("stop after capturing the limits")

    monkeypatch.setattr(sanitize, "materialize_tree", spy)
    raw = tmp_path / "raw"
    raw.mkdir()
    unit = tmp_path / "unit"
    unit.mkdir()
    try:
        retain_output(raw, tmp_path / "kept", unit)
    except RuntimeError:
        pass
    limits = seen["limits"]
    assert (limits.max_file_bytes, limits.max_total_bytes) == (256 * 1024 * 1024, 256 * 1024 * 1024)


def _cleanup(root: Path) -> None:  # pragma: no cover - helper for the script runner
    shutil.rmtree(root, ignore_errors=True)
