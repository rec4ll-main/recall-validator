"""Explicit successful raw envelopes for existing synthetic query-based fixtures.

This fixture uses JSON bytes, not SCALE. Dedicated compatibility tests exercise
real SCALE vector decoding separately. Production never uses this adapter.
"""
import json
from types import SimpleNamespace


class VerifiedStorageFake:
    def create_storage_key(self, *, pallet, storage_function, params, block_hash):
        assert pallet == "SubtensorModule"
        chain = self
        encoded_key = "0x" + json.dumps([storage_function, params]).encode().hex()

        def decode(data):
            if data is None:
                value = [] if storage_function == "LastUpdate" else None
            else:
                value = json.loads(bytes(data.data).decode())
            return SimpleNamespace(value=value)
        return SimpleNamespace(
            to_hex=lambda: encoded_key, decode_scale_value=decode,
            metadata_storage_function=SimpleNamespace(value={"modifier": "Default"}),
        )

    def rpc_request(self, method, params):
        assert method == "state_getStorageAt"
        name, query_params = json.loads(bytes.fromhex(params[0][2:]).decode())
        value = self.query(module="SubtensorModule", storage_function=name,
                           params=query_params, block_hash=params[1]).value
        return {"result": "0x" + json.dumps(value).encode().hex()}
