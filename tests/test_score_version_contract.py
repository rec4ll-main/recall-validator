"""Cross-repository score_version contract pinned to recall-web release SHAs.

Fails closed when SUPPORTED_SCORE_VERSION, the web-sourced v3 evidence fixture, or the
vendored recall-web constant line in the contract drift apart. Historical journal
recovery of older versions is unrelated: this pin is about *new* API/client alignment
so a web v3ScoreVersion bump cannot silently mismatch.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from score_version import SUPPORTED_PROFILE, SUPPORTED_SCORE_VERSION

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "tests" / "contracts" / "score_version_contract.json"
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "recall_v3_metrics_from_web_handler.json"

# Exact shape of the Go constant as emitted by recall-web at the pinned SHA.
_WEB_CONSTANT_RE = re.compile(
    r'^v3ScoreVersion\s+=\s+"(?P<ver>[^"]+)"\s*(?://.*)?$'
)


def _contract() -> dict:
    return json.loads(CONTRACT_PATH.read_text())


def test_contract_pins_supported_version_and_web_release_sha():
    c = _contract()
    assert c["supported_score_version"] == SUPPORTED_SCORE_VERSION == "11"
    web = c["recall_web"]
    assert re.fullmatch(r"[0-9a-f]{40}", web["release_sha"]), web["release_sha"]
    assert web["release_sha"] == "362b8d11754919887cf032c80d647fb9dccbe4ff"
    assert web["source_path"] == "api/internal/handlers/validator_v3_params.go"
    m = _WEB_CONSTANT_RE.match(web["constant_line"])
    assert m is not None, web["constant_line"]
    assert m.group("ver") == SUPPORTED_SCORE_VERSION
    assert web["html_url"].endswith(f"/{web['release_sha']}/{web['source_path']}")


def test_web_probe_evidence_tag_is_recorded_as_not_a_score_version():
    tag = _contract()["recall_web"]["probe_evidence_tag"]
    assert tag["constant_name"] == "scoreVersion" and tag["value"] == "10"
    assert tag["value"] != SUPPORTED_SCORE_VERSION
    assert "not a validator score version" in tag["role"]


def test_web_fixture_matches_contract_and_supported_profile():
    c = _contract()
    assert c["recall_validator"]["fixture"] == "tests/fixtures/recall_v3_metrics_from_web_handler.json"
    body = json.loads(FIXTURE_PATH.read_text())
    assert (body["score_version"], body["score_kind"], body["window_seconds"]) == SUPPORTED_PROFILE


def test_contract_documents_no_silent_ingest_of_unsupported_new_versions():
    notes = " ".join(_contract()["notes"]).lower()
    assert "historical" in notes and "not permission" in notes
    assert "release_sha" in notes or "bump" in notes
    assert "v10" in notes and "tier-scores" in notes
