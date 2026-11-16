# Child-hotkey delegation

You can support Recall without running a validator by delegating a share of your
parent hotkey's stake weight to a child hotkey on subnet 31. You do not need to
install this repository or obtain Recall API credentials.

Get the intended child's public SS58 address from Recall. Check its registration
on Finney subnet 31 and its child take before making the change; the master
validator address is not automatically the delegation target. Bittensor's
[child-hotkey guide](https://www.bittensor.com/docs/tx/set-children) explains the
network's delegation rules.

The example below assigns **100% to one child and replaces the existing child
list**. Review your current children first. Checked with btcli 9.23.2; run
`btcli stake child set --help` if you use a different version.

`<wallet>` and `<parent hotkey>` are your local wallet and hotkey **names** (the
directory and file names under `~/.bittensor/wallets`); `<child hotkey ss58>` is the
public address of the hotkey you delegate to:

```bash
btcli stake child set --netuid 31 --wallet-name <wallet> --hotkey <parent hotkey> \
  --children <child hotkey ss58> --proportions 1.0 --network finney
```

This is a coldkey-signed transaction. Keep seeds and private keys out of the
command, and keep the coldkey off the validator host. Delegation and child take
follow Bittensor's rules; delegation does not guarantee earnings.
