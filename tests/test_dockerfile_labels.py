"""Every Dockerfile this repository ships carries the submission interface label in its final stage.

    python tests/test_dockerfile_labels.py

SUBMISSION_CLI.md requires ``LABEL qfbench2.interface_version="2.0"`` on every submission image, and
the platform refuses an image without it before it runs. The baseline always had it; the GPU
starter, which participants copy, did not. A ``LABEL`` in an earlier build stage does not reach the
image, so the label must sit after the last ``FROM``.

Stdlib-only, so it runs in the firewall job with the other no-secret guards.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
LABEL_KEY = "qfbench2.interface_version"
LABEL_VALUE = "2.0"
#: The Dockerfiles the repository shipped when this guard was added. The discovery below must find
#: at least these, so a change to the walk cannot make the guard pass by finding nothing.
KNOWN = {
    "baselines/Dockerfile",
    "baselines/gpu_starter/Dockerfile",
    "baselines/gpu_starter/Dockerfile.verify",
}


def dockerfiles(root: Path = _REPO) -> list[Path]:
    """Every file named Dockerfile, Dockerfile.<x> or <x>.Dockerfile, outside dot-directories."""
    found = []
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if any(part.startswith(".") for part in rel.parts) or not path.is_file():
            continue
        name = path.name
        if name == "Dockerfile" or name.startswith("Dockerfile.") or name.endswith(".Dockerfile"):
            found.append(path)
    return sorted(found)


def _instructions(text: str) -> list[str]:
    """Logical instructions: comments dropped, backslash continuations joined."""
    out, current = [], ""
    for raw in text.splitlines():
        line = raw.strip()
        if not current and (not line or line.startswith("#")):
            continue
        if line.endswith("\\"):
            current += line[:-1] + " "
            continue
        current += line
        out.append(current.strip())
        current = ""
    if current.strip():
        out.append(current.strip())
    return out


def final_stage_labels(text: str) -> dict[str, str]:
    """The labels set after the last FROM (later assignments win, as in Docker)."""
    labels: dict[str, str] = {}
    for instruction in _instructions(text):
        keyword, _, rest = instruction.partition(" ")
        keyword = keyword.upper()
        if keyword == "FROM":
            labels = {}
        elif keyword == "LABEL":
            for token in shlex.split(rest):
                key, sep, value = token.partition("=")
                if sep:
                    labels[key] = value
    return labels


def check(text: str) -> str | None:
    """None when the final stage carries the label with the published value, else the reason."""
    if not re.search(r"(?im)^\s*FROM\s", text):
        return "no FROM instruction"
    labels = final_stage_labels(text)
    if LABEL_KEY not in labels:
        return f'the final stage has no LABEL {LABEL_KEY}="{LABEL_VALUE}"'
    if labels[LABEL_KEY] != LABEL_VALUE:
        return f"{LABEL_KEY} is {labels[LABEL_KEY]!r}, not {LABEL_VALUE!r}"
    return None


def test_the_walk_finds_every_known_dockerfile() -> None:
    found = {p.relative_to(_REPO).as_posix() for p in dockerfiles()}
    missing = KNOWN - found
    assert not missing, f"the Dockerfile walk no longer finds {sorted(missing)}"


def test_every_dockerfile_carries_the_interface_label() -> None:
    problems = [f"{p.relative_to(_REPO).as_posix()}: {reason}"
                for p in dockerfiles() if (reason := check(p.read_text(encoding="utf-8")))]
    assert not problems, "\n".join(problems)


def test_the_checker_reads_the_final_stage_only() -> None:
    good = 'FROM python:3.13-slim\nLABEL qfbench2.interface_version="2.0"\nCMD ["simulate"]\n'
    assert check(good) is None
    # Several labels on one instruction, continued over two lines.
    assert check('FROM x\nLABEL a=b \\\n      qfbench2.interface_version="2.0"\n') is None
    # Missing.
    assert check("FROM x\nCMD [\"simulate\"]\n") is not None
    # Wrong value.
    assert check('FROM x\nLABEL qfbench2.interface_version="1.0"\n') is not None
    # Only in a builder stage: it never reaches the image.
    assert check('FROM x AS build\nLABEL qfbench2.interface_version="2.0"\nFROM scratch\n') is not None
    # Only in a comment.
    assert check('FROM x\n# LABEL qfbench2.interface_version="2.0"\n') is not None
    # Overridden later in the same stage.
    assert check('FROM x\nLABEL qfbench2.interface_version="2.0"\n'
                 'LABEL qfbench2.interface_version="2.1"\n') is not None


def _run_all() -> int:
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
            else:
                print(f"ok   {name}")
    print("FAILED" if failures else "all Dockerfile label guards passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
