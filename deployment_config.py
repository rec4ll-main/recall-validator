"""Side-effect-free deployment checks shared by the validator and one-shot tools.

Importing this module loads no wallet, opens no connection, creates no files and
imports no signing or chain runtime (only ``api_transport`` and the standard
library). ``main`` re-exports ``validate_deployment_secrets`` unchanged in name.
"""

from api_transport import METAGRAPH_SYNC_PATH, ApiDestinationError, validated_endpoint


def validate_deployment_secrets(env):
    """Fail fast unless this process is chain-v2.

    chain-v2 master requires a real (non-placeholder) internal API key and an
    explicitly-set score API URL. chain-v2 slave does not call the web API and
    needs no API key. Role, genesis, and master identity are enforced by the
    environment model. Secret values are never logged.

    Chain-v2 startup requires explicit NETUID and NETWORK in env.
    Refuse silent finney/31 defaults.
    """
    fields_set = getattr(env, "model_fields_set", frozenset())

    def _require_explicit_netuid_network(context: str) -> None:
        if "netuid" not in fields_set:
            raise RuntimeError(
                "NETUID must be explicitly set for "
                + context
                + " (refusing silent code default; .env.example documents mainnet 31)"
            )
        if "network" not in fields_set:
            raise RuntimeError(
                "NETWORK must be explicitly set for "
                + context
                + " (refusing silent finney default)"
            )

    if getattr(env, "bundle_mode", "") != "chain-v2":
        raise RuntimeError("BUNDLE_MODE=chain-v2 is the only supported mode")
    _require_explicit_netuid_network("chain-v2")
    if env.bundle_role == "master":
        if not env.internal_api_key or _is_placeholder(env.internal_api_key):
            raise RuntimeError(
                "INTERNAL_API_KEY must be a real secret for chain-v2 master "
                "(placeholder or empty detected)"
            )
        if "recall_api_url" not in fields_set or not env.recall_api_url.strip():
            raise RuntimeError(
                "RECALL_API_URL must be explicitly set for chain-v2 master "
                "(score API URL must not fall back to a public default)"
            )
        # The internal key travels to this URL. Remote destinations must be HTTPS;
        # only a literal loopback IP may use plaintext. ALLOCATION_ALLOW_PLAINTEXT
        # does not relax this check.
        try:
            validated_endpoint(env.recall_api_url, METAGRAPH_SYNC_PATH)
        except ApiDestinationError as exc:
            raise RuntimeError(f"RECALL_API_URL refused for chain-v2 master: {exc}") from None


def _is_placeholder(value):
    lower = value.lower()
    for marker in (
        "change-me",
        "changeme",
        "local-dev",
        "local-api",
        "placeholder",
    ):
        if marker in lower:
            return True
    return False
