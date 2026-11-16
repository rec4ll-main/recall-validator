"""Fakes for registry_bootstrap tests: a finalized-chain model and a loopback API.

Standard library only, so an isolated subprocess can use them without pytest.
``FakeApi`` follows the recall-web ``ValidatorMetagraphSync`` contract
(``handlers/validator.go`` + ``decideMetagraphSync`` in ``metagraph_sync.go``):
internal-key auth, hotkey de-duplication, the netuid target guard, qualified
authoritative-empty semantics, the stale-empty guard, and the
``status``/``active_miners``/``marked_offline`` response.
"""

from __future__ import annotations

import http.server
import json
import threading
from types import SimpleNamespace

GENESIS = "0x" + "ab" * 32
MASTER = "master-hotkey"
HOTKEYS = ["owner-hotkey", MASTER, "miner-a", "miner-b", "miner-c"]
SYNTH_KEY = "synthetic-internal-key-91c2d7"


def block_hash(n: int) -> str:
    return "0x" + f"{n:064x}"


def block_of(h: str) -> int:
    return int(h[2:], 16)


class FakeChain:
    def __init__(self, hotkeys=HOTKEYS, *, head=10_000, genesis=GENESIS, netuid=353):
        self.hotkeys = list(hotkeys)
        self.coldkeys = [f"cold-{i}" for i in range(len(self.hotkeys))]
        self.regs = list(range(1, len(self.hotkeys) + 1))
        self.head = head
        self.genesis = genesis
        self.netuid = netuid
        self.queries = []           # (storage_function, block_hash)
        self.submissions = []       # any submission entry point reached
        self.head_reads = 0
        self.unfinalized_head_reads = 0
        self.advance_per_head_read = 0
        self.subnetwork_n = None    # override SubnetworkN
        self.keys_override = {}     # uid -> hotkey at the snapshot hash
        self.uids_override = {}
        self.sync_block_offset = 0
        self.metagraph_netuid = None
        self.failing = None         # exception raised by every storage query
        self.metagraph_inits = []
        self.subtensor = FakeSubtensor(self)

    def metagraph_factory(self, *, netuid, network, lite=True, sync=True, subtensor=None, mechid=0):
        self.metagraph_inits.append({"netuid": netuid, "lite": lite, "sync": sync})
        assert sync is False, "the bootstrap must not sync at construction (unpinned read)"
        return FakeMetagraph(self, netuid if self.metagraph_netuid is None else self.metagraph_netuid)


class FakeSubstrate:
    def __init__(self, chain):
        self._c = chain

    def get_chain_finalised_head(self):
        c = self._c
        c.head_reads += 1
        h = block_hash(c.head)
        c.head += c.advance_per_head_read
        return h

    def get_chain_head(self):
        self._c.unfinalized_head_reads += 1
        return block_hash(self._c.head + 5)

    def get_block_number(self, h):
        return block_of(h)

    def query(self, module, storage_function, params, block_hash=None):
        c = self._c
        assert module == "SubtensorModule"
        c.queries.append((storage_function, block_hash))
        if c.failing is not None:
            raise c.failing
        if storage_function == "SubnetworkN":
            value = len(c.hotkeys) if c.subnetwork_n is None else c.subnetwork_n
        elif storage_function == "Keys":
            uid = params[1]
            value = c.keys_override.get(uid, c.hotkeys[uid] if uid < len(c.hotkeys) else None)
        elif storage_function == "Uids":
            hk = params[1]
            value = c.uids_override.get(hk, c.hotkeys.index(hk) if hk in c.hotkeys else None)
        else:
            raise AssertionError(f"unexpected query {storage_function}")
        return SimpleNamespace(value=value)

    def submit_extrinsic(self, *a, **k):
        self._c.submissions.append("submit_extrinsic")

    def create_signed_extrinsic(self, *a, **k):
        self._c.submissions.append("create_signed_extrinsic")


class FakeSubtensor:
    def __init__(self, chain):
        self._c = chain
        self.substrate = FakeSubstrate(chain)

    def get_block_hash(self, block):
        if block == 0:
            return self._c.genesis
        return block_hash(block) if block <= self._c.head else None

    def max_weight_limit(self, netuid, block=None):
        return 1.0

    def _record(name):
        def method(self, *a, **k):
            self._c.submissions.append(name)
        return method

    set_weights = _record("set_weights")
    commit_weights = _record("commit_weights")
    reveal_weights = _record("reveal_weights")
    set_commitment = _record("set_commitment")
    set_reveal_commitment = _record("set_reveal_commitment")
    mev_submit_encrypted = _record("mev_submit_encrypted")
    sign_and_send_extrinsic = _record("sign_and_send_extrinsic")


class FakeMetagraph:
    def __init__(self, chain, netuid):
        self._c = chain
        self.netuid = netuid
        self.hotkeys, self.coldkeys, self.block_at_registration = [], [], []
        self.block = None

    def sync(self, subtensor=None, block=None, lite=True):
        c = self._c
        self.hotkeys = list(c.hotkeys)
        self.coldkeys = list(c.coldkeys)
        self.block_at_registration = list(c.regs)
        self.block = block + c.sync_block_offset


class FakeApi:
    """Loopback HTTP API implementing the metagraph-sync handler contract."""

    def __init__(self, *, key=SYNTH_KEY, netuid=353, override=None, redirect_to=None):
        self.key, self.netuid = key, netuid
        self.override, self.redirect_to = override, redirect_to
        self.active = {}            # hotkey -> uid, active=true rows
        self.requests = []
        self.last_block = None
        api = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, status, payload):
                raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                if api.redirect_to and status in (301, 302, 307, 308):
                    self.send_header("Location", api.redirect_to)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                api.requests.append({"path": self.path, "headers": dict(self.headers.items()), "body": raw})
                if api.redirect_to:
                    return self._reply(307, b"")
                if self.path != "/api/validator/metagraph-sync":
                    return self._reply(404, {"error": "not found"})
                if self.headers.get("X-Internal-API-Key") != api.key:
                    return self._reply(401, {"error": "unauthorized"})
                if api.override is not None:
                    return self._reply(*api.override)
                return self._reply(*api.apply(json.loads(raw)))

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def apply(self, req):
        seen, miners = set(), []
        for m in req.get("miners") or []:
            if not m.get("hotkey") or m.get("uid", -1) < 0 or m["hotkey"] in seen:
                continue
            seen.add(m["hotkey"])
            miners.append(m)
        netuid, block = req.get("netuid"), req.get("metagraph_block", 0)
        if miners:
            if netuid != self.netuid:
                return 400, {"error": "Metagraph sync netuid must match the configured subnet"}
            status = "ok"
        else:
            if not req.get("authoritative_empty"):
                return 200, {"status": "ignored_unqualified_empty", "active_miners": 0, "marked_offline": 0}
            if netuid != self.netuid or block <= 0:
                return 400, {"error": "Authoritative empty sync requires matching netuid and a positive metagraph block"}
            if self.last_block is not None and block < self.last_block:
                return 200, {"status": "ignored_stale_empty", "active_miners": 0, "marked_offline": 0}
            status = "ok_empty_authoritative"
        new = {m["hotkey"]: m["uid"] for m in miners}
        offline = sum(1 for hk in self.active if hk not in new)
        self.active = new
        if block > 0 and (self.last_block is None or block > self.last_block):
            self.last_block = block
        return 200, {"status": status, "active_miners": len(miners), "marked_offline": offline}

    def close(self):
        self.server.shutdown()
        self.server.server_close()
