"""Delivery uses the official C5 and descriptor-bound team claim, never a fake ID."""

import importlib.util
import json
from pathlib import Path
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "prepare_submission", ROOT / "scripts/prepare_submission.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
TEST_KEY = "fixture-only-team-key-qualification"


@pytest.fixture
def candidate():
    return module.load_json(ROOT / "submission/candidate.json")


@pytest.fixture
def key_file(tmp_path):
    path = tmp_path / "key"
    path.write_text(TEST_KEY)
    path.chmod(0o600)
    return path


@pytest.fixture
def delivery(candidate):
    return {
        "image": module.candidate_image(candidate),
        "status": "passed",
        "profile": "developer",
        "rankable": False,
        "scope": "delivery",
        "identity": module.delivery_identity(),
        "anonymous_pull": True,
        "platform": "linux/amd64",
        "runs": {
            verb: {"returncode": 0, "n_events": 10, "reported_n_events": 10}
            for verb in ("simulate", "simulate-batch")
        },
    }


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(
        module,
        "anonymous_pull",
        lambda c, p, execution_platform="linux/amd64": {
            "image": module.candidate_image(c),
            "anonymous_pull": True,
            "platform": execution_platform,
        },
    )


def test_descriptor_has_no_fictional_identity(candidate):
    template = module.descriptor(candidate)
    assert "team_id" not in template
    assert "descriptor_digest" not in template
    assert template["models"] == []
    body = module.descriptor(candidate, TEST_KEY)
    parsed = module.SubmissionDescriptor.from_mapping(body)
    assert parsed.models == () or parsed.models == []
    assert parsed.team_id.startswith("team-")


@pytest.mark.parametrize("phase", ["dev", "final"])
def test_official_package_roundtrip(candidate, delivery, key_file, tmp_path, phase):
    candidate["phase"] = phase
    first, second = tmp_path / "first.zip", tmp_path / "second.zip"
    result = module.package(candidate, delivery, first, key_file)
    module.package(candidate, delivery, second, key_file)
    assert first.read_bytes() == second.read_bytes()
    assert result["rankable"] is False and result["scope"] == "G1"
    with zipfile.ZipFile(first) as z:
        assert sorted(z.namelist()) == ["submission.json", "team-claim.json"]
        body = json.loads(z.read("submission.json"))
        claim = json.loads(z.read("team-claim.json"))
        assert body["phase"] == phase and body["models"] == []
        assert claim["schema_version"] == "2.0" and claim["site_team_id"] == 23
        assert all(TEST_KEY.encode() not in z.read(n) for n in z.namelist())
    module.validate_package(candidate, first, TEST_KEY)


def test_real_key_required(candidate, delivery, tmp_path):
    with pytest.raises(ValueError, match="Submission blocked"):
        module.package(candidate, delivery, tmp_path / "no.zip")
    assert not (tmp_path / "no.zip").exists()


@pytest.mark.parametrize(
    "delta",
    [
        {"image": "wrong"},
        {"rankable": True},
        {"scope": "historical"},
        {"status": "failed"},
        {"runs": {}},
        {"anonymous_pull": False},
    ],
)
def test_old_or_wrong_delivery_refused(candidate, delivery, key_file, tmp_path, delta):
    with pytest.raises(ValueError, match="delivery verification"):
        module.package(candidate, {**delivery, **delta}, tmp_path / "no.zip", key_file)


def test_claim_cannot_move_to_other_descriptor(candidate, delivery, key_file, tmp_path):
    archive = tmp_path / "one.zip"
    module.package(candidate, delivery, archive, key_file)
    with zipfile.ZipFile(archive) as z:
        raw = z.read("submission.json")
        claim = z.read("team-claim.json")
    altered = tmp_path / "altered.zip"
    with zipfile.ZipFile(altered, "w") as z:
        z.writestr("submission.json", raw + b" ")
        z.writestr("team-claim.json", claim)
    with pytest.raises(ValueError, match="claim does not bind"):
        module.validate_package(candidate, altered, TEST_KEY)
    with pytest.raises(ValueError, match="does not match"):
        module.validate_package(candidate, archive, "another-fixture-key")


def test_existing_archive_preserved(candidate, delivery, key_file, tmp_path):
    path = tmp_path / "old.zip"
    path.write_bytes(b"reviewed")
    with pytest.raises(FileExistsError):
        module.package(candidate, delivery, path, key_file)
    assert path.read_bytes() == b"reviewed"


def test_pull_rechecked_when_packaging(
    candidate, delivery, key_file, tmp_path, monkeypatch
):
    def unavailable(*args):
        raise ValueError("registry unavailable now")

    monkeypatch.setattr(module, "anonymous_pull", unavailable)
    with pytest.raises(ValueError, match="unavailable now"):
        module.package(candidate, delivery, tmp_path / "no.zip", key_file)
    assert not (tmp_path / "no.zip").exists()


def test_secret_and_legacy_fields_refused(candidate):
    for key in ("team_key", "confirmed_c5_team_id"):
        with pytest.raises(ValueError, match="never add a Team Key"):
            module.descriptor({**candidate, key: "not-a-secret"})


def test_wrong_event_count_refused(candidate, delivery):
    delivery["runs"]["simulate"]["reported_n_events"] = 100000000
    with pytest.raises(ValueError, match="cross-check counts"):
        module.validate_delivery(delivery, module.candidate_image(candidate))


def test_repeat_artifacts_follow_card_ledger_policy(tmp_path):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import prepare_submission as preparation

    unit = tmp_path / "unit"
    unit.mkdir()
    card = unit / "card.toml"
    for declared, required in (("false", False), ("true", True)):
        card.write_text(
            '[task]\nscenario_family = "throughput-scale"\n[scoring.params]\nrequires_message_ledger = '
            + declared
            + "\n"
        )
        assert preparation.repeat_artifacts(unit) == (
            {"trace.parquet", "message_trace.parquet"}
            if required
            else {"trace.parquet"}
        )
