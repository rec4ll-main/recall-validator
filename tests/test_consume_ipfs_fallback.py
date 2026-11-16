"""Consume tries the configured gateways after a Kubo fetch failure.

Runs the real ``slave_consume`` against the deterministic chain fixture and real
loopback HTTP servers standing in for Kubo and a content gateway.
"""

from __future__ import annotations

import http.server
import sqlite3
import threading
import time
from pathlib import Path
from dataclasses import replace

import pytest

import bundle_ipfs
from bundle_consume import slave_consume
from bundle_ipfs import compute_raw_cid
from chain_v2_fake import (
    SLAVE,
    FakeChain,
    FakeMetagraph,
    FakeSubtensor,
    FakeWallet,
    build_envelope_for,
    config,
    keypair,
    publish_pointer,
)

E = 3600  # epoch start (index 10); cutoff D = 3612


class _Server:
    """Loopback HTTP server; ``mode`` controls the Kubo/gateway responses."""

    def __init__(self):
        self.blocks: dict[str, bytes] = {}
        self.mode = "ok"  # ok | 500 | slow | wrong_bytes | redirect | oversized
        self.requests: list[tuple[str, str]] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _serve(self, cid):
                outer.requests.append((self.command, self.path))
                if outer.mode == "500":
                    self.send_response(500)
                    self.end_headers()
                    return
                if outer.mode == "slow":
                    time.sleep(1.0)
                if outer.mode == "redirect":
                    self.send_response(302)
                    self.send_header("Location", "http://127.0.0.1:9/ipfs/redirected")
                    self.end_headers()
                    return
                data = outer.blocks.get(cid)
                if outer.mode == "wrong_bytes":
                    data = b"not the pinned bundle"
                if outer.mode == "oversized":
                    data = b"x" * (bundle_ipfs.MAX_BYTES + 10)
                if data is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_POST(self):
                if self.path.startswith("/api/v0/block/get"):
                    return self._serve(self.path.split("arg=", 1)[1])
                outer.requests.append((self.command, self.path))
                self.send_response(404)
                self.end_headers()

            def do_GET(self):
                if self.path.startswith("/ipfs/"):
                    return self._serve(self.path[len("/ipfs/"):])
                outer.requests.append((self.command, self.path))
                self.send_response(405)
                self.end_headers()

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://fixture-gateway.example:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture()
def kubo():
    s = _Server()
    yield s
    s.close()


@pytest.fixture()
def gateway():
    s = _Server()
    yield s
    s.close()


@pytest.fixture()
def closed_port_url():
    httpd = http.server.HTTPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.server_close()
    return url


def _setup(tmp_path, envelope=None, **env_kw):
    chain = FakeChain(head=E + 20)
    sub = FakeSubtensor(chain)
    if envelope is None:
        envelope = build_envelope_for(chain, sub, E, **env_kw)
    cid = publish_pointer(chain, envelope, E + 5)
    return chain, sub, envelope, cid


def _consume(tmp_path, sub, **cfg_kw):
    cfg_kw.setdefault("gateway_urls", ())
    cfg = replace(config(tmp_path / "state", **cfg_kw), ipfs_plaintext_hosts=("fixture-gateway.example",))
    return slave_consume(sub, FakeMetagraph(), FakeWallet(SLAVE), cfg)


def _journal_rows(tmp_path) -> list[tuple]:
    path = Path(tmp_path / "state" / "journal.sqlite3")
    if not path.exists():
        return []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT subnet_epoch_index, state FROM operations").fetchall()
    finally:
        conn.close()


def _assert_no_intent(tmp_path, sub):
    assert sub.set_weights_calls == 0
    assert sub.chain.submissions == []
    assert _journal_rows(tmp_path) == []


@pytest.mark.parametrize("kubo_failure", ["500", "refused", "timeout"])
def test_secondary_gateway_succeeds_with_admin_unavailable(tmp_path, kubo, gateway, closed_port_url,
                                            kubo_failure, monkeypatch):
    chain, sub, envelope, cid = _setup(tmp_path)
    gateway.blocks[cid] = envelope
    api = kubo.url
    if kubo_failure == "500":
        kubo.mode = "500"
    elif kubo_failure == "refused":
        api = closed_port_url
    else:
        monkeypatch.setattr(bundle_ipfs, "TIMEOUT_SECONDS", 0.2)
        kubo.mode = "slow"

    out = _consume(tmp_path, sub, ipfs_api_url=api, gateway_urls=(gateway.url,))

    assert out.status == "applied", out
    assert ("GET", f"/ipfs/{cid}") in gateway.requests
    assert sub.set_weights_calls == 1
    assert len(chain.applied_weight_sets) == 1


def test_secondary_never_contacts_successful_kubo(tmp_path, kubo, gateway):
    chain, sub, envelope, cid = _setup(tmp_path)
    kubo.blocks[cid] = envelope
    gateway.blocks[cid] = envelope

    out = _consume(tmp_path, sub, ipfs_api_url=kubo.url, gateway_urls=(gateway.url,))

    assert out.status == "applied", out
    assert kubo.requests == []
    assert gateway.requests == [("GET", f"/ipfs/{cid}")]


def test_gateway_only_configuration_still_works(tmp_path, gateway):
    chain, sub, envelope, cid = _setup(tmp_path)
    gateway.blocks[cid] = envelope

    out = _consume(tmp_path, sub, gateway_urls=(gateway.url,))

    assert out.status == "applied", out
    assert gateway.requests == [("GET", f"/ipfs/{cid}")]


def test_second_gateway_is_tried_after_first_gateway_fails(tmp_path, kubo, gateway):
    chain, sub, envelope, cid = _setup(tmp_path)
    kubo.mode = "500"
    first = _Server()
    try:
        first.mode = "500"
        gateway.blocks[cid] = envelope
        out = _consume(
            tmp_path, sub, ipfs_api_url=kubo.url, gateway_urls=(first.url, gateway.url)
        )
    finally:
        first.close()
    assert out.status == "applied", out


def test_primary_and_all_fallbacks_failing_is_a_clear_blocked_outcome(tmp_path, kubo, gateway):
    chain, sub, envelope, cid = _setup(tmp_path)
    kubo.mode = "500"
    gateway.mode = "500"

    out = _consume(tmp_path, sub, ipfs_api_url=kubo.url, gateway_urls=(gateway.url,))

    assert out.status == "blocked"
    assert out.reason.startswith("bundle fetch failed: ")
    assert kubo.requests == []
    assert "all gateways failed: IPFS HTTP 500" in out.reason
    _assert_no_intent(tmp_path, sub)


def test_no_fetch_source_configured_is_blocked(tmp_path):
    chain, sub, envelope, cid = _setup(tmp_path)
    out = _consume(tmp_path, sub)
    assert (out.status, out.reason) == ("blocked", "no IPFS API or gateway to fetch")
    _assert_no_intent(tmp_path, sub)


def test_fallback_bytes_with_wrong_cid_are_rejected(tmp_path, kubo, gateway):
    chain, sub, envelope, cid = _setup(tmp_path)
    kubo.mode = "500"
    gateway.mode = "wrong_bytes"

    out = _consume(tmp_path, sub, ipfs_api_url=kubo.url, gateway_urls=(gateway.url,))

    assert out.status == "blocked"
    assert "do not hash to the requested CID" in out.reason
    _assert_no_intent(tmp_path, sub)


def test_fallback_bundle_with_invalid_signature_is_rejected(tmp_path, kubo, gateway):
    chain = FakeChain(head=E + 20)
    sub = FakeSubtensor(chain)
    forged = build_envelope_for(chain, sub, E, signer=keypair("Attacker"))
    cid = publish_pointer(chain, forged, E + 5)
    kubo.mode = "500"
    gateway.blocks[cid] = forged

    out = _consume(tmp_path, sub, ipfs_api_url=kubo.url, gateway_urls=(gateway.url,))

    assert out.status == "blocked"
    assert out.reason == "bundle verification failed: signature verification failed"
    _assert_no_intent(tmp_path, sub)


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"epoch_start_block": E - 360, "subnet_epoch_index": 9, "selection_block": E - 348},
         "epoch_start_block binding mismatch"),
        ({"genesis_hash": "0x" + "cd" * 32}, "genesis_hash binding mismatch"),
        ({"anchor_hash": "0x" + "ee" * 32}, "anchor_hash binding mismatch"),
    ],
)
def test_fallback_bundle_with_stale_epoch_or_wrong_bindings_is_rejected(
    tmp_path, kubo, gateway, overrides, message
):
    chain = FakeChain(head=E + 20)
    sub = FakeSubtensor(chain)
    bad = build_envelope_for(chain, sub, E, **overrides)
    cid = publish_pointer(chain, bad, E + 5)
    kubo.mode = "500"
    gateway.blocks[cid] = bad

    out = _consume(tmp_path, sub, ipfs_api_url=kubo.url, gateway_urls=(gateway.url,))

    assert out.status == "blocked"
    assert out.reason == f"bundle verification failed: {message}"
    _assert_no_intent(tmp_path, sub)


@pytest.mark.parametrize("mode, message", [
    ("oversized", "fetched block exceeds 1 MiB"),
    ("redirect", "redirect not allowed (HTTP 302)"),
])
def test_fallback_oversized_and_redirect_responses_stay_rejected(
    tmp_path, kubo, gateway, mode, message
):
    chain, sub, envelope, cid = _setup(tmp_path)
    kubo.mode = "500"
    gateway.blocks[cid] = envelope
    gateway.mode = mode

    out = _consume(tmp_path, sub, ipfs_api_url=kubo.url, gateway_urls=(gateway.url,))

    assert out.status == "blocked"
    assert message in out.reason
    _assert_no_intent(tmp_path, sub)


def test_fallback_to_disallowed_plaintext_gateway_is_refused(tmp_path, kubo):
    chain, sub, envelope, cid = _setup(tmp_path)
    kubo.mode = "500"

    out = _consume(
        tmp_path, sub, ipfs_api_url=kubo.url, gateway_urls=("http://gateway.example.com",)
    )

    assert out.status == "blocked"
    assert "plaintext HTTP requires a private/loopback or allowlisted IPFS host" in out.reason
    _assert_no_intent(tmp_path, sub)


@pytest.fixture(autouse=True)
def isolated_gateway_dns(monkeypatch):
    # Public URL policy remains real; only this synthetic hostname resolves locally
    # inside the network-disabled test container. Never resolve external hosts.
    import socket
    import urllib.request
    original = socket.getaddrinfo
    def resolve(host, *args, **kwargs):
        if host == "fixture-gateway.example": host = "127.0.0.1"
        return original(host, *args, **kwargs)
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(bundle_ipfs, "_OPENER", urllib.request.build_opener(
        urllib.request.ProxyHandler({}), bundle_ipfs._NoRedirect()))
