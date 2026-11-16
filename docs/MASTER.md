# Run the master validator

This guide is for the authorized operator of Recall's scoring and bundle-publishing infrastructure. Independent validators use the [secondary guide](../SETUP.md).

The master (`BUNDLE_ROLE=master`) does five things:

1. Fetches scoring evidence from the Recall API.
2. Calculates the allocation, signs a bundle, publishes it to IPFS and writes its pointer on chain (`set_commitment`).
3. Verifies the published bundle and runs the weight loop with its own hotkey.
4. After every tick, refreshes the API's miner registry from a finalized metagraph snapshot (`POST /api/validator/metagraph-sync`).
5. Every five minutes after a registry refresh, sends chain incentive, its weight row and the subnet's `MinerBurned` share to `POST /api/validator/chain-economics` for display. A failed update does not interrupt bundle publication or weight submission.

This guide targets Finney subnet 31. Nothing is signed until you start the validator in step 5. The optional registry bootstrap writes API state without starting the validator.

## 0. Before you begin

Read [Before you begin](../SETUP.md#1-before-you-begin) in the secondary guide. The settings, key roles and one-process-per-hotkey rule apply unchanged. The master also needs:

- **The master hotkey**, whose public SS58 equals the mainnet pin `MAINNET_MASTER_HOTKEY_SS58` in `environment.py`. It must be registered on netuid 31 with a validator permit. Startup refuses any other key (`validator_signer_guard.assert_v3_signer`). Changing the master identity needs a new release; see [Changing the master identity](#changing-the-master-identity).
- **A compatible Recall API release** that serves mainnet v3 evidence for netuid 31. You also need its **internal API key**, provisioned privately to this host. That key is privileged. Never give it to secondaries or miners, and never send it over HTTP.
- **A Kubo RPC endpoint with publication rights** (`BUNDLE_IPFS_API_URL`). A read-only gateway is not enough. Keep the Kubo management API inside its trusted network boundary.
- **The release you will run:** the tag of the [latest release](https://github.com/rec4ll-main/recall-validator/releases/latest), installed as in the secondary guide, and an API release that serves the same evidence contract and score version.

### Registry bootstrap

On a fresh API database, the miner registry is empty. Miners cannot complete `POST /api/miner/register` or heartbeats until the registry marks their hotkey active (`isRegisteredMiner` in recall-web `handlers/miners.go`). Two things can write that registry:

1. **The master's post-tick refresh** (`main._refresh_master_registry`). It runs only after the tick's bundle step (`master_publish` → `set_commitment`, or consumption) and the weight loop. **Starting the master briefly to populate the registry is activation, not a bootstrap.** Do not do it.
2. **`registry_bootstrap.py`, a one-shot preparation command.** It **writes API state**; it is not a dry run. With `--confirm-registry-write` it reads one complete finalized UID/hotkey snapshot and sends at most one internal-key-authenticated metagraph-sync update (the same body the master sends), then exits. It never loads a wallet, signs, submits a transaction, imports the validator runtime or touches the state directory.

Also note:

- **Setting only `WEIGHT_LOOP_ENABLED=false` does not make the master read-only.** Bundle and pointer publication still run. Consumption then falls back to its legacy once-per-slot `set_weights` dispatch (`bundle_consume.py`).
- There is no dry-run mode of the validator. Incentive v3 (score version 11) is the only scoring path; `INCENTIVE_V3_MODE=off|shadow` is refused at startup (see [Update](../SETUP.md#update)).

#### Exclusive registry writer

Only one registry writer may run against an API at a time.

- Run the bootstrap only while the master is **stopped** and no other master process on any machine, container or test setup is configured against this API.
- One operator runs it once. Don't run it from two places, from a timer or in a loop. If it is refused, read the reason, fix the cause and rerun it deliberately.
- Unless the API release rejects out-of-order syncs, an older snapshot sent after a newer one would briefly reactivate miners that deregistered in between. The bootstrap treats any outcome other than an applied sync as exit 4. Either way, it refuses a snapshot more than `--max-snapshot-lag-blocks` (default 3) behind the finalized head, and it must never overlap a running master.
- After activation (step 5), the master's refresh is the only writer. Don't run the bootstrap while a master runs.

#### Run the bootstrap

Run it after the checks in step 4 pass and before activation (step 5). Every value must equal `.env`: the command refuses a mismatch. On the Finney genesis the master must be the source-pinned `MAINNET_MASTER_HOTKEY_SS58`.

```bash
cd ~/recall-validator
.venv/bin/python registry_bootstrap.py --confirm-registry-write --network '<NETWORK from .env>' --netuid 31 \
  --genesis-hash '<ALLOCATION_EXPECTED_GENESIS_HASH from .env>' --master-hotkey '<MASTER_VALIDATOR_HOTKEY_SS58 from .env>'
```

Success is exit code 0 and one JSON line with `"result": "applied"`, `"api_status": "ok"`, `finalized_block`, `uid_count`, and `miner_count` = `api_active_miners` (registered UIDs minus the master). The output never contains the key, the API URL or hotkeys.

Re-run the `miners/active` count from step 4; it should equal `miner_count`. Other exit codes mean:

- exit 2: usage or configuration refused. Nothing was read from the chain and nothing was sent.
- exit 3: the finalized snapshot was refused (wrong genesis or netuid, master not registered, incomplete or mismatched mapping, stale snapshot, chain read error, or an empty mapping). Nothing was sent.
- exit 4: a request was attempted and the API outcome was not an applied sync (a non-200 status, a redirect, a malformed body, a no-op or rejected status, or a different miner count), or the connection failed or timed out, in which case the update may still have been applied. A 2xx status alone is not success. Check the `miners/active` count before any rerun.

An empty mapping is refused unless `--allow-authoritative-empty` is passed. That flag would deactivate every API miner. Don't use it for a launch.

There is no undo command for a registry sync. The next accepted sync replaces the active set.

## 1–3. Install and configure

Install as in secondary steps [2](../SETUP.md#2-install) and [3](../SETUP.md#3-configure), with the master hotkey, but start from the master template:

```bash
cd ~/recall-validator
cp config/master.env.example .env
mkdir -p bundle_state
```

Set `WALLET_NAME`, `HOTKEY_NAME`, `WALLET_PATH` (if needed) and `BUNDLE_STATE_DIR` (full path) as in the secondary guide, plus:

| Setting | Value |
|---|---|
| `RECALL_API_URL` | The API base including `/api`, **HTTPS only**. Startup refuses a master unless it is set and passes the destination check (`api_transport.validate_api_url`): `https://` for every remote host, no userinfo, query or fragment. Plaintext `http://` is accepted only for a literal loopback IP, for local tests. The registry refresh re-checks it before each authenticated POST and never follows redirects. |
| `INTERNAL_API_KEY` | The privately provisioned key. Placeholder-looking values are refused at startup. Never give it to secondaries or miners. |
| `ALLOCATION_ALLOW_PLAINTEXT` | Keep `false`. |
| `BUNDLE_IPFS_API_URL` | The Kubo RPC base with publication rights. Required for `BUNDLE_ROLE=master`. |

Keep `BUNDLE_ROLE=master`, the pinned master address and the rest of the template as shipped. The template sets `INCENTIVE_V3_READINESS_MODE=shares`, `INCENTIVE_V3_BURN_UNEARNED=false` and `INCENTIVE_V3_READINESS_SPLIT=0.40`; keep them explicit, because the parser defaults `INCENTIVE_V3_BURN_UNEARNED` to `true` if omitted. `INCENTIVE_V3_SLOT_WINDOW_HOURS=48` must match the API's 172800-second slot window. An existing master keeps its existing `bundle_state`; never reuse a secondary's.

## 4. Check the configuration, API and IPFS

```bash
cd ~/recall-validator
uv run --frozen --no-dev python preflight.py
```

It should print `READ-ONLY PREFLIGHT PASSED`, with `"role": "master"` and `signer_hotkey` equal to `master_hotkey`. The preflight does not call the API. Then check the API and the internal key (sent only in an HTTPS header, read from `.env` through a pipe):

```bash
API_BASE=$(sed -n 's/^RECALL_API_URL=//p' .env)
curl --fail --silent --show-error --max-time 15 "$API_BASE/health"
sed -n 's/^INTERNAL_API_KEY=/X-Internal-API-Key: /p' .env \
  | curl --proto '=https' --fail --silent --show-error --max-time 15 -H @- "$API_BASE/miners/active" \
  | python3 -c 'import json,sys; print("active miners in API registry:", len(json.load(sys.stdin)["miners"]))'
```

On a fresh API database the count is `0`; see [Registry bootstrap](#registry-bootstrap). A `401` means the API did not accept the key. Then the publication endpoint (Kubo requires POST):

```bash
curl --fail --silent --show-error --max-time 15 -X POST "$(sed -n 's/^BUNDLE_IPFS_API_URL=//p' .env)/api/v0/version"
```

It should return JSON with `"Version"`. That checks reachability, not publication rights.

## 5. Start

Starting the master begins bundle publication and chain submissions. Before starting, check that:

- Step 4 passed, and the API release serves the evidence contract and score version of this validator release.
- No other process on any machine uses the master hotkey, including earlier masters and containers.
- An existing master's `bundle_state` is retained.
- Miners intended for launch are registered on chain. Unless the [registry bootstrap](#registry-bootstrap) was applied, the registry stays empty until the first post-activation refresh.
- You have reviewed the [allocation settings](#payout-rules-the-allocation-settings-encode) and the [fallback behavior](../SETUP.md#operating-notes).

Start it as in secondary [step 5](../SETUP.md#5-start) (pm2 or directly). Startup logs `score_version 11 active on netuid 31 (genesis and signer verified)`, then `Validator running chain-v2 role=master; ...`. Each tick logs `bundle master publish: status=... cid=... reason=...` before the publication cutoff, or `bundle consume: status=...` after it, then `weight loop: ...`. A refused snapshot logs `chain-v2 registry refresh skipped: ...`; a rejected POST logs `master metagraph registry sync not published (transport or auth rejected)`. Neither disturbs the tick; check the `miners/active` count.

Verify:

1. Fresh v3 evidence is fetched.
2. A signed score-11 bundle is published, and its pointer is finalized on chain.
3. The intended validators verify and consume it.
4. Weights are applied at a finalized block for the intended hotkey and mechanism.
5. The `miners/active` count from step 4 now reflects the finalized metagraph minus the master.

An included commit or a changed `LastUpdate` alone is not proof of application. Stopping does not undo submitted transactions or registry rows; keep `bundle_state`.

## 10. Operate, stop and update

Use the secondary guide's [operating notes](../SETUP.md#operating-notes), [stop](../SETUP.md#stop) and [update](../SETUP.md#update) procedures. In addition:

- Monitor API evidence errors, `401`/`403` from the API, IPFS publication failures, and the `chain-v2 registry refresh skipped`, `master metagraph registry sync not published` and `chain economics ...` warnings.
- Coordinate API and master releases: evidence contract, 48-hour slot window, score version. A changed API URL needs the same checks as step 4.
- Never copy the master's state or environment file to another host while this master may still run.

## Changing the master identity

This source pins `5CDZ527X1CiStK1ViGkbC4EjVoouwiGW4ijRKRFfrLpQfftn` as the mainnet master (`environment.MAINNET_MASTER_HOTKEY_SS58`, also in both example files). A new master hotkey requires all of these before activation:

- a new release that updates the pin, the tests and both example files;
- every secondary updated to that release;
- the new key's registration and validator permit verified.

Editing `MASTER_VALIDATOR_HOTKEY_SS58` alone cannot bypass the pin.

The Recall API's signing key (`5ECDAred992gLh55nJ7PxQDtihHXy5qZZXuCkqLVJdmDsrYw` on mainnet) is a separate identity: miners trust it for probes and heartbeats, and it does not change when the master changes. The secondary's mainnet values are in [SETUP.md](../SETUP.md#1-before-you-begin).

## Payout rules the allocation settings encode

These settings affect the master's allocation. Secondaries verify and submit its
signed vector; they do not select a different payout policy.

| Setting | Shipped master template | Effect |
|---|---|---|
| `INCENTIVE_V3_READINESS_SPLIT` | `0.40` | Allocate 40% of each tier budget to readiness and 60% to delivered service. |
| `INCENTIVE_V3_READINESS_MODE` | `shares` | Pay every eligible miner a readiness share based on quality. `slots` instead pays the four slot holders per tier. |
| `INCENTIVE_V3_BURN_UNEARNED` | `false` | Redistribute unearned units among miners that earned units, subject to the bundle's per-miner cap. |

### Readiness

For a tier with readiness pool `R`, each miner has quality `q`: its lowest decayed
check pass rate, multiplied by the latency/capacity factor and its readiness-stage
weight. Quality is at most 1.

With `shares`, an eligible miner initially earns:

```text
R × q / max(sum of eligible miners' quality, 4)
```

With `slots`, each of the four holders initially earns `R / 4 × q`. The slot table,
stages and challenger streaks advance in both modes. Changing the mode takes
effect at the next epoch without resetting that history.

The one-slot ceiling applies to this initial readiness calculation. It is **not
a ceiling on final rewards**: redistribution can increase a miner's allocation.
Readiness can earn units even when there is no customer demand.

Shares mode rewards every eligible identity after coldkey deduplication. The
allocator keeps one miner per coldkey, choosing the latest registration and then
the lowest UID on a tie. It does not prove that different coldkeys have different
operators.

### Delivered service and redistribution

Service allocation depends on demand and each eligible miner's credited work.
Missing per-miner credit earns no service units. The storage reward pool is
disabled in this release; its unused units follow the same burn or redistribution
policy as other unearned units.

With `INCENTIVE_V3_BURN_UNEARNED=true`, unearned units go to the owner/burn UID.
With the template's `false`, the allocator redistributes them in proportion to
units already earned. If a miner reaches the bundle's row cap, the excess is
redistributed among the remaining earning miners. Any remainder that cannot fit
under those caps still goes to the owner/burn UID. If no miner earns units, all
100 units go there.

The initial per-miner cap uses the number of eligible miners. Redistribution's
bundle row cap uses the number of paid miners; both include encoding safeguards.
Do not treat the initial readiness ceiling or cap as the final reward share.
The implementation is in [`allocation_v3.py`](../allocation_v3.py).

Each saved epoch record includes its `readiness_mode`. To compare both modes
without submitting anything, run `python3 -m v3_whatif <epoch-record-path>` from
the checkout with its installed environment. Older records without that field
used slots mode.
