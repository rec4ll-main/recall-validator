"""Equal split: the fallback that replaces burning.

Recall never burns miner emissions. When rewards cannot be computed (no usable bundle,
no miner left in it, or no miner earned anything this epoch), the weight goes to every
miner on the subnet in equal shares instead of to the subnet owner's UID.

"Miner" here means every registered UID except:
- the subnet owner hotkey (it is not a miner), and
- active validators: a validator permit and validator trust above zero.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("recall.equal_split")


def select_miners(hotkeys, validator_permit=None, validator_trust=None, exclude_hotkeys=()) -> list[tuple[int, str]]:
    """(uid, hotkey) of every miner, ascending uid. Pure: no chain access."""
    exclude = {h for h in exclude_hotkeys if h}
    out = []
    for uid, hk in enumerate(hotkeys):
        if not hk or hk in exclude:
            continue
        permit = bool(validator_permit[uid]) if validator_permit is not None and uid < len(validator_permit) else False
        vtrust = float(validator_trust[uid]) if validator_trust is not None and uid < len(validator_trust) else 0.0
        if permit and vtrust > 0:
            continue
        out.append((uid, str(hk)))
    return out


def _owner_hotkey(subtensor, netuid: int) -> str | None:
    """The subnet owner hotkey (None: no owner set). A failed read raises: the split is
    then not built, so the owner can never be paid by accident."""
    result = subtensor.substrate.query("SubtensorModule", "SubnetOwnerHotkey", [int(netuid)])
    value = getattr(result, "value", result)
    return str(value) if value else None


def chain_miners(subtensor, netuid: int, exclude_hotkeys=()) -> list[tuple[int, str]]:
    """Miners from a fresh metagraph read. Raises on a failed metagraph or owner read."""
    mg = subtensor.metagraph(int(netuid), lite=True)
    owner = _owner_hotkey(subtensor, netuid)
    tv = getattr(mg, "Tv", None)
    if tv is None:
        tv = getattr(mg, "validator_trust", None)
    return select_miners(list(mg.hotkeys), getattr(mg, "validator_permit", None), tv,
                         exclude_hotkeys=(owner, *exclude_hotkeys))


def snapshot_miners(snapshot_hotkeys, metagraph, owner_hotkey=None, exclude_hotkeys=()) -> list[tuple[int, str]]:
    """Miners from an epoch snapshot's chain hotkeys. Validator flags come from
    ``metagraph`` when its arrays cover the same UIDs; otherwise none are excluded as
    validators (paying a validator UID is better than burning)."""
    n = len(snapshot_hotkeys)

    def arr(name):
        a = getattr(metagraph, name, None)
        try:
            return a if a is not None and len(a) == n else None
        except TypeError:
            return None

    tv = arr("Tv")
    if tv is None:
        tv = arr("validator_trust")
    return select_miners(list(snapshot_hotkeys), arr("validator_permit"), tv,
                         exclude_hotkeys=(owner_hotkey, *exclude_hotkeys))


