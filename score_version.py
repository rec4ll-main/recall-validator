"""The one score_version this validator build supports, shared by every layer.

Incentive v3 (score_version 11, ``readiness_service_v3``) is the only scoring and
weights path. recall-web serves the v3 evidence for it from
``POST /api/validator/v3/metrics``; the master allocates it with ``allocation_v3``
and signs a score_version 11 envelope, and every validator accepts only that version.
The same single version is enforced at every boundary:

1. API client (``v3_metrics_client``): the fetched evidence.
2. Master envelope build (``v3_runtime``): the signed payload.
3. Signed codec (``bundle_v2_types``): signing and parsing an envelope.
4. Slave consume (``bundle_consume``), which parses through the codec.

There is no list of accepted legacy versions and no relabelling: a v10 (or older)
envelope is refused for *new* construction and submission, never treated as v11.
The sole exception is journal recovery of an already-persisted, digest-bound
historical envelope (see ``parse_bundle_for_journal_recovery``): that path may read
uids/weights of a known historical profile for applied/pending/expired settlement
only, so operations left pending by a score_version 10 build still settle after the
upgrade. See SETUP.md "Upgrading from a release with INCENTIVE_V3_MODE".

Cross-repository pin: ``tests/contracts/score_version_contract.json`` records the
recall-web release SHA this build was coordinated against. The contract test
fails closed if that pin and ``SUPPORTED_SCORE_VERSION`` drift apart.
"""

from __future__ import annotations

from v3_params import V3_EVIDENCE_WINDOW_SECONDS, V3_SCORE_KIND, V3_SCORE_VERSION

SUPPORTED_SCORE_VERSION = V3_SCORE_VERSION

# (score_version, score_kind, evidence_window_seconds) of the supported profile.
SUPPORTED_PROFILE = (SUPPORTED_SCORE_VERSION, V3_SCORE_KIND, V3_EVIDENCE_WINDOW_SECONDS)

# Every profile a persisted envelope may carry. Only journal recovery reads the
# historical entries; nothing in this build signs or accepts them for submission.
PROFILES = {
    "10": ("10", "window_quality_v1", 3600),
    SUPPORTED_SCORE_VERSION: SUPPORTED_PROFILE,
}


class UnsupportedScoreVersion(ValueError):
    """A score_version this build was not written for."""


def active_score_version() -> str:
    return SUPPORTED_SCORE_VERSION


def active_profile() -> tuple[str, str, int]:
    """(score_version, score_kind, evidence_window_seconds) this process signs/accepts."""
    return SUPPORTED_PROFILE


def require_supported_score_version(value: object, where: str) -> str:
    """Return ``value`` if it is exactly the supported version, else raise."""
    if not isinstance(value, str) or value != SUPPORTED_SCORE_VERSION:
        raise UnsupportedScoreVersion(
            f"{where}: unsupported score version {value!r} "
            f"(this validator supports only {SUPPORTED_SCORE_VERSION!r})"
        )
    return value
