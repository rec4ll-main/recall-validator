# Validator development

The validator is a Python application with `main.py` as its entry point. Modules
live at the repository root. This guide follows the runtime flow so you can find
the code responsible for a behavior before changing it.

## Run the tests

Use Python 3.12 and the locked dependencies. From the repository root:

```bash
uv sync --frozen
uv run --frozen python -m compileall -q .
uv run --frozen pytest -q -p no:cacheprovider
```

The suite uses local fixtures and fake chain clients. Do not start `main.py` to
test a change: a configured validator can submit real transactions.

Configuration examples and operator guides have tests in
[`test_operator_examples.py`](../tests/test_operator_examples.py) and
[`test_operator_docs.py`](../tests/test_operator_docs.py). Changes to public
settings, commands or file locations should keep these guides consistent.

## Startup and chain identity

| Modules | Responsibility |
|---|---|
| `main.py`, `environment.py` | Load settings, validate the deployment and run the polling loop. |
| `preflight.py`, `registry_bootstrap.py` | Check an installation without signing; separately, populate the API registry before the master starts. The bootstrap writes API state. |
| `validator_signer_guard.py`, `setter_guard.py` | Check the registered signer and validator permit; prevent two local processes from using one state directory. |
| `bundle_schedule.py`, `bundle_chain.py` | Read finalized chain state, determine the publication window and decode commitment pointers. |
| `allocation_snapshot.py`, `allocation_types.py`, `metagraph_arrays.py` | Build and validate a consistent UID, hotkey and coldkey snapshot. |
| `owner_burn.py` | Resolve the subnet owner UID from chain state for fallback allocations. |

## Scoring and allocation

Only the master calculates allocations. Secondaries verify its signed result.

| Modules | Responsibility |
|---|---|
| `v3_metrics_client.py` | Fetch and validate evidence from `POST /api/validator/v3/metrics`. |
| `v3_acceptance.py`, `v3_slots.py` | Decide tier eligibility and advance readiness stages and slot holders. |
| `v3_demand.py` | Calculate the demand ramp for each service pool. |
| `allocation_v3.py` | Allocate readiness and service units, apply caps, and account for burned or redistributed units. |
| `allocation.py`, `allocation_encoding.py` | Normalize weights, encode them with the installed SDK, and check the decoded result. |
| `v3_runtime.py`, `v3_state.py` | Build an epoch allocation and persist its state and records. |
| `v3_eligibility.py` | Select the allocator's state scope and bind it to stored artifacts. Submission journals remain shared across scopes. |
| `v3_params.py`, `score_version.py` | Define incentive parameters and the supported score-version contract. |
| `v3_whatif.py` | Compare shares and slots readiness allocation from a saved epoch record, offline. |

## Bundle publication and verification

The current signed schema is `recall-allocation-bundle-v2`. It carries
score-version-11 allocations.

| Modules | Responsibility |
|---|---|
| `bundle_envelope.py`, `bundle_v2.py`, `bundle_v2_types.py` | Build, sign, parse and validate allocation bundles. |
| `bundle_weights.py` | Check weight rows on both publication and consumption. |
| `bundle_ipfs.py` | Publish raw IPFS blocks and fetch them from content gateways. |
| `bundle_publish.py`, `bundle_commit_guard.py` | Persist and publish a bundle, verify its availability, then submit its chain pointer. |
| `bundle_consume.py` | Read the chain pointer, fetch the bundle and verify it for the weight loop. |
| `bundle_health.py` | Per epoch, record whether the bundle was published and consumed; log `ALERT bundle_missed` and count missed epochs in `bundle_health.json`. |
| `bundle_store.py`, `bundle_runtime.py` | Store immutable artifacts and coordinate chain and journal operations. |

`weight_bundle.py` contains the earlier `recall-weight-bundle-v1` verifier. It is
not the current allocation-bundle format or the weight-setting loop.

## Weight submission and recovery

| Modules | Responsibility |
|---|---|
| `weight_loop.py` | Select usable verified weights and submit them under the chain's rate limit. |
| `weight_loop_dispatch.py`, `bundle_dispatch.py` | Track submissions, distinguish an unsent request from an uncertain outcome, and reconcile finalized results. |
| `bundle_journal.py`, `bundle_journal_types.py` | Expose the durable operation journal and its states. |
| `bundle_journal_open.py`, `bundle_journal_query.py`, `bundle_journal_store.py` | Open the SQLite journal and read or persist its records. |

The state directory contains both allocation history and submission recovery
records. Never delete it to force a retry or use an older copy after the signer
has submitted newer transactions.

## API updates

| Modules | Responsibility |
|---|---|
| `chain_v2_registry.py` | Refresh the API's miner registry from a finalized metagraph snapshot. |
| `chain_economics.py` | Send chain incentive, the master's weight row and `MinerBurned` to the API for display. |
| `validator_http.py`, `api_transport.py` | Apply the validator's HTTP identity and transport rules to API requests. |

These API updates run on the master. A secondary needs only the chain and public
IPFS content gateways.
