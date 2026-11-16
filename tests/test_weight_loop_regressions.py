"""Black-box regressions that can also run against the original PR head."""

import inspect
from types import SimpleNamespace

from tests.chain_v2_fake import GENESIS, MASTER
from tests.verified_storage_fake import VerifiedStorageFake
from weight_loop import sdk_submitter


def test_sdk_timeout_after_broadcast_is_not_retried_after_restart(tmp_path, monkeypatch):
    from bittensor.core.extrinsics import weights

    broadcasts = []
    def broadcast(call, **kwargs):
        broadcasts.append(call)
        raise TimeoutError("connection lost after broadcast")

    def query(*, storage_function, **kwargs):
        return SimpleNamespace(value={"Uids": 0, "Keys": MASTER.ss58_address,
                                      "MechanismCountCurrent": 1, "LastUpdate": [0],
                                      "Weights": []}[storage_function])

    class Substrate(VerifiedStorageFake, SimpleNamespace):
        pass

    substrate = Substrate(
        submit_extrinsic=broadcast, query=query, query_map=lambda **kwargs: [],
        get_chain_finalised_head=lambda: "0x4b0", get_block_number=lambda h: 1200,
    )
    st = SimpleNamespace(substrate=substrate, get_block_hash=lambda b: GENESIS)
    wallet = SimpleNamespace(hotkey=MASTER)
    def sdk_call(**kwargs):
        return st.substrate.submit_extrinsic(object())
    monkeypatch.setattr(weights, "set_weights_extrinsic", sdk_call)

    # The original adapter had no durable configuration. Keep this regression
    # runnable on that head so the unsafe second broadcast is directly visible.
    options = {}
    if "state_dir" in inspect.signature(sdk_submitter).parameters:
        options = {"state_dir": str(tmp_path), "genesis_hash": GENESIS}
    for _ in range(2):
        restarted = sdk_submitter(st, wallet, 353, **options)
        try:
            restarted(mechid=0, uids=[1], weights=[1.0], version_key=0, commit_reveal=False)
        except TimeoutError:
            pass
    assert len(broadcasts) == 1
