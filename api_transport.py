"""Side-effect-free destination and transport policy for key-bearing API requests.

The internal API key and Epistula headers may only travel to an encrypted
destination. This module is pure standard library: importing it creates no files,
opens no connections, loads no wallet and imports no signing or chain runtime, so
startup checks, the master registry refresh and one-shot tools can share it.

Policy (``validate_api_url``):

* ``https://`` is required for every remote destination.
* Plaintext ``http://`` is allowed only when the host is a literal loopback IP
  (``127.0.0.0/8`` or ``::1``), the documented local-test exception. Host names such
  as ``localhost`` and non-canonical IP spellings are not literal loopback.
* ``ALLOCATION_ALLOW_PLAINTEXT`` is deliberately not a parameter here, so it cannot
  relax this policy.
* Userinfo, query strings, fragments, whitespace/control characters, backslashes,
  invalid ports and hosts outside ``[a-z0-9.-:]`` are refused.
* Errors never echo the URL or any part of it.

``send_request`` re-validates the final request URL immediately before opening it
and never follows redirects (a redirect is a transport failure).
"""

from __future__ import annotations

import ipaddress
import json
import re
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit

METAGRAPH_SYNC_PATH = "/api/validator/metagraph-sync"

_HOST_RE = re.compile(r"[a-z0-9.\-:]+")


class ApiDestinationError(ValueError):
    """The API destination is refused. The message never contains the URL."""


@dataclass(frozen=True, slots=True)
class ApiDestination:
    """A validated destination. ``plaintext_loopback`` marks the test exception."""

    scheme: str
    host: str
    port: int | None
    plaintext_loopback: bool


def is_literal_loopback(host: str) -> bool:
    """True only for a canonical loopback IP literal (no names, no zone ids)."""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_api_url(url: object) -> ApiDestination:
    """Return the validated destination or raise ``ApiDestinationError``."""
    if not isinstance(url, str) or not url.strip():
        raise ApiDestinationError("API URL is empty")
    if any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in url) or "\\" in url:
        raise ApiDestinationError("API URL contains whitespace, control or backslash characters")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise ApiDestinationError("API URL is malformed") from None
    scheme = parts.scheme.lower()
    if scheme not in ("https", "http"):
        raise ApiDestinationError("API URL scheme must be https")
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        raise ApiDestinationError("API URL must not contain userinfo")
    if "?" in url or "#" in url:
        raise ApiDestinationError("API URL must not carry a query or fragment")
    host = parts.hostname or ""
    if not host or not _HOST_RE.fullmatch(host):
        raise ApiDestinationError("API URL host is missing or malformed")
    if port == 0:
        raise ApiDestinationError("API URL is malformed")
    loopback = is_literal_loopback(host)
    if scheme == "http" and not loopback:
        raise ApiDestinationError(
            "remote API destinations must use https "
            "(plaintext is allowed only for a literal loopback IP)"
        )
    return ApiDestination(scheme=scheme, host=host, port=port, plaintext_loopback=scheme == "http")


def api_endpoint_url(api_url: str, path: str) -> str:
    """Join API base URLs that may already include /api."""
    base = api_url.rstrip("/")
    endpoint = "/" + path.lstrip("/")
    if base.endswith("/api") and endpoint.startswith("/api/"):
        endpoint = endpoint.removeprefix("/api")
    return base + endpoint


def validated_endpoint(api_url: str, path: str) -> str:
    """Validate the configured base and the joined endpoint; return the endpoint."""
    validate_api_url(api_url)
    endpoint = api_endpoint_url(api_url, path)
    validate_api_url(endpoint)
    return endpoint


def metagraph_sync_body(
    active_miners, netuid: int, metagraph_block: int, authoritative_empty: bool,
    coldkeys=(),
) -> bytes:
    """The exact metagraph-sync request body (shared by every producer).

    ``coldkeys``, when given, is aligned with ``active_miners`` and adds each miner's chain
    coldkey (omitted when the lengths differ or a value is empty)."""
    coldkeys = tuple(coldkeys or ())
    aligned = len(coldkeys) == len(active_miners)
    miners = []
    for i, (uid, hotkey) in enumerate(active_miners):
        entry = {"uid": uid, "hotkey": hotkey}
        if aligned and coldkeys[i]:
            entry["coldkey"] = coldkeys[i]
        miners.append(entry)
    return json.dumps(
        {
            "miners": miners,
            "netuid": netuid,
            "metagraph_block": metagraph_block,
            "authoritative_empty": authoritative_empty,
        }
    ).encode("utf-8")


def internal_key_headers(internal_api_key: str, request_uuid: str = "") -> dict[str, str]:
    """Headers for internal-key-only authentication (no wallet, no signature)."""
    headers = {"Content-Type": "application/json", "Epistula-Uuid": request_uuid or str(uuid.uuid4())}
    if internal_api_key:
        headers["X-Internal-API-Key"] = internal_api_key
    return headers


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse HTTP redirects so key-bearing headers never reach another origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, f"redirect not allowed (HTTP {code})", headers, fp)


# HTTPS keeps the process proxy settings (a proxy only tunnels the TLS stream). The
# plaintext loopback exception never goes through a proxy, which would otherwise see
# the key in clear text.
NO_REDIRECT_OPENER = urllib.request.build_opener(NoRedirectHandler())
_LOOPBACK_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirectHandler())


def send_request(request: urllib.request.Request, *, timeout, urlopen=None):
    """Re-validate ``request.full_url`` immediately before opening it, then open it.

    ``urlopen`` lets tests inject a transport; a refused destination raises
    ``ApiDestinationError`` before the transport is called.
    """
    destination = validate_api_url(request.full_url)
    if urlopen is None:
        urlopen = (_LOOPBACK_OPENER if destination.plaintext_loopback else NO_REDIRECT_OPENER).open
    return urlopen(request, timeout=timeout)


def describe_transport_error(exc: BaseException) -> str:
    """A secret-free, URL-free description of a transport failure."""
    if isinstance(exc, ApiDestinationError):
        return str(exc)
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return f"connection failed ({type(exc.reason).__name__})"
    return type(exc).__name__
