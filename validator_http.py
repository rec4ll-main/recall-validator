"""Public HTTP identity and shared transport helpers for the validator's Recall API requests."""

import ipaddress
import urllib.request

# A stable, truthful service product token. Do not add wallet, host, platform,
# credential or operator details, or impersonate an interactive browser.
VALIDATOR_USER_AGENT = "RecallValidator"


class ApiFetchError(Exception):
    """A fetch, transport, or validation failure for a Recall API endpoint."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ApiFetchError(f"redirect not allowed (HTTP {code})")


# Never follow a redirect: the request carries the internal API key.
NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect())


def is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
