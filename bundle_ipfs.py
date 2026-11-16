"""Raw CIDv1 SHA-256 IPFS block publish/pin/readback and read-only gateway fetch.

Fetch path: Kubo API ``fetch_block`` against the configured API endpoint
(e.g. ``http://ipfs.example.invalid:5001``), not a content-gateway bridge.

Publishes through the official Kubo ``/api/v0/block/put`` as multipart/form-data
(``file`` part, ``cid-codec=raw&mhtype=sha2-256``), pins via ``/api/v0/pin/add``, and
reads the block back with a real GET before the pointer is trusted. URLs are strict:
http(s) only, no userinfo, plaintext HTTP requires a loopback/private host or
(when ``allow_plaintext``) an allowlisted configured IPFS host — never arbitrary
public origins — and redirects are refused. Raw CIDs are validated before any
URL is built so a CID can never inject query/path syntax.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Sequence
from urllib.parse import urlparse

MAX_BYTES = 1 << 20
TIMEOUT_SECONDS = 10
# Backoff before each re-attempt of a transient failure (2 retries, ~20 s added).
TRANSIENT_RETRY_DELAYS = (5.0, 15.0)
RAW_CODEC = 0x55
SHA2_256 = 0x12
DIGEST_LEN = 0x20
_B32 = frozenset("abcdefghijklmnopqrstuvwxyz234567")


class IpfsError(Exception):
    """A publish, pin, fetch, or CID verification failure."""


def _varint(n: int) -> bytes:
    out = bytearray()
    while n >= 0x80:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n)
    return bytes(out)


def compute_raw_cid(data: bytes) -> str:
    """Compute the raw CIDv1 SHA2-256 CID for ``data`` (multibase base32, lowercase)."""
    multihash = _varint(SHA2_256) + _varint(DIGEST_LEN) + hashlib.sha256(data).digest()
    cid = _varint(1) + _varint(RAW_CODEC) + multihash
    return "b" + base64.b32encode(cid).decode("ascii").lower().rstrip("=")


def is_valid_raw_cid(cid: object) -> bool:
    """Strictly validate a raw CIDv1 SHA2-256 base32 CID string.

    Returns False for anything that is not exactly ``b`` + lowercase base32 of the
    36-byte CIDv1 (version 1, raw codec, sha2-256, 32-byte digest).
    """
    if not isinstance(cid, str) or not cid or len(cid) > 128:
        return False
    if cid[0] != "b" or any(c not in _B32 for c in cid[1:]):
        return False
    try:
        padded = cid[1:].upper() + "=" * (-len(cid[1:]) % 8)
        raw = base64.b32decode(padded)
    except Exception:
        return False
    return len(raw) == 36 and raw[:4] == bytes((1, RAW_CODEC, SHA2_256, DIGEST_LEN))


def _is_private_or_loopback_host(host: str) -> bool:
    """True for loopback/private IP literals and localhost DNS names."""
    h = (host or "").strip().lower()
    if not h:
        return False
    if h == "localhost" or h.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    return bool(ip.is_loopback or ip.is_private)


def _plaintext_host_set(plaintext_hosts: Sequence[str]) -> frozenset[str]:
    return frozenset(h.strip().lower() for h in plaintext_hosts if h and h.strip())


def _validate_url(
    url: str,
    *,
    allow_plaintext: bool,
    plaintext_hosts: Sequence[str] = (),
) -> None:
    """Reject unsafe IPFS URLs.

    HTTPS is always fine (no userinfo). Plaintext HTTP is allowed only for:
    - loopback/private hosts (always), or
    - when ``allow_plaintext`` is True, hosts in ``plaintext_hosts``
      (configured/self-hosted IPFS API hosts — S-14a host scope).

    ``allow_plaintext=True`` alone must NOT open arbitrary public hosts
    (e.g. ``http://example.com:5001`` is DENY without an allowlist entry).
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise IpfsError("unsupported URL scheme")
    if parsed.username or parsed.password:
        raise IpfsError("URL userinfo is not allowed")
    if parsed.scheme != "http":
        return
    host = (parsed.hostname or "").strip().lower()
    if _is_private_or_loopback_host(host):
        return
    if allow_plaintext and host and host in _plaintext_host_set(plaintext_hosts):
        return
    raise IpfsError(
        "plaintext HTTP requires a private/loopback or allowlisted IPFS host"
    )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise IpfsError(f"redirect not allowed (HTTP {code})")


_OPENER = urllib.request.build_opener(_NoRedirect())


def _request(
    url: str,
    *,
    method: str = "GET",
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    urlopen: Callable | None = None,
) -> bytes:
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    opener = urlopen if urlopen is not None else _OPENER.open
    try:
        with opener(req, timeout=TIMEOUT_SECONDS) as resp:
            return resp.read(MAX_BYTES + 1)
    except IpfsError:
        raise
    except urllib.error.HTTPError as exc:
        raise IpfsError(f"IPFS HTTP {exc.code}") from exc
    except (OSError, urllib.error.URLError) as exc:
        raise IpfsError(f"IPFS request failed: {exc}") from exc


def is_transient(exc: BaseException) -> bool:
    """True only for a timeout, a connection error, or an HTTP 5xx from the node.

    HTTP 4xx, refused redirects, bad JSON, CID/byte mismatches and URL policy
    errors are permanent and must never be retried.
    """
    cause = exc.__cause__ if isinstance(exc, IpfsError) else exc
    if isinstance(cause, urllib.error.HTTPError):
        return cause.code >= 500
    if isinstance(cause, urllib.error.URLError):
        cause = cause.reason
    return isinstance(cause, (TimeoutError, ConnectionError))


def retry_transient(
    op: Callable[[], object],
    *,
    delays: Sequence[float] = TRANSIENT_RETRY_DELAYS,
    on_retry: Callable[[IpfsError, int, float], None] | None = None,
):
    """Run ``op``; re-run it after each delay while it fails transiently.

    Only safe for idempotent IPFS steps (content-addressed put, pin, read-back).
    A permanent error, or the last transient one, is raised unchanged.
    """
    for attempt, delay in enumerate((*delays, None), start=1):
        try:
            return op()
        except IpfsError as exc:
            if delay is None or not is_transient(exc):
                raise
            if on_retry is not None:
                on_retry(exc, attempt, delay)
            time.sleep(delay)


def _json(body: bytes) -> dict:
    if len(body) > MAX_BYTES:
        raise IpfsError("IPFS response exceeded 1 MiB")
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IpfsError("IPFS invalid JSON response") from exc
    if not isinstance(value, dict):
        raise IpfsError("IPFS response is not an object")
    return value


def _multipart_file(boundary: str, data: bytes) -> bytes:
    head = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="block"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode("ascii")
    tail = f"\r\n--{boundary}--\r\n".encode("ascii")
    return head + data + tail


def publish_block(
    api_url: str,
    data: bytes,
    *,
    urlopen: Callable | None = None,
    allow_plaintext: bool = False,
    plaintext_hosts: Sequence[str] = (),
) -> str:
    """Multipart ``block/put`` (cid-codec=raw, mhtype=sha2-256); returns the CID."""
    if not isinstance(data, bytes):
        raise IpfsError("block data must be bytes")
    if len(data) > MAX_BYTES:
        raise IpfsError("block exceeds 1 MiB")
    _validate_url(
        api_url, allow_plaintext=allow_plaintext, plaintext_hosts=plaintext_hosts
    )
    expected = compute_raw_cid(data)
    boundary = uuid.uuid4().hex
    body = _multipart_file(boundary, data)
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    raw = _request(
        api_url.rstrip("/") + "/api/v0/block/put?cid-codec=raw&mhtype=sha2-256",
        method="POST",
        data=body,
        headers=headers,
        urlopen=urlopen,
    )
    key = _json(raw).get("Key")
    if key != expected:
        raise IpfsError(f"returned CID {key!r} != recomputed {expected!r}")
    return key


def pin_block(
    api_url: str,
    cid: str,
    *,
    urlopen: Callable | None = None,
    allow_plaintext: bool = False,
    plaintext_hosts: Sequence[str] = (),
) -> None:
    """Pin a CID via ``pin/add`` and confirm the pin reports the CID."""
    if not is_valid_raw_cid(cid):
        raise IpfsError("invalid raw CID")
    _validate_url(
        api_url, allow_plaintext=allow_plaintext, plaintext_hosts=plaintext_hosts
    )
    raw = _request(
        api_url.rstrip("/") + "/api/v0/pin/add?arg=" + cid,
        method="POST",
        urlopen=urlopen,
    )
    pins = _json(raw).get("Pins")
    if not pins or cid not in pins:
        raise IpfsError("pin did not report the CID")


def fetch_block(
    api_url: str,
    cid: str,
    *,
    urlopen: Callable | None = None,
    allow_plaintext: bool = False,
    plaintext_hosts: Sequence[str] = (),
) -> bytes:
    """POST a block back via ``/api/v0/block/get`` and require the bytes hash to ``cid``.

    Kubo's admin RPC serves ``/api/v0/block/get`` as POST only (GET returns 405); the
    content gateway (``fetch_gateway``) is the GET path.
    """
    if not is_valid_raw_cid(cid):
        raise IpfsError("invalid raw CID")
    _validate_url(
        api_url, allow_plaintext=allow_plaintext, plaintext_hosts=plaintext_hosts
    )
    body = _request(
        api_url.rstrip("/") + "/api/v0/block/get?arg=" + cid,
        method="POST",
        urlopen=urlopen,
    )
    if len(body) > MAX_BYTES:
        raise IpfsError("fetched block exceeds 1 MiB")
    if compute_raw_cid(body) != cid:
        raise IpfsError("fetched bytes do not hash to the requested CID")
    return body


def read_back_block(
    api_url: str,
    cid: str,
    expected: bytes,
    *,
    urlopen: Callable | None = None,
    allow_plaintext: bool = False,
    plaintext_hosts: Sequence[str] = (),
) -> bytes:
    """POST-read the block back and require exact byte and CID equality with ``expected``."""
    body = fetch_block(
        api_url,
        cid,
        urlopen=urlopen,
        allow_plaintext=allow_plaintext,
        plaintext_hosts=plaintext_hosts,
    )
    if body != expected:
        raise IpfsError("read-back bytes differ from published bytes")
    return body


def fetch_gateway(
    gateway_urls: Sequence[str],
    cid: str,
    *,
    urlopen: Callable | None = None,
    allow_plaintext: bool = False,
    plaintext_hosts: Sequence[str] = (),
) -> bytes:
    """Read-only gateway GET ``/ipfs/<cid>`` with byte/CID equality, no redirects."""
    if not is_valid_raw_cid(cid):
        raise IpfsError("invalid raw CID")
    last: Exception | None = None
    for gateway in gateway_urls:
        try:
            _validate_url(
                gateway,
                allow_plaintext=allow_plaintext,
                plaintext_hosts=plaintext_hosts,
            )
            body = _request(gateway.rstrip("/") + "/ipfs/" + cid, urlopen=urlopen)
            if len(body) > MAX_BYTES:
                raise IpfsError("fetched block exceeds 1 MiB")
            if compute_raw_cid(body) != cid:
                raise IpfsError("fetched bytes do not hash to the requested CID")
            return body
        except IpfsError as exc:
            last = exc
    raise IpfsError(f"all gateways failed: {last}") from last


def block_is_pinned(api_url: str, cid: str, **kwargs) -> bool:
    """Inspect this exact CID only. Unknown/error responses fail closed."""
    if not is_valid_raw_cid(cid):
        raise IpfsError('invalid raw CID')
    _validate_url(api_url, allow_plaintext=kwargs.get('allow_plaintext', False),
                  plaintext_hosts=kwargs.get('plaintext_hosts', ()))
    try:
        body = _request(api_url.rstrip('/') + '/api/v0/pin/ls?arg=' + cid + '&type=all',
                        method='POST', urlopen=kwargs.get('urlopen'))
    except IpfsError as exc:
        cause = exc.__cause__
        if isinstance(cause, urllib.error.HTTPError) and cause.code == 500:
            result = _json(cause.read(MAX_BYTES + 1))
            if result.get('Message') in (f"path '{cid}' is not pinned", f"path '/ipfs/{cid}' is not pinned"):
                return False
        raise
    keys = _json(body).get('Keys')
    if not isinstance(keys, dict) or cid not in keys:
        raise IpfsError('pin status did not report requested CID')
    return True


def unpin_block(api_url: str, cid: str, **kwargs) -> None:
    """Remove only a ledger-authorized pin. Never block/rm or repo/gc."""
    if not is_valid_raw_cid(cid):
        raise IpfsError('invalid raw CID')
    _validate_url(api_url, allow_plaintext=kwargs.get('allow_plaintext', False),
                  plaintext_hosts=kwargs.get('plaintext_hosts', ()))
    body = _request(api_url.rstrip('/') + '/api/v0/pin/rm?arg=' + cid,
                    method='POST', urlopen=kwargs.get('urlopen'))
    pins = _json(body).get('Pins')
    if not isinstance(pins, list) or cid not in pins:
        raise IpfsError('unpin did not report requested CID')
