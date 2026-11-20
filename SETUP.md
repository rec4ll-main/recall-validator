# Run a secondary validator

A secondary validator follows Recall's master on **Finney subnet 31**. It verifies the master's signed weight bundles and sets the weights with your own registered hotkey. In configuration this role is named `slave`.

You need a Linux server, Python 3.12 (installed for you by `uv`) and your registered hotkey. No GPU, database, open port or Recall API credentials. For stake delegation without a server, see [Child-hotkey delegation](docs/DELEGATION.md).

How you secure the server and the hotkey is up to you. The validator only needs the **hotkey**, which signs weights; keep your **coldkey** off this server.

## Contents

1. [Before you begin](#1-before-you-begin)
2. [Install](#2-install)
3. [Configure](#3-configure)
4. [Check the configuration](#4-check-the-configuration)
5. [Start](#5-start)
6. [Confirm it is working](#6-confirm-it-is-working)
- [Stop](#stop) · [Update](#update) · [Operating notes](#operating-notes) · [Troubleshooting](#troubleshooting)

## 1. Before you begin

- **A hotkey registered on finney netuid 31**, different from the master's, with a **validator permit** and enough stake for the chain to accept its weights. Register and stake from your wallet machine with btcli; the validator refuses an unregistered hotkey or one without a permit.
- **One process per hotkey.** Run the validator for a hotkey on one machine only.
- **A Linux server** (for example Ubuntu 22.04 or later, x86_64) with `git` and `curl`. No GPU. Minimum 2 vCPU, 4 GB RAM, 20 GB disk; recommended 4 vCPU and 8 GB RAM ([`min_compute.yml`](min_compute.yml)). Outbound internet only.

These public mainnet values are already set in `.env.example`; leave them as shipped:

| Item | Value |
|---|---|
| Network / subnet | `finney`, netuid `31` |
| Master validator, signer of the weight bundles (`MASTER_VALIDATOR_HOTKEY_SS58`) | `5CDZ527X1CiStK1ViGkbC4EjVoouwiGW4ijRKRFfrLpQfftn` |

## 2. Install

Install `uv` (it also installs Python 3.12 for the validator):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source "$HOME/.local/bin/env"
```

Get the [latest release](https://github.com/rec4ll-main/recall-validator/releases/latest) and install its locked dependencies. Replace `<release-tag>` with the tag shown on that page:

```bash
git clone --branch <release-tag> https://github.com/rec4ll-main/recall-validator.git ~/recall-validator
cd ~/recall-validator
uv sync --frozen --no-dev
```

## 3. Configure

This guide assumes your registered hotkey is already on this server in the usual Bittensor place, `~/.bittensor/wallets/<wallet name>/hotkeys/<hotkey name>`. It must load without a password prompt. The validator never needs the coldkey.

Create the settings file and set your wallet and hotkey names (the local names, not addresses):

```bash
cd ~/recall-validator
cp .env.example .env
mkdir -p bundle_state
sed -i -e 's|^WALLET_NAME=.*|WALLET_NAME=<wallet name>|' \
       -e 's|^HOTKEY_NAME=.*|HOTKEY_NAME=<hotkey name>|' \
       -e "s|^BUNDLE_STATE_DIR=.*|BUNDLE_STATE_DIR=$PWD/bundle_state|" .env
grep -E '^(WALLET_NAME|HOTKEY_NAME|BUNDLE_STATE_DIR)=' .env
```

If your wallets are not in `~/.bittensor/wallets`, also set `WALLET_PATH` to that directory. Leave every other line as shipped. `bundle_state` holds the validator's journals and locks: keep it across restarts and updates, and never copy it from another validator.

## 4. Check the configuration

The preflight reads your settings, your hotkey's public address, the chain and the master's registration. It cannot sign or submit anything:

```bash
cd ~/recall-validator
uv run --frozen --no-dev python preflight.py
```

It prints `READ-ONLY PREFLIGHT PASSED` and your hotkey address. Otherwise it names the problem; see [Troubleshooting](#troubleshooting). Run it while the validator is stopped.

## 5. Start

**Starting the validator sets real weights on chain.**

With [pm2](https://pm2.keymetrics.io/) (restarts it on failure and after a reboot). It needs Node.js; on Ubuntu: `sudo apt install -y nodejs npm`.

```bash
sudo npm install -g pm2
cd ~/recall-validator
PYTHONUNBUFFERED=1 pm2 start main.py --name recall-validator --interpreter .venv/bin/python --kill-timeout 120000
pm2 save
pm2 startup     # prints one command; run it to start pm2 at boot
```

Or run it directly (for example inside `tmux` or `screen`):

```bash
cd ~/recall-validator
PYTHONUNBUFFERED=1 .venv/bin/python main.py
```

## 6. Confirm it is working

The examples use pm2. If you run Python directly, read the same lines from its output.

The validator polls every 12 seconds. After the next publication slot (one subnet tempo) you should see verified bundles:

```bash
pm2 logs recall-validator --lines 500 --nostream | grep 'bundle consume:' | tail -n 5
```

`bundle consume: status=verified ...` means the master's bundle passed every check. Then confirmed weights:

```bash
pm2 logs recall-validator --lines 2000 --nostream | grep 'weight loop: ' | grep -v 'status=not_due' | tail -n 5
```

`weight loop: mech=0 status=confirmed reason=finalized WeightsSet <block> ...` each time the chain's rate limit allows a new set. In between it logs `status=not_due reason=<n> blocks`. `confirmed` is logged only after the validator has read back the exact weights it submitted from a finalized block.

| Status | Meaning |
|---|---|
| `stale_last_valid` | Weights were set from an older verified bundle because no fresh one was available. |
| `failclosed_burn` | No usable bundle, or no miner in it still holds its UID, so all weight went to the subnet owner's UID. Normal only on a first start, before the first bundle is verified. |
| `pending_reveal` | Commit-reveal is on: the commit is waiting for the chain to reveal it. Leave it alone. |
| `waiting` | A retry or an earlier attempt is pending; the reason says why. |
| `alert`, `error`, `failclosed` | Needs attention; see [Troubleshooting](#troubleshooting). |

Warnings and alerts (`ERROR | ALERT weight_loop <code>: <detail>`):

```bash
pm2 logs recall-validator --lines 2000 --nostream | grep -E '\| (WARNING|ERROR) \|' | tail -n 20
```

The validator also rewrites `bundle_state/weight_loop_status.json` after every tick: `written_at` should be recent, and `loop.alert_counts` shows the alerts since the last start. Point your monitoring at that file or at the log. `bundle_health.consecutive_missed` above 0 means the latest epoch had no weight bundle to consume (the log says `ALERT bundle_missed`); your validator keeps setting the last valid weights until a new bundle arrives.

## Stop

```bash
pm2 stop recall-validator
```

Make sure it is stopped before you run the same hotkey anywhere else.

## Update

Read the release notes of the [latest release](https://github.com/rec4ll-main/recall-validator/releases/latest) first. Then:

```bash
cd ~/recall-validator
pm2 stop recall-validator
git fetch --tags
git checkout --detach <new-release-tag>
uv sync --frozen --no-dev
uv run --frozen --no-dev python preflight.py
pm2 restart recall-validator
```

Keep `.env` and `bundle_state`. Never `git pull` a moving branch on a running validator. To go back, check out the previous tag, run `uv sync --frozen --no-dev` again, and restart with the same, newest `bundle_state`; never restore an older copy of it.

Coming from a release that had `INCENTIVE_V3_MODE`: delete `INCENTIVE_V3_MODE`, `INCENTIVE_V3_MAINNET_31_OPT_IN`, `INCENTIVE_V3_WEIGHTS_NETUIDS` and `INCENTIVE_V3_APPROVED_MAINNET_GENESIS_HASH` from `.env`, along with the old score-version-10 `ORGANIC_*` and `*_RANK_ENABLED` settings. Incentive v3 (score version 11) is always on. `INCENTIVE_V3_MAINNET_31_OPT_IN=false` no longer keeps a validator from setting weights: keep it stopped instead.

## Operating notes

- **Replaced miners:** when a miner's UID is taken by a new hotkey after the bundle was built, the validator leaves that UID out and shares its weight out among the other miners in proportion to their weights. It logs a warning naming the UIDs and keeps setting weights.
- **Stale or missing bundles:** the validator keeps setting the last verified weights (`stale_last_valid`) for the miners whose hotkeys still match the chain, and raises a `bundle_failclosed` alert. With no usable bundle at all it gives all weight to the subnet owner's UID (`failclosed_burn`), or submits nothing if that UID cannot be proven (`failclosed`).
- **Rejected bundles:** a publication that fails verification is never fetched again. The last verified weights stay in use until a later publication passes.
- **Don't force retries:** never delete `bundle_state`, its journals or its lock to get past an error, and don't run a second process for the same hotkey. After a timeout or a pending reveal, let the validator finish on its own.
- **Payout rules** (whether unearned units burn, the readiness share and who readiness pays) are set by the master and signed into each bundle. A secondary sets none of them.
- **Logs:** a configuration error can print setting values in a traceback. Redact before sharing logs.

## Troubleshooting

Messages appear in the preflight output or in the validator log (`pm2 logs recall-validator`).

### Configuration

| Message | Fix |
|---|---|
| `Configuration invalid. ... values and exception details are suppressed.` (preflight exit 2) | Run the settings check below to see which setting is refused, then use this table. |
| `INCENTIVE_V3_MODE='off' is no longer supported` (or `'shadow'`), or `... is removed and ignored` | Delete the named line; see [Update](#update). |
| `mainnet cannot use a testnet or archive network`, `mainnet is restricted to netuid 31`, `mainnet requires ...` | Restore the shipped values from `.env.example`. |
| `BUNDLE_MODE must be 'chain-v2'` or `... requires ALLOCATION_MODE=v2` | Restore `BUNDLE_MODE=chain-v2` and `ALLOCATION_MODE=v2`. |
| `secondary requires public BUNDLE_GATEWAY_URLS`, or `BUNDLE_GATEWAY_URLS must be public HTTP(S) content gateways` | Use a public IPFS content gateway, for example the shipped `http://ipfs.rec4ll.com:8080`. With `http://` gateways keep `IPFS_ALLOW_PLAINTEXT=true`. |
| `PREFLIGHT FAILED: BUNDLE_STATE_DIR must be an absolute path` or `... does not exist` | Set `BUNDLE_STATE_DIR` to the full path of an existing directory, as in [step 3](#3-configure). |
| `PREFLIGHT FAILED: configured hotkey file could not be loaded` | The file must be at `<WALLET_PATH or ~/.bittensor/wallets>/<WALLET_NAME>/hotkeys/<HOTKEY_NAME>` and readable by the user running the validator. |
| A setting seems to have no effect | Unknown keys are ignored; check the spelling against `.env.example`. |

**Settings check** (prints `settings OK` or the reasons; it reads `.env` and nothing else):

```bash
cd ~/recall-validator
READ_ONLY=1 .venv/bin/python -B - <<'PY'
from pydantic import ValidationError
from environment import EnvironmentVariables
try:
    EnvironmentVariables()
    print("settings OK")
except ValidationError as exc:
    for err in exc.errors():
        print(err["msg"])
PY
```

### Chain and RPC

| Message | Fix |
|---|---|
| `signer or trusted master is not registered` | Check the hotkey names and your registration on finney netuid 31. |
| `signer has no validator permit` | Get enough stake for a validator permit. |
| `slave must use a distinct hotkey from the master` | Use your own hotkey. |
| `genesis hash mismatch` | `NETWORK` points at another chain. Use a finney endpoint (`NETWORK=finney` or your own node's `ws://`/`wss://` URL). |
| `ConnectionError during chain reads`, or while running `finalized head unavailable`, `Error in chain-v2 validator loop` | The RPC endpoint is unreachable or failed. The loop retries every poll; if it persists, use another endpoint or your own node. |

### Bundles

| Message | Fix |
|---|---|
| `bundle consume: status=blocked reason=no pointer at D`, or `commitment block ... outside [...]` | No pointer for this slot, or the public endpoint no longer holds the slot's state (for example after a start late in a slot). The next slot is read normally. If it happens on every start, use an archive node. |
| `bundle fetch failed: all gateways failed`, or `publication unavailable; waiting for next publication` | No gateway serves the bundle from this server. Check `curl -s -o /dev/null -w '%{http_code}\n' http://ipfs.rec4ll.com:8080/ipfs/bafkqaaa`, which should print `200`. |
| `bundle verification failed`, `bundle parse failed`, `publication failed verification` | The bundle was rejected. Nothing to fix locally with the shipped settings; the last verified weights stay in use. |
| `unsupported score version '10' (this validator supports only '11')` | Run the release the master runs. |

### Weights

| Message | Fix |
|---|---|
| `status=not_due reason=... blocks` | Normal: the chain's rate limit has not elapsed yet. |
| `status=waiting reason=SettingWeightsTooFast` | Another process probably set weights with this hotkey. Make sure only one runs. |
| `ALERT weight_loop NoValidatorPermit` or `NotRegistered` | Restore stake or re-register the hotkey from your wallet machine. |
| `ALERT weight_loop submit_rejected: ...` (for example `NotEnoughStakeToSetWeights`) | Fix the named cause. |
| `status=error reason=transient (1/3): ...` | The loop backs off and retries, at most three times per window. |
| `status=waiting reason=... fenced until finalized block ...` | An earlier submission is not proven yet. Wait; don't restart or delete state. |

### Process

| Message | Fix |
|---|---|
| `another validator process holds the state-dir lock` (preflight) or `another v3 setter process holds the state-dir lock` | The validator is already running with this `bundle_state`. Stop it first (`pm2 stop recall-validator`), or find the other process. Don't delete the lock. |
| pm2 shows the process restarting over and over | Read the first error in `pm2 logs recall-validator --lines 200 --nostream`, fix it, then `pm2 restart recall-validator`. |
