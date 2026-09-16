"""Recheck the participant image and package a Development submission.

Local verification is non-rankable. The official toolkit derives the team alias
and builds the claim; platform availability does not block local packaging.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import subprocess
import sys
from datetime import datetime, timezone
from importlib.resources import files
from importlib.metadata import distribution
from urllib.request import Request, urlopen
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from qfbench2_common.contracts.descriptor import (  # noqa: E402
    SubmissionDescriptor,
)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text())


def candidate_image(candidate: dict) -> str:
    allowed = {"image", "license", "team_number", "phase"}
    if set(candidate) != allowed:
        raise ValueError(
            "candidate fields must match submission/candidate.json; never add a Team Key"
        )
    from qfbench2_common.team_claim import validate_team_number

    validate_team_number(candidate["team_number"])
    if candidate["phase"] not in {"dev", "final"}:
        raise ValueError("phase must be dev or final")
    image = candidate["image"]
    if set(image) != {"registry", "repository", "digest"} or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", image["digest"]
    ):
        raise ValueError("candidate requires a fixed image digest")
    return f"{image['registry']}/{image['repository']}@{image['digest']}"


def descriptor(candidate: dict, team_key: str | None = None) -> dict:
    """Return a template without a fictional identity, or a sealed real-team C5."""
    candidate_image(candidate)
    fixture = files("qfbench2_common").joinpath(
        f"contracts/fixtures/c5/simulation_{candidate['phase']}.json"
    )
    body = json.loads(fixture.read_text())
    body.update(
        models=[],
        image=candidate["image"],
        image_access="public",
        license=candidate["license"],
    )
    body.pop("descriptor_digest", None)
    body.pop("team_id", None)
    if team_key is not None:
        from qfbench2_common.team_claim import seal_for_team

        return seal_for_team(body, candidate["team_number"], team_key)
    return body


def blockers(candidate: dict, key_file: Path | None = None) -> list[str]:
    candidate_image(candidate)
    if key_file is None:
        return [
            "team_key_file: real-team key required for G1; supply a private file outside the repository"
        ]
    read_key(key_file)
    return []


def read_key(path: Path) -> str:
    from qfbench2_common.team_claim import read_team_key_file

    if path.resolve().is_relative_to(ROOT):
        raise ValueError("Team Key must be outside the repository")
    return read_team_key_file(path)


def public_image(image: dict) -> None:
    if image["registry"] != "ghcr.io":
        raise ValueError(
            "anonymous registry probe currently supports this candidate's GHCR registry"
        )
    base = f"https://ghcr.io/v2/{image['repository']}/manifests/{image['digest']}"
    token_url = f"https://ghcr.io/token?service=ghcr.io&scope=repository:{image['repository']}:pull"
    with urlopen(token_url, timeout=30) as response:
        token = json.load(response)["token"]
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": ", ".join(
            [
                "application/vnd.oci.image.index.v1+json",
                "application/vnd.oci.image.manifest.v1+json",
                "application/vnd.docker.distribution.manifest.list.v2+json",
                "application/vnd.docker.distribution.manifest.v2+json",
            ]
        ),
    }
    with urlopen(Request(base, headers=headers), timeout=30) as response:
        payload = response.read()
        if "sha256:" + hashlib.sha256(payload).hexdigest() != image["digest"]:
            raise ValueError("registry returned different image bytes")


def output_hashes(output: Path) -> dict[str, str]:
    hashes = {}
    for path in sorted(output.rglob("*.parquet")):
        with path.open("rb") as handle:
            hashes[path.relative_to(output).as_posix()] = hashlib.file_digest(
                handle, "sha256"
            ).hexdigest()
    return hashes


def batch_units() -> list[Path]:
    return sorted((ROOT / "units").glob("t3-gbatch-*"))


def repeat_units() -> list[Path]:
    return [ROOT / "units/t3-as06-throughput-fast", batch_units()[0]]


def repeat_artifacts(unit: Path) -> set[str]:
    from qfbench2_track_simulation.batch import load_subs
    from qfbench2_track_simulation.scoring import _CardPolicy

    batch = (unit / "batch.json").exists()
    prefixes = [entry["sub"] + "/" for entry in load_subs(unit)] if batch else [""]
    names = ["trace.parquet"]
    if batch or _CardPolicy(unit).requires_message_ledger:
        names.append("message_trace.parquet")
    return {prefix + name for prefix in prefixes for name in names}


def anonymous_pull(
    candidate: dict, output: Path, execution_platform: str = "linux/amd64"
) -> dict:
    """Pull using an empty Docker auth config, preserving only the daemon endpoint."""
    if execution_platform not in {"linux/amd64", "linux/arm64"}:
        raise ValueError("unsupported execution platform")
    image = candidate_image(candidate)
    public_image(candidate["image"])
    endpoint = os.environ.get("DOCKER_HOST")
    if not endpoint:
        context = json.loads(subprocess.check_output(["docker", "context", "inspect"]))[
            0
        ]
        endpoint = context["Endpoints"]["docker"]["Host"]
    with tempfile.TemporaryDirectory(prefix="t3-anonymous-") as config:
        command = [
            "docker",
            "--config",
            config,
            "--host",
            endpoint,
            "pull",
            "--platform=" + execution_platform,
            image,
        ]
        with (output / "anonymous-pull.log").open("xb") as log:
            subprocess.run(
                command, check=True, stdout=log, stderr=subprocess.STDOUT, timeout=600
            )
    inspected = json.loads(
        subprocess.check_output(["docker", "image", "inspect", image])
    )[0]
    if (
        inspected["Architecture"] != execution_platform.split("/")[1]
        or inspected["Os"] != "linux"
    ):
        raise ValueError("candidate must resolve to " + execution_platform)
    required = {
        "qfbench2.interface_version": "2.0",
        "qfbench2.track": "simulation",
        "qfbench2.category": "simulator",
    }
    if any(inspected["Config"]["Labels"].get(k) != v for k, v in required.items()):
        raise ValueError("incorrect interface labels")
    return {
        "image": image,
        "platform": execution_platform,
        "labels": required,
        "anonymous_pull": True,
    }


def delivery_identity() -> dict:
    package = distribution("qfbench2-common")
    return {
        "toolkit_version": package.version,
        "toolkit_source": json.loads(package.read_text("direct_url.json") or "null"),
        "gate_sha256": hashlib.sha256(
            Path(__file__).read_bytes() + (ROOT / "throughput/run_unit.py").read_bytes()
        ).hexdigest(),
    }


def verify_delivery(
    candidate: dict, output: Path, execution_platform: str = "linux/amd64"
) -> dict:
    """Execute both verbs offline; this limited check is never a G2 report."""
    from dataclasses import asdict
    from throughput.run_unit import run_once, is_batch_unit

    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    evidence = anonymous_pull(candidate, output, execution_platform)
    evidence.update(
        profile="developer",
        rankable=False,
        scope="delivery",
        identity=delivery_identity(),
        checked_at=datetime.now(timezone.utc).isoformat(),
        runs={},
    )
    for unit in repeat_units():
        record = run_once(
            candidate_image(candidate),
            unit,
            batch=is_batch_unit(unit),
            keep_output=output / unit.name,
            run_as_host_user=True,
            timeout_sec=1800,
            scratch_root=output,
        )
        evidence["runs"]["simulate-batch" if is_batch_unit(unit) else "simulate"] = (
            asdict(record)
        )
    evidence["status"] = "passed"
    (output / "delivery.json").write_text(json.dumps(evidence, indent=2) + "\n")
    return evidence


def validate_delivery(
    evidence: dict, image: str, execution_platform: str = "linux/amd64"
) -> None:
    if execution_platform not in {"linux/amd64", "linux/arm64"}:
        raise ValueError("unsupported execution platform")
    if (
        evidence.get("image") != image
        or evidence.get("status") != "passed"
        or evidence.get("identity") != delivery_identity()
        or evidence.get("scope") != "delivery"
        or evidence.get("profile") != "developer"
        or evidence.get("rankable") is not False
        or evidence.get("anonymous_pull") is not True
        or evidence.get("platform") != execution_platform
    ):
        raise ValueError("delivery verification must pass for the exact candidate")
    runs = evidence.get("runs", {})
    if set(runs) != {"simulate", "simulate-batch"} or any(
        r.get("returncode") != 0
        or r.get("n_events", 0) <= 0
        or r.get("n_events") != r.get("reported_n_events")
        for r in runs.values()
    ):
        raise ValueError(
            "delivery verification must execute both verbs and cross-check counts"
        )


def validate_package(candidate: dict, archive: Path, key: str) -> dict:
    from qfbench2_common.team_claim import (
        build_team_claim,
        descriptor_digest,
        derive_team_alias,
    )

    with zipfile.ZipFile(archive) as z:
        if sorted(z.namelist()) != ["submission.json", "team-claim.json"]:
            raise ValueError("package must contain exactly the descriptor and claim")
        raw = z.read("submission.json")
        body = json.loads(raw)
        parsed = SubmissionDescriptor.from_mapping(body)
        if body != descriptor(
            candidate, key
        ) or parsed.image_reference() != candidate_image(candidate):
            raise ValueError("package descriptor does not match candidate")
        if parsed.team_id != derive_team_alias(candidate["team_number"], key):
            raise ValueError("package identity does not match team")
        expected = build_team_claim(
            candidate["team_number"], key, descriptor_digest(raw)
        )
        if z.read("team-claim.json") != expected:
            raise ValueError("team claim does not bind the exact descriptor")
        if any(key.encode() in z.read(n) for n in z.namelist()):
            raise ValueError("key material must not enter the package")
    return {
        "descriptor_digest": body["descriptor_digest"],
        "team_id": parsed.team_id,
        "claim_schema": "2.0",
    }


def package(
    candidate: dict,
    evidence: dict,
    output: Path,
    key_file: Path | None = None,
    execution_platform: str = "linux/amd64",
) -> dict:
    from qfbench2_common.team_claim import pack_submission

    pending = blockers(candidate, key_file)
    if pending:
        raise ValueError("Submission blocked: " + "; ".join(pending))
    validate_delivery(evidence, candidate_image(candidate), execution_platform)
    key = read_key(key_file)
    body = descriptor(candidate, key)
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    # The toolkit replaces existing files. Stage privately, then exclusively copy so
    # a previously reviewed archive cannot be overwritten, including via a symlink.
    with tempfile.TemporaryDirectory(prefix="t3-pack-", dir=output.parent) as temporary:
        staging = Path(temporary)
        pull = anonymous_pull(candidate, staging, execution_platform)
        archive = staging / "submission.zip"
        pack_submission(body, candidate["team_number"], key, archive)
        binding = validate_package(candidate, archive, key)
        with output.open("xb") as handle:
            handle.write(archive.read_bytes())
    return {
        **pull,
        **binding,
        "profile": "developer",
        "qualification_scope": "local-arm64"
        if execution_platform == "linux/arm64"
        else "amd64",
        "rankable": False,
        "scope": "G1",
        "status": "passed",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "archive_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "archive": str(output),
        "delivery": evidence,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "verify-delivery", "package"))
    parser.add_argument(
        "--candidate", type=Path, default=ROOT / "submission/candidate.json"
    )
    parser.add_argument("--out", type=Path)
    parser.add_argument("--verification", type=Path)
    parser.add_argument("--team-key-file", type=Path)
    parser.add_argument(
        "--execution-platform",
        choices=("linux/amd64", "linux/arm64"),
        default="linux/amd64",
    )
    args = parser.parse_args()
    try:
        candidate = load_json(args.candidate)
        descriptor(candidate)
        if args.action == "status":
            pending = blockers(candidate, args.team_key_file)
            print(
                json.dumps(
                    {"registration_ready": not pending, "blockers": pending}, indent=2
                )
            )
            return int(bool(pending))
        if args.out is None:
            parser.error("--out is required; verification directories must be new")
        if args.action == "verify-delivery":
            print(
                json.dumps(
                    verify_delivery(candidate, args.out, args.execution_platform),
                    indent=2,
                )
            )
        else:
            if args.verification is None:
                parser.error("--verification is required for packaging")
            result = package(
                candidate,
                load_json(args.verification),
                args.out,
                args.team_key_file,
                args.execution_platform,
            )
            args.out.with_suffix(".g1.json").write_text(
                json.dumps(result, indent=2) + "\n"
            )
            print(json.dumps(result, indent=2))
        return 0
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
