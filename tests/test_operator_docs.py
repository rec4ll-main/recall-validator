"""Operator guides stay consistent with the code they describe.

Static checks only: settings named in guide env blocks exist in the settings model,
relative links and anchors resolve, and referenced repository files exist.
"""
import re
from pathlib import Path

import pytest

from environment import EnvironmentVariables

ROOT = Path(__file__).resolve().parents[1]
GUIDES = ["README.md", "SETUP.md", "docs/MASTER.md"]


def slug(heading: str) -> str:
    text = heading.strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def anchors(path: Path) -> set[str]:
    return {slug(m.group(1)) for m in re.finditer(r"^#+ (.+)$", path.read_text(), re.M)}


@pytest.mark.parametrize("name", GUIDES)
def test_relative_links_and_anchors_resolve(name):
    path = ROOT / name
    for target in re.findall(r"\]\(([^)\s]+)\)", path.read_text()):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        file_part, _, anchor = target.partition("#")
        dest = (path.parent / file_part).resolve() if file_part else path
        assert dest.exists(), f"{name}: missing link target {target}"
        if anchor:
            assert anchor in anchors(dest), f"{name}: missing anchor {target}"


@pytest.mark.parametrize("name", ["SETUP.md", "docs/MASTER.md"])
def test_guide_settings_exist_in_the_model(name):
    text = (ROOT / name).read_text()
    names = set(re.findall(r"`([A-Z][A-Z0-9_]{3,})(?:=[^`]*)?`", text))
    fields = {f.upper() for f in EnvironmentVariables.model_fields}
    not_settings = {
        # shell variables defined by the guides themselves
        "SERVICE_USER", "SERVICE_HOME", "INSTALL_DIR", "CONFIG_DIR", "ENV_FILE", "STATE_DIR",
        "WALLET_DIR", "WALLET_NAME", "HOTKEY_NAME", "RELEASE_COMMIT", "RELEASE_TREE",
        "UV_VERSION", "PYTHON_VERSION", "UNIT", "API_BASE", "IPFS_API_URL", "UV_NO_MODIFY_PATH",
        "USER",
        # code identifiers and chain storage names cited by the guides
        "MAINNET_MASTER_HOTKEY_SS58", "LastUpdate",
    }
    unknown = {n for n in names - not_settings if n not in fields}
    assert not unknown, f"{name} names settings the model does not define: {sorted(unknown)}"
    if name == "SETUP.md":
        for mode in ("BUNDLE_MODE", "ALLOCATION_MODE"):
            assert mode in text
        # Removed settings appear only in the update notes that tell operators to delete them.
        assert "## Update" in text and "Coming from a release that had `INCENTIVE_V3_MODE`" in text
    else:
        assert "INCENTIVE_V3_MAINNET_31_OPT_IN" not in text


def test_guides_reference_existing_operator_files():
    for rel in ("preflight.py", "registry_bootstrap.py", ".env.example", "config/master.env.example"):
        assert (ROOT / rel).exists(), rel
    setup = (ROOT / "SETUP.md").read_text()
    # Owner rule (2026-11-16): operators run their own security; the guide is uv + .env + pm2/python.
    assert "astral.sh/uv/install.sh" in setup
    assert "uv sync --frozen --no-dev" in setup
    assert "python preflight.py" in setup
    assert "pm2 start main.py" in setup and "--kill-timeout" in setup
    assert "--evaluate-activated-profile" not in setup
    for gone in ("SERVICE_USER", "sudoedit", "systemctl"):
        assert gone not in setup, gone


def test_master_guide_documents_the_registry_bootstrap_contract():
    import registry_bootstrap

    master = (ROOT / "docs/MASTER.md").read_text()
    assert "registry_bootstrap.py --confirm-registry-write" in master
    assert "#### Exclusive registry writer" in master
    assert "while the master is **stopped**" in master
    flags = set(re.findall(r"--[a-z][a-z-]+", master))
    parser_flags = {a for action in registry_bootstrap._parser()._actions for a in action.option_strings}
    assert "--evaluate-activated-profile" not in master
    for flag in ("--confirm-registry-write", "--network", "--netuid",
                 "--genesis-hash", "--master-hotkey", "--max-snapshot-lag-blocks",
                 "--allow-authoritative-empty"):
        assert flag in flags and flag in parser_flags, flag
    assert f"(default {registry_bootstrap.DEFAULT_MAX_SNAPSHOT_LAG_BLOCKS})" in master


def test_two_setup_guides_and_the_secondary_guide_stands_alone():
    """Owner rule (2026-11-09): two validator guides. SETUP.md (secondary) is the setup guide and
    never sends readers to the master guide; docs/MASTER.md has its own link in the README."""
    readme = (ROOT / "README.md").read_text()
    assert "(SETUP.md)" in readme and "(docs/MASTER.md)" in readme
    assert "MASTER.md" not in (ROOT / "SETUP.md").read_text()
