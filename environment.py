"""Validator environment settings."""

from pathlib import Path
from urllib.parse import urlparse
import ipaddress
import logging
import re

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


def _reject_lan_bundle_fetch_url(url: str) -> None:
    """Fail closed if a bundle gateway URL targets RFC1918/loopback/link-local.

    Weight-bundle fetch must not use LAN/local IPs.
    DNS names (e.g. ipfs.rec4ll.com) are allowed; IP literals that are private are not.
    """
    parsed = urlparse(url)
    if (parsed.scheme not in ("http", "https") or parsed.port == 5001
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or "/api/v0" in parsed.path):
        raise ValueError("BUNDLE_GATEWAY_URLS must be public HTTP(S) content gateways, not admin RPC")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise ValueError(f"BUNDLE_GATEWAY_URLS entry missing host: {url!r}")
    if host in ("relay-gateway", "localhost") or host.endswith(".internal"):
        raise ValueError(
            f"BUNDLE_GATEWAY_URLS must not use LAN/bridge hostnames: {url!r}"
        )
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return  # DNS name — allowed (product uses public hostnames)
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified:
        raise ValueError(
            f"BUNDLE_GATEWAY_URLS must not use RFC1918/loopback addresses: {url!r}"
        )



def configured_ipfs_plaintext_hosts(
    *,
    bundle_ipfs_api_url: str = "",
    ipfs_read_url: str = "",
    bundle_gateway_urls: str = "",
    ipfs_plaintext_hosts: str = "",
) -> tuple[str, ...]:
    """Hostnames allowed for plaintext HTTP when IPFS_ALLOW_PLAINTEXT is true.

    OBSERVED (S-14a): collect hosts from configured Kubo/read/gateway URLs plus
    an optional CSV allowlist. Does not include arbitrary public origins.
    """
    hosts: list[str] = []

    def _add_url(url: str) -> None:
        u = (url or "").strip()
        if not u:
            return
        h = (urlparse(u).hostname or "").strip().lower()
        if h:
            hosts.append(h)

    def _add_host_or_url(raw: str) -> None:
        item = (raw or "").strip()
        if not item:
            return
        if "://" in item:
            _add_url(item)
            return
        h = item.lower()
        # strip accidental port from bare host:port
        if h.startswith("[") and "]" in h:
            h = h[1 : h.index("]")]
        elif ":" in h and h.count(":") == 1:
            h = h.split(":", 1)[0]
        if h:
            hosts.append(h)

    _add_url(bundle_ipfs_api_url)
    _add_url(ipfs_read_url)
    for raw in (bundle_gateway_urls or "").split(","):
        _add_url(raw)
    for raw in (ipfs_plaintext_hosts or "").split(","):
        _add_host_or_url(raw)
    return tuple(dict.fromkeys(hosts))


# The validator modules live at the repository root, so the checkout directory
# is the directory of this file. SETUP.md copies .env.example to .env there.
# Process environment variables take precedence over values from this file.
PACKAGE_BASE_PATH = Path(__file__).resolve().parent
PROJECT_BASE_PATH = PACKAGE_BASE_PATH
ENV_FILE_PATH = PROJECT_BASE_PATH / ".env"


# Mainnet profile pins. Network aliases never prove chain identity: the runtime still
# proves the actual RPC genesis against ALLOCATION_EXPECTED_GENESIS_HASH.
FINNEY_GENESIS_HASH = "0x2f0555cc76fc2840a25a6ea3b9637146806f1f44b090c175ffde2a7e5ab36c03"
MAINNET_MASTER_HOTKEY_SS58 = "5CDZ527X1CiStK1ViGkbC4EjVoouwiGW4ijRKRFfrLpQfftn"
MAINNET_NETUID = 31


# Settings removed with shadow mode, the score_version 10 allocator, the v3 network
# opt-ins and the retired test network. Declared on the model only so a leftover value
# is noticed (_validate_removed); mainnet still refuses the test-network ones.
REMOVED_SETTINGS = (
    "incentive_v3_mode",
    "incentive_v3_weights_netuids",
    "incentive_v3_testnet_353_opt_in",
    "settlement_checks_enabled",
    "incentive_v3_approved_testnet_genesis_hash",
    "incentive_v3_generation_benchmark_policy_file",
    "incentive_v3_mainnet_31_opt_in",
    "incentive_v3_approved_mainnet_genesis_hash",
    "empty_organic_burn_enabled",
    "organic_burn_residual",
    "organic_burn_apply_order",
    "organic_count_window_seconds",
    "organic_sustained_min_count",
    "organic_sustained_min_principals",
    "organic_sustained_min_databases",
    "organic_events_enabled",
    "dual_rank_enabled",
    "organic_router_preempt_enabled",
    "organic_dual_rank_enabled",
    "organic_routing_preempt_enabled",
    "reliability_rank_enabled",
)


# Values pydantic parses as boolean true (the removed SETTLEMENT_CHECKS_ENABLED was a bool).
_TRUE_VALUES = ("1", "true", "t", "yes", "y", "on")


def _is_mainnet(network: str, genesis_hash: str) -> bool:
    """Declared mainnet intent by network alias or the canonical Finney genesis pin."""
    net = (network or "").strip().lower()
    return (net in ("finney", "mainnet", "archive") or "finney" in net or "mainnet" in net
            or (genesis_hash or "").strip().lower() == FINNEY_GENESIS_HASH)


class EnvironmentVariables(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE_PATH,
        extra="ignore",
    )

    environment: str = ""
    log_level: str = "INFO"

    network: str = "finney"
    netuid: int = 31
    wallet_name: str = "validator"
    hotkey_name: str = "default"
    validator_poll_seconds: int = 12
    epoch_start_window_blocks: int = 3
    recall_api_url: str = "https://rec4ll.com/api"
    internal_api_key: str = ""
    master_validator_hotkey_ss58: str = ""

    ipfs_read_url: str = "http://ipfs.rec4ll.com:5001"

    # Allocation-v2 integration. Legacy is the default path.
    allocation_mode: str = "legacy"
    allocation_expected_genesis_hash: str = ""
    allocation_allow_plaintext: bool = False
    # IPFS plaintext is independent of score-fetch plaintext so enabling
    # http://ipfs.rec4ll.com:5001 does NOT also cleartext the INTERNAL_API_KEY
    # bearing v3 evidence fetch.
    # Default True for self-hosted IPFS (no TLS in front,
    # low data sensitivity). Set IPFS_ALLOW_PLAINTEXT=false for stricter deploys.
    # S-14a: when True, plaintext is still host-scoped — private/loopback OR
    # hosts derived from BUNDLE_IPFS_API_URL / IPFS_READ_URL / BUNDLE_GATEWAY_URLS
    # plus optional IPFS_PLAINTEXT_HOSTS (CSV). Arbitrary public hosts stay DENY.
    ipfs_allow_plaintext: bool = True
    # Optional CSV of extra hostnames allowed for plaintext IPFS when the flag
    # is true (bare host or URL). Empty = only configured URL hosts + private.
    ipfs_plaintext_hosts: str = ""

    # Integrated chain-bundle flow. chain-v2 is the only supported mode.
    bundle_mode: str = ""
    bundle_role: str = ""  # explicit "master" or "slave"; never inferred from URLs
    bundle_state_dir: str = "./bundle_state"
    ipfs_rotation_enabled: bool = False
    bundle_ipfs_api_url: str = ""  # Kubo /api/v0 (publish+fetch); product: ipfs.rec4ll.com:5001
    bundle_gateway_urls: str = ""  # optional CSV content gateways; LAN/RFC1918 forbidden

    # Weight cadence is blocks_since >= the live rate limit; epoch markers are telemetry.
    weight_loop_enabled: bool = True
    # Retained for compatibility; the rate-limit-only loop ignores this margin.
    weight_due_margin_blocks: int = 15
    # Age above tempo + grace raises an alert; the last verified vector is still set.
    weight_bundle_grace_blocks: int = 60
    # Above this age (blocks) a stale verified vector is no
    # longer re-set; the loop falls back to the owner/immune UID and alerts. 0 = never
    # (legacy behaviour: the last valid vector is re-set indefinitely).
    weight_max_stale_blocks: int = 0
    # Legacy compatibility setting. The enabled weight loop reuses valid stale
    # vectors and uses owner fallback when none is valid, regardless of this value.
    weight_failclosed_policy: str = "keep"

    # Optional wallet directory for a deploy-host local mount (empty = legacy default).
    wallet_path: str = ""

    # Incentive v3 / score_version 11 is the only scoring and weights path. Startup
    # still requires actual genesis, registered signer/permit and process-lock checks.
    incentive_v3_slot_window_hours: int = 48
    # Allocator state scope: empty (default) or "grounded-v1".
    incentive_v3_eligibility_scope: str = ""
    # Master allocation only: readiness share of each tier budget (rest is service), and
    # whether unearned units burn to the owner UID or are paid to miners pro rata.
    incentive_v3_readiness_split: float = 0.40
    incentive_v3_burn_unearned: bool = True
    # Master allocation only: how each tier's readiness pool is paid. "shares" (default)
    # pays every eligible miner by quality, at most one slot's worth each; "slots" pays
    # the four slot holders. Both are valid on every network, mainnet included.
    incentive_v3_readiness_mode: str = "shares"
    # Legacy compatibility setting; distinct registered/permitted hotkeys coexist.
    incentive_v3_setter_max_age_blocks: int = 1080

    # Removed settings (REMOVED_SETTINGS): raw strings, never parsed or used.
    incentive_v3_mode: str | None = None
    incentive_v3_weights_netuids: str | None = None
    incentive_v3_testnet_353_opt_in: str | None = None
    settlement_checks_enabled: str | None = None
    incentive_v3_approved_testnet_genesis_hash: str | None = None
    incentive_v3_generation_benchmark_policy_file: str | None = None
    incentive_v3_mainnet_31_opt_in: str | None = None
    incentive_v3_approved_mainnet_genesis_hash: str | None = None
    empty_organic_burn_enabled: str | None = None
    organic_burn_residual: str | None = None
    organic_burn_apply_order: str | None = None
    organic_count_window_seconds: str | None = None
    organic_sustained_min_count: str | None = None
    organic_sustained_min_principals: str | None = None
    organic_sustained_min_databases: str | None = None
    organic_events_enabled: str | None = None
    dual_rank_enabled: str | None = None
    organic_router_preempt_enabled: str | None = None
    organic_dual_rank_enabled: str | None = None
    organic_routing_preempt_enabled: str | None = None
    reliability_rank_enabled: str | None = None


    def ipfs_plaintext_allowed_hosts(self) -> tuple[str, ...]:
        """S-14a: hosts permitted for plaintext IPFS when the flag is true."""
        return configured_ipfs_plaintext_hosts(
            bundle_ipfs_api_url=self.bundle_ipfs_api_url,
            ipfs_read_url=self.ipfs_read_url,
            bundle_gateway_urls=self.bundle_gateway_urls,
            ipfs_plaintext_hosts=self.ipfs_plaintext_hosts,
        )

    @model_validator(mode="after")
    def _validate_allocation(self):
        mode = (self.allocation_mode or "").strip().lower()
        if mode not in ("legacy", "v2"):
            raise ValueError(
                f"ALLOCATION_MODE must be 'legacy' or 'v2', got {self.allocation_mode!r}"
            )
        if mode == "v2" and not self.allocation_expected_genesis_hash.strip():
            raise ValueError(
                "ALLOCATION_MODE=v2 requires ALLOCATION_EXPECTED_GENESIS_HASH"
            )
        self.allocation_mode = mode
        return self

    @model_validator(mode="after")
    def _validate_bundle(self):
        mode = (self.bundle_mode or "").strip().lower()
        if mode in ("", "off", "legacy"):
            raise ValueError(
                "BUNDLE_MODE=chain-v2 is the only supported mode "
                f"(got {self.bundle_mode!r}); the legacy weight-fetch path is removed"
            )
        if mode != "chain-v2":
            raise ValueError(
                f"BUNDLE_MODE must be 'chain-v2', got {self.bundle_mode!r}"
            )
        if self.allocation_mode != "v2":
            raise ValueError("BUNDLE_MODE=chain-v2 requires ALLOCATION_MODE=v2")
        if not self.master_validator_hotkey_ss58.strip():
            raise ValueError(
                "BUNDLE_MODE=chain-v2 requires MASTER_VALIDATOR_HOTKEY_SS58"
            )
        if not self.bundle_state_dir.strip():
            raise ValueError("BUNDLE_MODE=chain-v2 requires BUNDLE_STATE_DIR")
        role = (self.bundle_role or "").strip().lower()
        if role not in ("master", "slave"):
            raise ValueError(
                "BUNDLE_MODE=chain-v2 requires BUNDLE_ROLE to be 'master' or 'slave'"
            )
        if role == "master" and not self.bundle_ipfs_api_url.strip():
            raise ValueError(
                "BUNDLE_ROLE=master requires BUNDLE_IPFS_API_URL"
            )
        if role == "slave":
            if self.bundle_ipfs_api_url.strip():
                # Secondaries never contact the IPFS API; drop it so it cannot be used
                # or widen the plaintext host scope.
                logger.warning(
                    "BUNDLE_IPFS_API_URL is ignored on a secondary (BUNDLE_ROLE=slave); "
                    "secondaries fetch only from BUNDLE_GATEWAY_URLS. Leave it empty."
                )
                self.bundle_ipfs_api_url = ""
            if not self.bundle_gateway_urls.strip():
                raise ValueError("secondary requires public BUNDLE_GATEWAY_URLS")
        for raw in self.bundle_gateway_urls.split(","):
            u = raw.strip()
            if u:
                _reject_lan_bundle_fetch_url(u)
        if self.ipfs_rotation_enabled and role != "master":
            raise ValueError("IPFS_ROTATION_ENABLED is master-only")
        self.bundle_role = role
        self.bundle_mode = mode
        return self

    @model_validator(mode="after")
    def _validate_ipfs_plaintext(self):
        """Refuse http:// IPFS URLs that the runtime transport would refuse, on every network.

        Runtime fetch/publish allows plaintext only for loopback/private hosts or, with
        IPFS_ALLOW_PLAINTEXT=true, for the configured IPFS hosts, whatever the network. Any
        other http:// URL would start cleanly and then fail every fetch, so it is refused here.
        """
        from bundle_ipfs import IpfsError, _validate_url
        hosts = self.ipfs_plaintext_allowed_hosts()
        urls = [("BUNDLE_IPFS_API_URL", self.bundle_ipfs_api_url)]
        urls += [("BUNDLE_GATEWAY_URLS", u) for u in self.bundle_gateway_urls.split(",")]
        for name, raw in urls:
            url = raw.strip()
            if not url or urlparse(url).scheme != "http":
                continue
            try:
                _validate_url(url, allow_plaintext=self.ipfs_allow_plaintext, plaintext_hosts=hosts)
            except IpfsError as exc:
                raise ValueError(
                    f"{name} uses plaintext http:// for a non-loopback host, which "
                    "IPFS_ALLOW_PLAINTEXT=false refuses at runtime; use https:// or allow "
                    "plaintext for the configured IPFS hosts"
                ) from exc
        return self

    @model_validator(mode="after")
    def _validate_removed(self):
        """Settings removed with the legacy score paths: never silently run something else.

        INCENTIVE_V3_MODE=weights names exactly what this build always does, so it is
        accepted with a deprecation warning. off/shadow (or any other value) asked for a
        mode that no longer exists; starting anyway would set score_version 11 weights
        the operator did not choose, so they fail at startup with the fix in the message.
        The other removed switches only gated or tuned the removed paths: they are
        ignored with a warning, except a removed genesis pin that contradicts
        ALLOCATION_EXPECTED_GENESIS_HASH, which fails closed, and the removed test-network
        settings on mainnet, which _validate_mainnet_profile refuses.
        """
        if self.incentive_v3_mode is not None:
            mode = self.incentive_v3_mode.strip().lower()
            if mode == "weights":
                logger.warning(
                    "INCENTIVE_V3_MODE is deprecated: incentive v3 weights (score_version 11) is "
                    "the only scoring path. Delete INCENTIVE_V3_MODE from the configuration."
                )
            elif mode:
                raise ValueError(
                    f"INCENTIVE_V3_MODE={self.incentive_v3_mode!r} is no longer supported: incentive "
                    "v3 weights (score_version 11) is the only scoring path in this release. Delete "
                    "INCENTIVE_V3_MODE to run it, or keep the previous release to stay on score_version 10."
                )
        pin = (self.incentive_v3_approved_mainnet_genesis_hash or "").strip().lower()
        if pin and pin != (self.allocation_expected_genesis_hash or "").strip().lower():
            raise ValueError(
                "INCENTIVE_V3_APPROVED_MAINNET_GENESIS_HASH is removed and contradicts "
                "ALLOCATION_EXPECTED_GENESIS_HASH; delete it and keep the correct expected pin"
            )
        for name in REMOVED_SETTINGS:
            value = getattr(self, name)
            if name != "incentive_v3_mode" and value is not None and value.strip():
                logger.warning("%s is removed and ignored; delete it from the configuration", name.upper())
        return self

    @model_validator(mode="after")
    def _validate_incentive_v3(self):
        if int(self.weight_max_stale_blocks) < 0:
            raise ValueError("WEIGHT_MAX_STALE_BLOCKS must be >= 0 (0 = never fall back on age)")
        if int(self.incentive_v3_setter_max_age_blocks) <= 0:
            raise ValueError("INCENTIVE_V3_SETTER_MAX_AGE_BLOCKS must be > 0")
        expected_genesis = (self.allocation_expected_genesis_hash or "").strip()
        if re.fullmatch(r"0[xX][0-9a-fA-F]{64}", expected_genesis):
            # Canonical pin for the runtime genesis proof (block 0 hashes are lowercase).
            self.allocation_expected_genesis_hash = expected_genesis = expected_genesis.lower()
        if not (0.0 <= float(self.incentive_v3_readiness_split) <= 1.0):
            raise ValueError("INCENTIVE_V3_READINESS_SPLIT must be between 0 and 1")
        from v3_params import READINESS_MODES
        readiness_mode = (self.incentive_v3_readiness_mode or "").strip().lower()
        if readiness_mode not in READINESS_MODES:
            raise ValueError(
                f"INCENTIVE_V3_READINESS_MODE must be {'|'.join(READINESS_MODES)}, "
                f"got {self.incentive_v3_readiness_mode!r}"
            )
        self.incentive_v3_readiness_mode = readiness_mode
        if self.incentive_v3_eligibility_scope not in ("", "grounded-v1"):
            raise ValueError("invalid INCENTIVE_V3_ELIGIBILITY_SCOPE (empty or grounded-v1)")
        if _is_mainnet(self.network, expected_genesis):
            self._validate_mainnet_profile((self.network or "").strip().lower(), expected_genesis)
        return self

    def _validate_mainnet_profile(self, network: str, expected_genesis: str) -> None:
        """Mainnet (declared by network alias or the Finney pin) runs only the production profile.

        Declared intent only; the runtime must still prove the actual RPC genesis.
        """
        if network in ("test", "testnet", "archive"):
            raise ValueError("mainnet cannot use a testnet or archive network")
        if int(self.netuid) != MAINNET_NETUID:
            raise ValueError(f"mainnet is restricted to netuid {MAINNET_NETUID}")
        if expected_genesis != FINNEY_GENESIS_HASH:
            raise ValueError("mainnet requires the canonical Finney ALLOCATION_EXPECTED_GENESIS_HASH")
        # Removed test-network settings (REMOVED_SETTINGS) left set are refused on mainnet,
        # not merely ignored, so a test configuration can never start against Finney.
        if (self.incentive_v3_approved_testnet_genesis_hash or "").strip():
            raise ValueError("mainnet must not carry testnet genesis settings")
        if (self.incentive_v3_generation_benchmark_policy_file or "").strip():
            raise ValueError("synthetic testnet generation benchmark is forbidden on mainnet")
        if (self.settlement_checks_enabled or "").strip().lower() in _TRUE_VALUES:
            raise ValueError("SETTLEMENT_CHECKS_ENABLED is refused on mainnet")
        if self.master_validator_hotkey_ss58 != MAINNET_MASTER_HOTKEY_SS58:
            raise ValueError("mainnet requires the official Recall master hotkey")
        if self.incentive_v3_slot_window_hours != 48:
            raise ValueError("mainnet requires the production 48-hour slot window")
        # The allocator reads the process environment directly. Validate that view
        # as well so .env/explicit settings cannot mask a shortened runtime window.
        from v3_params import slot_window_hours
        if slot_window_hours() != 48:
            raise ValueError("mainnet runtime requires the production 48-hour slot window")
        self.allocation_expected_genesis_hash = FINNEY_GENESIS_HASH
