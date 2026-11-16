# Recall Validator

Recall is Bittensor subnet **31** on Finney. This repository runs the validators
that submit its miner weights to the chain.

Most operators run a **secondary validator**. It downloads a signed weight bundle
from Recall's master validator, checks the signature and chain identities, and
submits the verified weights with its own hotkey. It does not calculate miner
scores independently.

## Get started

| Goal | Guide |
|---|---|
| Run a validator with your own registered hotkey and validator permit | [Secondary validator setup](SETUP.md) |
| Delegate stake without running a validator | [Child-hotkey delegation](docs/DELEGATION.md) |
| Operate the authorized scoring and bundle publisher | [Master validator setup](docs/MASTER.md) |
| Understand or contribute to the code | [Module map and development](docs/MODULE_MAP.md) |
| Report a vulnerability | [Security policy](SECURITY.md) |

A secondary needs a Linux host with **2 vCPU, 4 GB RAM and 20 GB disk**
at minimum. The recommended size is 4 vCPU and 8 GB RAM. It needs outbound access
to Bittensor RPC and an IPFS content gateway, but no inbound ports, GPU, database
or Recall API credentials. See [Before you begin](SETUP.md#1-before-you-begin).

Install a [published release](https://github.com/rec4ll-main/recall-validator/releases/latest)
and use the documentation from that tag. Documentation on `main` follows the
current source and may describe changes not yet included in a release.

## How weights reach the chain

1. The master fetches scoring evidence from the Recall API and calculates miner weights.
2. It signs a bundle, publishes it to IPFS, and records the bundle's location on Bittensor.
3. Each secondary checks the bundle's content hash, master signature, network,
   score version and miner identities.
4. The secondary submits the verified weights when the chain's rate limit permits it.

The current scoring model is incentive v3, **score version 11**. Configuration
still calls the delivery protocol `BUNDLE_MODE=chain-v2`, with
`ALLOCATION_MODE=v2`; these describe the bundle format, not the scoring model.
The secondary role is named `BUNDLE_ROLE=slave` in configuration.

**Starting the service starts signing.** There is no dry-run flag.
`ENVIRONMENT` is a log label; the network is selected by `NETWORK` and `NETUID`.
Run one process per signing hotkey and preserve its state directory when updating
or moving it. Keep the coldkey on your wallet-management device.

## Monitor operation

Look for a verified bundle followed by a confirmed weight submission. The
[verification steps](SETUP.md#6-confirm-it-is-working) show the log messages
and the fields in `weight_loop_status.json` that establish this. A running process
or a recent chain `LastUpdate` alone is insufficient.

By default, the validator continues using its last verified weights during a
publication outage. If those weights are no longer usable, it submits owner
fallback weights; if it cannot establish the owner UID from the chain, it submits
nothing. Read the [operating notes](SETUP.md#operating-notes) before starting.

## Recall ecosystem

- [Recall](https://rec4ll.com): the service and operator console.
- [Miner](https://github.com/rec4ll-main/recall-miner): storage and inference services.
- [API documentation](https://rec4ll.com/docs): client integration and request examples.
