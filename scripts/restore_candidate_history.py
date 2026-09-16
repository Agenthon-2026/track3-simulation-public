"""Restore failures and spent confirmation opportunities before qualification.

Read only named history members from same-repository, same-branch artifacts.
Moving to a new worker cannot reset failures or repeat a confirmation experiment.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import urlencode
import zipfile

import benchmark_candidates as benchmark


def api(endpoint: str) -> bytes:
    return subprocess.check_output(
        ["gh", "api", endpoint], timeout=600 if endpoint.endswith("/zip") else 30
    )


def validate_history(payload: bytes) -> None:
    latest = {}
    for line in payload.decode().splitlines():
        record = json.loads(line)
        unit, failure = record["unit"], record["failure"]
        previous = latest.get(unit)
        count = benchmark.next_problem_count(previous, failure)
        if record["count"] != count:
            raise ValueError("failure history count disagrees with prior records")
        latest[unit] = record


def pages(endpoint: str, key: str) -> list[dict]:
    result = []
    page = 1
    separator = "&" if "?" in endpoint else "?"
    while True:
        items = json.loads(api(f"{endpoint}{separator}per_page=100&page={page}"))[key]
        result.extend(items)
        if len(items) < 100:
            return result
        page += 1


def read_reservations(z: zipfile.ZipFile, prefix: str) -> dict[str, bytes]:
    result = {}
    for item in z.infolist():
        if item.is_dir() or not item.filename.startswith(prefix):
            continue
        name = item.filename.removeprefix(prefix)
        if not re.fullmatch(r"[0-9a-f]{64}\.json", name) or item.file_size > 65536:
            raise ValueError("invalid confirmation reservation member")
        payload = z.read(item)
        record = json.loads(payload)
        image = record.get("image", "")
        if (
            not re.fullmatch(r".+@sha256:[0-9a-f]{64}", image)
            or name != benchmark.digest(image) + ".json"
            or record.get("rankable") is not False
            or not isinstance(record.get("plan"), dict)
            or not re.fullmatch(r"[0-9a-f]{64}", record["plan"].get("sha256", ""))
            or not re.fullmatch(r"[0-9a-f]{64}", record.get("plan_sha256", ""))
        ):
            raise ValueError("invalid confirmation reservation identity")
        if name in result:
            raise ValueError("duplicate confirmation reservation member")
        result[name] = payload
    return result


def restore(repo: str, branch: str, current_run: int, output: Path) -> dict:
    query = urlencode({"event": "workflow_dispatch", "branch": branch})
    runs = pages(f"repos/{repo}/actions/runs?{query}", "workflow_runs")
    chosen = None
    confirmations = {}
    reservation_sources = []

    def merge_reservations(values):
        for name, data in values.items():
            if name in confirmations and confirmations[name] != data:
                raise ValueError("conflicting confirmation reservations")
            confirmations[name] = data

    # The job's concurrency group serializes native runs on this branch. Ignore
    # the current run and newer queued dispatches, regardless of API list order.
    for run in sorted(runs, key=lambda r: r["id"], reverse=True):
        if run["id"] >= current_run:
            continue
        artifacts = pages(
            f"repos/{repo}/actions/runs/{run['id']}/artifacts", "artifacts"
        )
        history_name = f"native-history-{run['id']}"
        for artifact in sorted(artifacts, key=lambda a: a["name"] != history_name):
            reservation_only = artifact["name"] == f"native-reservations-{run['id']}"
            if reservation_only:
                if artifact["expired"]:
                    raise ValueError(
                        "confirmation reservations expired; recover before qualification"
                    )
                archive = api(f"repos/{repo}/actions/artifacts/{artifact['id']}/zip")
                with zipfile.ZipFile(io.BytesIO(archive)) as z:
                    values = read_reservations(z, "")
                if not values:
                    raise ValueError("empty confirmation reservation artifact")
                merge_reservations(values)
                reservation_sources.append(
                    {
                        "source_run": run["id"],
                        "source_artifact": artifact["id"],
                        "archive_sha256": hashlib.sha256(archive).hexdigest(),
                    }
                )
                continue
            if chosen is not None:
                continue
            if artifact["name"] not in {
                history_name,
                f"native-qualification-{run['id']}",
            }:
                continue
            if artifact["expired"]:
                raise ValueError(
                    "prior native evidence expired; failure history needs recovery"
                )
            archive = api(f"repos/{repo}/actions/artifacts/{artifact['id']}/zip")
            with zipfile.ZipFile(io.BytesIO(archive)) as z:
                member = (
                    "problems.jsonl"
                    if artifact["name"] == history_name
                    else "history/problems.jsonl"
                )
                if member not in z.namelist():
                    if any(name.endswith("/record.json") for name in z.namelist()):
                        raise ValueError(
                            "prior run records exist without failure history"
                        )
                    continue
                payload = z.read(member)
                prefix = (
                    "confirmations/"
                    if artifact["name"] == history_name
                    else "history/confirmations/"
                )
                merge_reservations(read_reservations(z, prefix))
            validate_history(payload)
            chosen = (
                payload,
                {
                    "rankable": False,
                    "source_run": run["id"],
                    "source_artifact": artifact["id"],
                    "archive_sha256": hashlib.sha256(archive).hexdigest(),
                    "history_sha256": hashlib.sha256(payload).hexdigest(),
                },
            )
    if chosen is None:
        raise ValueError(
            "no retained native history found; recover history before qualification"
        )
    payload, result = chosen
    result["confirmation_reservations"] = {
        name: hashlib.sha256(data).hexdigest() for name, data in confirmations.items()
    }
    result["reservation_sources"] = reservation_sources
    output.mkdir(parents=True, exist_ok=True)
    with (output / "problems.jsonl").open("xb") as f:
        f.write(payload)
    for name, data in confirmations.items():
        target = output / "confirmations" / name
        target.parent.mkdir(exist_ok=True)
        with target.open("xb") as f:
            f.write(data)
    benchmark.save(output / "restored-history.json", result)
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo", required=True)
    p.add_argument("--branch", required=True)
    p.add_argument("--current-run", type=int, required=True)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    print(json.dumps(restore(args.repo, args.branch, args.current_run, args.out)))
