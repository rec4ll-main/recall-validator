"""Authoritative owner-hotkey resolution, independent of cached metagraph rows."""

from types import SimpleNamespace

import pytest

from bundle_weights import signed_burn_uid
from owner_burn import OwnerReadError, resolve_owner_burn_uid


OWNER = "designated-owner-hotkey"
OTHER = "another-hotkey-with-same-coldkey"
HEAD = "0x" + "ab" * 32


class Substrate:
    def __init__(self, *, owner=OWNER, uid=7, key=OWNER, fail=None):
        self.values = {"SubnetOwnerHotkey": owner, "Uids": uid, "Keys": key}
        self.fail = fail
        self.queries = []
        self.finalized_reads = 0

    def get_chain_finalised_head(self):
        self.finalized_reads += 1
        if self.fail == "head":
            raise OSError("head unavailable")
        return HEAD

    def get_block_number(self, block_hash):
        assert block_hash == HEAD
        return 1200

    def query(self, *, module, storage_function, params, block_hash):
        assert module == "SubtensorModule"
        self.queries.append((storage_function, params, block_hash))
        if self.fail == storage_function:
            raise OSError("storage unavailable")
        return SimpleNamespace(value=self.values[storage_function])


def fixture(**kwargs):
    substrate = Substrate(**kwargs)
    # SDK 10.5.0 SubnetInfo exposes the owner coldkey, not owner_hotkey.
    subtensor = SimpleNamespace(
        substrate=substrate,
        get_subnet_info=lambda netuid: SimpleNamespace(owner_ss58="shared-coldkey"),
    )
    graph = SimpleNamespace(hotkeys=[OTHER, OWNER],
                            coldkeys=["shared-coldkey", "shared-coldkey"])
    return subtensor, graph


def test_two_hotkeys_sharing_owner_coldkey_resolve_only_designated_owner():
    st, graph = fixture()
    assert resolve_owner_burn_uid(st, graph, 353) == (7, OWNER)
    assert st.substrate.finalized_reads == 1
    assert st.substrate.queries == [
        ("SubnetOwnerHotkey", [353], HEAD),
        ("Uids", [353, OWNER], HEAD),
        ("Keys", [353, 7], HEAD),
    ]


def test_stale_metagraph_uid_does_not_override_finalized_registration():
    st, graph = fixture(uid=9)
    graph.hotkeys = [OWNER, OTHER]
    assert resolve_owner_burn_uid(st, graph, 353) == (9, OWNER)


def test_stale_uid_round_trip_mismatch_fails_closed():
    st, graph = fixture(uid=0, key=OTHER)
    graph.hotkeys = [OWNER, OTHER]
    assert resolve_owner_burn_uid(st, graph, 353) == (None, None)


def test_uid_zero_is_valid_when_authoritatively_proven():
    st, graph = fixture(uid=0)
    assert resolve_owner_burn_uid(st, graph, 353) == (0, OWNER)


@pytest.mark.parametrize("overrides", [
    {"owner": None}, {"owner": ""}, {"owner": 0},
    {"uid": None}, {"uid": True}, {"uid": -1}, {"uid": 65536}, {"uid": "7"},
    {"key": None}, {"key": ""},
])
def test_missing_or_malformed_chain_identity_fails_closed(overrides):
    st, graph = fixture(**overrides)
    assert resolve_owner_burn_uid(st, graph, 353) == (None, None)


@pytest.mark.parametrize("fail", ["head", "SubnetOwnerHotkey", "Uids", "Keys"])
def test_unavailable_chain_proof_never_falls_back_to_owner_coldkey(fail):
    st, graph = fixture(fail=fail)
    assert resolve_owner_burn_uid(st, graph, 353) == (None, None)


@pytest.mark.parametrize("fail", ["head", "SubnetOwnerHotkey", "Uids", "Keys"])
def test_strict_read_failure_raises_instead_of_reporting_no_owner(fail):
    st, graph = fixture(fail=fail)
    with pytest.raises(OwnerReadError):
        resolve_owner_burn_uid(st, graph, 353, strict=True)
    burn_row = SimpleNamespace(uids=[7], hotkeys=[OWNER])
    with pytest.raises(OwnerReadError):
        signed_burn_uid(st, graph, 353, burn_row, strict=True)
    # Default (master publish and other callers): still fails closed as no owner.
    assert signed_burn_uid(st, graph, 353, burn_row) == (None, None)


@pytest.mark.parametrize("overrides", [{"owner": None}, {"uid": None}, {"key": OTHER}])
def test_strict_proven_absent_owner_is_still_no_owner(overrides):
    st, graph = fixture(**overrides)
    assert resolve_owner_burn_uid(st, graph, 353, strict=True) == (None, None)


def test_no_metagraph_or_subnet_info_access_is_needed():
    st, _ = fixture()
    def unsupported(*args):
        raise AssertionError("legacy owner inference must not run")
    st.get_subnet_info = unsupported
    assert resolve_owner_burn_uid(st, None, 353) == (7, OWNER)
