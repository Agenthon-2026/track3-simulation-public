"""The README's quick start is executable documentation, so this executes it.

    python tests/test_readme_commands.py

Four published statements were wrong at the same time, and each of them costs a participant a
working session before it costs anyone else anything:

  * ``(cd abides-jpmc-public && pip install -e ".[all]")`` -- measured exit 1. ABIDES is a
    multi-package repository with no top-level ``setup.py``/``pyproject.toml`` and no ``[all]``
    extra, so pip answers "does not appear to be a Python project". The repo's own baseline
    recipe (``baselines/Dockerfile``) has always named the two subdirectories instead.
  * ``python throughput/timer.py --image abides-baseline:latest`` -- measured exit 2.
    ``--scenario`` is ``required=True`` in ``timer.py``'s own parser, and the README published
    the invocation three times without it.
  * ``python -m qfbench2_common.scoring.stylized_facts --candidate ... --reference ...`` --
    measured exit 0, output: nothing. The module defines no ``argparse`` parser and no
    ``__main__`` block, so both flags are ignored and a silent no-op reads as "nothing wrong".
  * the shared-toolkit install line points at a private repository, so it exits 1 for every
    participant. That one is not fixable here; what is fixable is the README asserting it works.

Stdlib-only by construction -- the timer's parser is loaded straight from its file -- so this runs
in the firewall job alongside the other no-secret guards.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import re
import shlex
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_README = _REPO / "README.md"

#: The toolkit install URL the README publishes. Private today: `pip` exits 1 on
#: `git clone ... exit code: 128`.
_TOOLKIT_URL = "git+https://github.com/Agenthon-2026/Agenthon2026-public.git"

#: The toolkit must be installed from the repository that actually publishes it. The private hub
#: is the wrong source in two independent ways -- an anonymous clone cannot reach it, and the tag
#: the README used to name predates `qfbench2_common.contracts`. This pins the source rather than
#: a caveat: the earlier version of this test asserted a "not installable yet" note was present,
#: which stopped being true the moment the package was published.
_PRIVATE_HUB = "github.com/Agenthon-2026/Agenthon2026.git"
_PUBLIC_TOOLKIT = "github.com/Agenthon-2026/Agenthon2026-public.git"


def _bash_blocks(text: str) -> list[str]:
    """Every ```bash fenced block in *text*, with shell line continuations joined."""
    blocks = re.findall(r"```bash\n(.*?)```", text, flags=re.DOTALL)
    return [b.replace("\\\n", " ") for b in blocks]


def _invocations(blocks: list[str], needle: str) -> list[str]:
    """Every single-line command in *blocks* that runs *needle*."""
    found = []
    for block in blocks:
        for line in block.splitlines():
            line = line.strip()
            if needle in line and not line.startswith("#"):
                found.append(line)
    return found


def _load_timer():
    """Load throughput/timer.py by path, so no package __init__ and no toolkit is imported."""
    path = _REPO / "throughput" / "timer.py"
    spec = importlib.util.spec_from_file_location("t3_timer_for_readme", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_readme_timer_invocations_are_accepted_by_the_timers_own_parser():
    """Every published `timer.py` command must survive timer.py's argparse.

    `python throughput/timer.py --image abides-baseline:latest` was published three times and
    exits 2 -- `--scenario` is required. Checking the flag by string match would only pin today's
    parser, so this runs the real one: `main()` is called with the README's argv and
    `measure_throughput` is replaced by a sentinel, which is reached only if parsing succeeded.
    """
    timer = _load_timer()
    blocks = _bash_blocks(_README.read_text(encoding="utf-8"))
    commands = _invocations(blocks, "throughput/timer.py")
    assert commands, "the README no longer publishes a throughput/timer.py invocation"

    class _Reached(Exception):
        pass

    def _sentinel(**kwargs: object) -> None:
        raise _Reached

    timer.measure_throughput = _sentinel  # type: ignore[assignment]

    for command in commands:
        argv = command.split()[2:]  # drop `python throughput/timer.py`
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                timer.main(argv)
        except _Reached:
            pass  # argparse accepted it, which is all this test is about
        except SystemExit as exc:  # argparse's own exit, not the timer's
            raise AssertionError(
                f"the README publishes `{command}`, which timer.py's own parser rejects "
                f"(exit {exc.code})"
            ) from None

        # A path the README names must exist, or the command fails on the next line instead.
        scenario = argv[argv.index("--scenario") + 1]
        if "<" not in scenario:
            assert (_REPO / scenario).exists(), (
                f"the README points `--scenario` at {scenario}, which is not in this repo"
            )


def _check_abides_instructions(
    readme: str, baseline_readme: str, dockerfile: str
) -> None:
    """Check the Docker quick start and its linked, complete non-Docker recipe.

    `pip install -e ".[all]"` from the ABIDES root exits 1: no top-level setup.py/pyproject.toml
    and no `[all]` extra. `baselines/Dockerfile` is the recipe that is executed on every baseline
    build, so it -- not prose -- is the reference this test compares against.
    """
    builds = _invocations(_bash_blocks(readme), "docker build ")
    assert builds, "the README no longer shows how to build the baseline"
    for command in builds:
        argv = shlex.split(command)
        assert (_REPO / argv[-1] / "Dockerfile").resolve() == (
            _REPO / "baselines" / "Dockerfile"
        ).resolve(), (
            "the README's build context does not select the baseline Dockerfile"
        )
        assert "--platform=linux/amd64" in argv, (
            "the baseline build must select linux/amd64"
        )
    assert "baselines/README.md" in readme, "the non-Docker recipe must be reachable"

    installs = [
        command
        for command in _invocations(
            _bash_blocks(readme + "\n" + baseline_readme), "pip install"
        )
        if "abides-" in command
    ]
    assert installs, "the baseline README no longer shows how to install ABIDES"

    for command in installs:
        assert '".[all]"' not in command and "'.[all]'" not in command, (
            f"the README publishes `{command}`; installing from the ABIDES repository root "
            f"exits 1 -- it has no top-level setup.py/pyproject.toml and no [all] extra"
        )
        for package in ("abides-core", "abides-markets"):
            assert package in command, (
                f"the README's ABIDES install does not name {package}, which "
                f"baselines/Dockerfile installs"
            )
        assert "abides-gym" not in command, (
            "the Scope section says abides-gym is out of scope; the install must not name it"
        )

    # The same two packages the image installs, and no third one.
    assert "abides-core" in dockerfile and "abides-markets" in dockerfile
    assert "/tmp/abides/abides-gym" not in dockerfile

    commands = _invocations(_bash_blocks(baseline_readme), "git -C abides-jpmc-public ")
    documented_patches = []
    for command in commands:
        argv = shlex.split(command)
        if argv[3] == "apply":
            assert len(argv) == 5 and argv[4].startswith("$PWD/baselines/patches/")
            patch = Path(argv[4]).name
            assert (_REPO / "baselines" / "patches" / patch).is_file()
            documented_patches.append(patch)
    image_patches = re.findall(
        r"git -C /tmp/abides apply /tmp/patches/(\S+)", dockerfile
    )
    assert image_patches and documented_patches == image_patches, (
        "the non-Docker recipe must apply every baseline overlay in Dockerfile order"
    )


def test_readme_installs_abides_the_way_the_baseline_image_does():
    _check_abides_instructions(
        _README.read_text(encoding="utf-8"),
        (_REPO / "baselines" / "README.md").read_text(encoding="utf-8"),
        (_REPO / "baselines" / "Dockerfile").read_text(encoding="utf-8"),
    )


def test_readme_baseline_guard_rejects_an_incomplete_overlay_recipe():
    readme = _README.read_text(encoding="utf-8")
    baseline_readme = (_REPO / "baselines" / "README.md").read_text(encoding="utf-8")
    dockerfile = (_REPO / "baselines" / "Dockerfile").read_text(encoding="utf-8")
    incomplete = "\n".join(
        line
        for line in baseline_readme.splitlines()
        if not (line.startswith("git -C ") and "kernel_message_ledger.patch" in line)
    )
    assert incomplete != baseline_readme.rstrip("\n"), (
        "negative control did not remove a patch"
    )
    try:
        _check_abides_instructions(readme, incomplete, dockerfile)
    except AssertionError as exc:
        assert "every baseline overlay" in str(exc), f"unexpected refusal: {exc}"
    else:
        raise AssertionError("the README guard accepted an incomplete overlay recipe")


def test_readme_does_not_claim_the_private_toolkit_can_be_installed():
    """Wherever the README publishes the toolkit install, the caveat must be published too.

    `Agenthon-2026/Agenthon2026` is private and is not part of the public release, so the
    published command exits 1 for every participant. The distribution decision is the
    organizers'; what this guards is that the README does not silently assert it works and send
    someone off to debug their own environment.
    """
    readme = _README.read_text(encoding="utf-8")
    assert _TOOLKIT_URL in readme, (
        "the README no longer names the toolkit install; if the toolkit became publicly "
        "installable, update this test along with the README"
    )
    assert _PRIVATE_HUB not in readme, (
        f"the README installs the toolkit from {_PRIVATE_HUB}, which is private: an anonymous "
        "clone returns 'Repository not found', so the command exits 1 for every participant"
    )
    assert _PUBLIC_TOOLKIT in readme, (
        f"the README must install the toolkit from {_PUBLIC_TOOLKIT}, the repository that "
        "publishes the package"
    )


def test_readme_does_not_publish_a_module_that_has_no_cli_as_a_command():
    """A `python -m ...` line must name a module that actually has a command-line entry point.

    The README published, twice, `python -m qfbench2_common.scoring.stylized_facts --candidate ...
    --reference ...` as the way to measure stylized facts. Measured: that module defines no
    `argparse` parser and no `__main__` block, so the command exits 0, ignores both flags and
    prints nothing -- a silent no-op reads as "nothing wrong", which is worse for a participant
    than a hard failure.

    Modules this repo ships are resolved against their own source, so a future `-m` line pointing
    at a library is caught the same way. Toolkit modules cannot be resolved from here -- the
    toolkit is not installed in the job this runs in -- so the one that shipped is named
    explicitly below rather than left to a check that cannot see it.
    """
    readme = _README.read_text(encoding="utf-8")
    commands = _invocations(_bash_blocks(readme), "python -m ")
    for command in commands:
        module = command.split("python -m ", 1)[1].split()[0]
        source = _REPO / (module.replace(".", "/") + ".py")
        if not source.exists():
            continue  # not ours to resolve; the explicit ban below covers the one that shipped
        text = source.read_text(encoding="utf-8")
        assert "__main__" in text or "argparse" in text, (
            f"the README publishes `{command}`, but {source.relative_to(_REPO)} has no "
            f"__main__ block and no argparse parser -- the command exits 0 and does nothing"
        )

    # The specific no-op that shipped must not come back, in any bash block.
    for command in commands:
        assert "qfbench2_common.scoring.stylized_facts" not in command, (
            "`python -m qfbench2_common.scoring.stylized_facts` is a no-op: the module has no CLI, "
            "so it exits 0, ignores its flags and prints nothing. The local numbers come from "
            "run_regression.py's stylized_fact_report.json"
        )


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
    print("FAILED" if failures else "all README command guards passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
