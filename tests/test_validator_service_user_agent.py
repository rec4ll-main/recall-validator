"""Inspect real API request construction; injected HTTP never leaves the test."""

import hashlib
import io
import json
from pathlib import Path

import pytest

from chain_v2_fake import MASTER, FakeWallet
from main import publish_metagraph_sync
from v3_metrics_client import DEFAULT_TIMEOUT_SECONDS as V3_TIMEOUT, fetch_v3_metrics

API_ORIGIN = "https://rec4ll.com/api"
FIXTURE_KEY = "fixture-api-key"


class Response(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def assert_service_request(request, path, timeout, expected_timeout):
    assert request.full_url == API_ORIGIN + path
    assert request.get_method() == "POST"
    assert timeout == expected_timeout
    headers = dict(request.header_items())
    # Exact public product token, without credentials, identities or platform data.
    assert headers["User-agent"] == "RecallValidator"
    assert headers["Content-type"] == "application/json"
    return headers


@pytest.mark.parametrize("timeout", [None, 7])
def test_scoring_requests_identify_service_and_preserve_auth_body_timeout(timeout):
    fixture = Path(__file__).parent / "fixtures" / "recall_v3_metrics_fixture.json"
    response = json.loads(fixture.read_text())["body"]
    cohort = [(m["uid"], m["hotkey"]) for m in response["miners"]]
    fetch, path = fetch_v3_metrics, "/validator/v3/metrics"
    seen = []

    def transport(request, timeout):
        headers = assert_service_request(request, path, timeout, expected_timeout)
        assert headers["X-internal-api-key"] == FIXTURE_KEY
        assert json.loads(request.data) == {
            "netuid": response["netuid"], "epoch_start_block": response["epoch_start_block"],
            "miners": [{"uid": uid, "hotkey": hotkey} for uid, hotkey in cohort],
        }
        seen.append(request)
        return Response(json.dumps(response).encode())

    expected_timeout = V3_TIMEOUT if timeout is None else timeout
    options = {} if timeout is None else {"timeout": timeout}
    result = fetch(API_ORIGIN, FIXTURE_KEY, cohort, response["netuid"],
                   response["epoch_start_block"], urlopen=transport, **options)
    assert len(seen) == 1 and result.netuid == response["netuid"]


@pytest.mark.parametrize("auth", ["key", "signature", "both"])
@pytest.mark.parametrize("timeout", [None, 7])
def test_registry_request_preserves_epistula_signature_and_auth(auth, timeout):
    wallet = None if auth == "key" else FakeWallet(MASTER)
    key = "" if auth == "signature" else FIXTURE_KEY
    seen = []

    def transport(request, timeout):
        headers = assert_service_request(request, "/validator/metagraph-sync", timeout, expected_timeout)
        assert headers.get("X-internal-api-key", "") == key
        assert json.loads(request.data) == {
            "miners": [{"uid": 3, "hotkey": "fixture-miner"}],
            "netuid": 353, "metagraph_block": 7200, "authoritative_empty": False,
        }
        assert headers["Epistula-uuid"]
        if wallet is not None:
            assert headers["Epistula-version"] == "2"
            assert headers["Epistula-signed-by"] == headers["Epistula-signed-for"] == MASTER.ss58_address
            message = (
                f'{hashlib.sha256(request.data).hexdigest()}.{headers["Epistula-uuid"]}.'
                f'{headers["Epistula-timestamp"]}.{headers["Epistula-signed-for"]}'
            ).encode()
            assert MASTER.verify(message, bytes.fromhex(headers["Epistula-request-signature"][2:]))
        else:
            assert "Epistula-request-signature" not in headers
        seen.append(request)
        return Response(b"{}")

    expected_timeout = 10 if timeout is None else timeout
    options = {} if timeout is None else {"timeout": timeout}
    assert publish_metagraph_sync(API_ORIGIN, key, [(3, "fixture-miner")], netuid=353,
                                  metagraph_block=7200, wallet=wallet, urlopen=transport, **options)
    assert len(seen) == 1
