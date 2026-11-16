# Security policy

## Reporting a vulnerability

Please do not open a public issue, pull request or discussion for a suspected
vulnerability. Report it privately by e-mail to `hello@rec4ll.com` with the
subject line `SECURITY`.

Include the details you have:

- the release or commit you tested, and whether the validator runs as master or
  secondary;
- the affected module or guide, the impact, and steps to reproduce it;
- whether the issue is already known to anyone else.

Do not include real credentials, wallet secrets or customer data in a report.

## What to expect

- We aim to acknowledge a report within 3 working days and to give a first
  assessment within 10 working days.
- We will keep you informed while a fix is prepared and agree a disclosure date
  with you. Please give us reasonable time to ship a fix before publishing.

## Supported versions

Only the latest release receives security fixes.

## Scope

In scope: the code, configuration templates and operator guides in this
repository. Examples: a way to make a secondary accept a bundle the master did
not sign, to make a validator set weights it did not verify, or to leak a key.

Out of scope, and not permitted:

- testing against mainnet with transactions, or against validators, miners or
  gateways you do not operate;
- denial-of-service or load testing of the hosted Recall API or IPFS gateway;
- social engineering of operators.

For issues in the Recall API or miner, use the same private reporting contact
and identify the affected component. Report issues in Bittensor itself to the
Bittensor maintainers.
