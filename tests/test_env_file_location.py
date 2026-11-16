"""Settings load the repository .env, never the parent directory's."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

_PROBE = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
sys.path.append(sys.argv[2])
import environment
assert environment.__file__.startswith(sys.argv[1]), environment.__file__
import main
try:
    env = environment.EnvironmentVariables()
    main.validate_deployment_secrets(env)
except Exception as exc:
    print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
    sys.exit(0)
print(json.dumps({
    "ok": True,
    "env_file": str(environment.ENV_FILE_PATH),
    "network": env.network,
    "netuid": env.netuid,
    "bundle_mode": env.bundle_mode,
    "bundle_role": env.bundle_role,
    "wallet_name": env.wallet_name,
    "hotkey_name": env.hotkey_name,
    "recall_api_url": env.recall_api_url,
}))
"""

_REPO_ENV = """\
NETWORK=finney
NETUID=31
WALLET_NAME=repo-wallet
HOTKEY_NAME=repo-hotkey
RECALL_API_URL=https://rec4ll.com/api
INTERNAL_API_KEY=repo-env-real-secret-value
BUNDLE_MODE=chain-v2
BUNDLE_ROLE=master
ALLOCATION_MODE=v2
ALLOCATION_EXPECTED_GENESIS_HASH=0x2f0555cc76fc2840a25a6ea3b9637146806f1f44b090c175ffde2a7e5ab36c03
MASTER_VALIDATOR_HOTKEY_SS58=5CDZ527X1CiStK1ViGkbC4EjVoouwiGW4ijRKRFfrLpQfftn
BUNDLE_STATE_DIR=./bundle_state
BUNDLE_IPFS_API_URL=http://127.0.0.1:5001
"""

_PARENT_ENV = """\
NETWORK=parent-sentinel-network
NETUID=999
WALLET_NAME=parent-sentinel-wallet
HOTKEY_NAME=parent-sentinel-hotkey
BUNDLE_MODE=chain-v2
BUNDLE_ROLE=slave
ALLOCATION_MODE=v2
ALLOCATION_EXPECTED_GENESIS_HASH=0xparent
MASTER_VALIDATOR_HOTKEY_SS58=5ParentSentinel
BUNDLE_IPFS_API_URL=http://127.0.0.1:5001
"""


def _checkout(tmp_path: Path) -> Path:
    parent = tmp_path / "parent"
    checkout = parent / "recall-validator"
    checkout.mkdir(parents=True)
    shutil.copy2(REPO / "environment.py", checkout / "environment.py")
    return checkout


def _run(checkout: Path, cwd: Path, extra_env: dict | None = None) -> dict:
    # Isolated process environment: nothing the settings read is inherited.
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(cwd),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE, str(checkout), str(REPO)],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_env_file_is_beside_environment_module():
    import environment

    assert environment.ENV_FILE_PATH == REPO / ".env"
    assert environment.EnvironmentVariables.model_config["env_file"] == REPO / ".env"


def test_repository_env_loads_and_parent_env_is_ignored(tmp_path):
    checkout = _checkout(tmp_path)
    (checkout / ".env").write_text(_REPO_ENV)
    (checkout.parent / ".env").write_text(_PARENT_ENV)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    out = _run(checkout, cwd=elsewhere)

    assert out["ok"], out
    assert out["env_file"] == str((checkout / ".env").resolve())
    assert out["network"] == "finney"
    assert out["netuid"] == 31
    assert out["bundle_mode"] == "chain-v2"
    assert out["bundle_role"] == "master"
    assert out["wallet_name"] == "repo-wallet"
    assert out["hotkey_name"] == "repo-hotkey"


def test_parent_env_alone_is_not_used(tmp_path):
    checkout = _checkout(tmp_path)
    (checkout.parent / ".env").write_text(_PARENT_ENV)

    out = _run(checkout, cwd=checkout.parent)

    assert not out["ok"]
    assert "BUNDLE_MODE=chain-v2 is the only supported mode" in out["error"]


def test_process_environment_overrides_repository_env(tmp_path):
    checkout = _checkout(tmp_path)
    (checkout / ".env").write_text(_REPO_ENV)

    out = _run(
        checkout,
        cwd=checkout,
        extra_env={"WALLET_NAME": "process-wallet", "NETWORK": "mainnet"},
    )

    assert out["ok"], out
    assert out["wallet_name"] == "process-wallet"
    assert out["network"] == "mainnet"
    assert out["hotkey_name"] == "repo-hotkey"


@pytest.mark.parametrize(
    "drop, message",
    [
        ("BUNDLE_MODE", "BUNDLE_MODE=chain-v2 is the only supported mode"),
        ("MASTER_VALIDATOR_HOTKEY_SS58", "requires MASTER_VALIDATOR_HOTKEY_SS58"),
        ("ALLOCATION_EXPECTED_GENESIS_HASH", "requires ALLOCATION_EXPECTED_GENESIS_HASH"),
        ("NETUID", "NETUID must be explicitly set"),
        ("NETWORK", "NETWORK must be explicitly set"),
        ("INTERNAL_API_KEY", "INTERNAL_API_KEY must be a real secret"),
    ],
)
def test_missing_required_chain_v2_config_still_fails(tmp_path, drop, message):
    checkout = _checkout(tmp_path)
    lines = [l for l in _REPO_ENV.splitlines() if not l.startswith(drop + "=")]
    (checkout / ".env").write_text("\n".join(lines) + "\n")

    out = _run(checkout, cwd=checkout)

    assert not out["ok"]
    assert message in out["error"], out["error"]
