from __future__ import annotations

import http.server
import json
import threading
import urllib.error
import urllib.request

import pytest

from bundle_ipfs import (
    IpfsError,
    compute_raw_cid,
    fetch_block,
    fetch_gateway,
    is_valid_raw_cid,
    pin_block,
    publish_block,
    read_back_block,
)

PAYLOAD = b"hello ipfs block"


def _extract_multipart(body: bytes, boundary: str) -> bytes:
    marker = f"--{boundary}".encode()
    parts = body.split(marker)
    for part in parts:
        if b'name="file"' in part:
            return part.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n", 1)[0]
    raise AssertionError("no file part in multipart body")


class _Kubo(http.server.BaseHTTPRequestHandler):
    blocks = {}
    pins = set()
    captured = {}

    def log_message(self, *args):
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path.startswith("/api/v0/block/put"):
            content_type = self.headers["Content-Type"]
            assert content_type.startswith("multipart/form-data; boundary=")
            boundary = content_type.split("boundary=", 1)[1]
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            _Kubo.captured["block_put_content_type"] = content_type
            data = _extract_multipart(raw, boundary)
            _Kubo.captured["block_put_has_file_part"] = True
            cid = compute_raw_cid(data)
            _Kubo.blocks[cid] = data
            self._json({"Key": cid, "Size": len(data)})
        elif self.path.startswith("/api/v0/pin/add"):
            cid = self.path.split("arg=", 1)[1]
            _Kubo.pins.add(cid)
            self._json({"Pins": [cid]})
        elif self.path.startswith("/api/v0/block/get"):
            _Kubo.captured["block_get_method"] = "POST"
            cid = self.path.split("arg=", 1)[1]
            data = _Kubo.blocks.get(cid)
            if data is None:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.ipld.raw")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self._json({"Message": "no such command"}, status=404)

    def do_GET(self):
        if self.path.startswith("/api/v0/block/get"):
            _Kubo.captured["block_get_method"] = "GET"
            self.send_response(405)
            self.end_headers()
            return
        elif self.path.startswith("/ipfs/"):
            cid = self.path[len("/ipfs/"):]
            data = _Kubo.blocks.get(cid)
            if data is None:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture()
def kubo():
    _Kubo.blocks = {}
    _Kubo.pins = set()
    _Kubo.captured = {}
    server = http.server.HTTPServer(("127.0.0.1", 0), _Kubo)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_compute_raw_cid_roundtrip_and_validation():
    cid = compute_raw_cid(PAYLOAD)
    assert cid.startswith("b")
    assert is_valid_raw_cid(cid)
    assert not is_valid_raw_cid("Qm" + "A" * 44)
    assert not is_valid_raw_cid("b" + "!" * 58)
    assert not is_valid_raw_cid("bafySTALE")
    assert not is_valid_raw_cid("")


def test_publish_block_sends_multipart_with_file_part(kubo):
    cid = publish_block(kubo, PAYLOAD)
    assert cid == compute_raw_cid(PAYLOAD)
    assert _Kubo.captured["block_put_has_file_part"] is True
    assert _Kubo.captured["block_put_content_type"].startswith("multipart/form-data")


def test_pin_and_read_back(kubo):
    cid = publish_block(kubo, PAYLOAD)
    pin_block(kubo, cid)
    assert cid in _Kubo.pins
    assert read_back_block(kubo, cid, PAYLOAD) == PAYLOAD
    assert _Kubo.captured["block_get_method"] == "POST"


def test_fetch_block_and_gateway_verify_hash(kubo):
    cid = publish_block(kubo, PAYLOAD)
    assert fetch_block(kubo, cid) == PAYLOAD
    assert _Kubo.captured["block_get_method"] == "POST"
    assert fetch_gateway([kubo], cid) == PAYLOAD


def test_block_get_rejects_get_method_405(kubo):
    cid = publish_block(kubo, PAYLOAD)
    req = urllib.request.Request(kubo + "/api/v0/block/get?arg=" + cid, method="GET")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 405
    assert _Kubo.captured["block_get_method"] == "GET"
    assert read_back_block(kubo, cid, PAYLOAD) == PAYLOAD


def test_publish_rejects_returned_cid_mismatch():
    class _Bad(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers["Content-Length"])
            self.rfile.read(n)
            body = json.dumps({"Key": "bafyWRONG"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.HTTPServer(("127.0.0.1", 0), _Bad)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with pytest.raises(IpfsError):
            publish_block(url, PAYLOAD)
    finally:
        server.shutdown()


def test_rejects_userinfo_and_bad_scheme():
    with pytest.raises(IpfsError):
        publish_block("ftp://127.0.0.1:5001", PAYLOAD)
    with pytest.raises(IpfsError):
        publish_block("http://user:pass@127.0.0.1:5001", PAYLOAD)


def test_rejects_plaintext_http_public_host_by_default():
    """Low-level publish_block kwarg default remains False (fail-closed if omitted)."""
    with pytest.raises(IpfsError):
        publish_block("http://example.com:5001", PAYLOAD)


def test_explicit_false_blocks_public_plaintext_http():
    """IPFS_ALLOW_PLAINTEXT=false / allow_plaintext=False still blocks public HTTP."""
    with pytest.raises(IpfsError):
        publish_block("http://example.com:5001", PAYLOAD, allow_plaintext=False)


def test_s14a_allow_plaintext_true_still_denies_arbitrary_public_host():
    """S-14a: IPFS_ALLOW_PLAINTEXT=true must NOT open arbitrary public hosts."""
    from bundle_ipfs import _validate_url

    with pytest.raises(IpfsError):
        _validate_url("http://example.com:5001", allow_plaintext=True)
    with pytest.raises(IpfsError):
        publish_block("http://example.com:5001", PAYLOAD, allow_plaintext=True)


def test_s14a_allow_plaintext_true_allows_configured_ipfs_host():
    """Configured/self-hosted host (e.g. ipfs.rec4ll.com) ALLOW when flag true."""
    from bundle_ipfs import _validate_url

    _validate_url(
        "http://ipfs.rec4ll.com:5001",
        allow_plaintext=True,
        plaintext_hosts=("ipfs.rec4ll.com",),
    )
    _validate_url(
        "http://ipfs.example.internal:5001",
        allow_plaintext=True,
        plaintext_hosts=("ipfs.example.internal",),
    )


def test_s14a_explicit_false_blocks_public_even_if_allowlisted():
    """Explicit false still blocks public plaintext even when host is configured."""
    from bundle_ipfs import _validate_url

    with pytest.raises(IpfsError):
        _validate_url(
            "http://ipfs.rec4ll.com:5001",
            allow_plaintext=False,
            plaintext_hosts=("ipfs.rec4ll.com",),
        )
    with pytest.raises(IpfsError):
        publish_block(
            "http://example.com:5001",
            PAYLOAD,
            allow_plaintext=False,
            plaintext_hosts=("example.com",),
        )


def test_s14a_private_loopback_ok_regardless_of_flag():
    """Private/loopback plaintext stays allowed (flag true or false)."""
    from bundle_ipfs import _validate_url

    for flag in (True, False):
        _validate_url("http://127.0.0.1:5001", allow_plaintext=flag)
        _validate_url("http://10.0.0.5:5001", allow_plaintext=flag)
        _validate_url("http://localhost:5001", allow_plaintext=flag)


def test_redirect_is_refused():
    class _Redirect(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:9999/elsewhere")
            self.end_headers()

    server = http.server.HTTPServer(("127.0.0.1", 0), _Redirect)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with pytest.raises(IpfsError):
            publish_block(url, PAYLOAD)
    finally:
        server.shutdown()


def test_invalid_cid_rejected_before_url_build(kubo):
    with pytest.raises(IpfsError):
        pin_block(kubo, "bafySTALE-invalid")
    with pytest.raises(IpfsError):
        fetch_gateway([kubo], "../../etc/passwd")
