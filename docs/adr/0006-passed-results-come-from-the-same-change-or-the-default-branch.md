# Passed results come from the same change or from the default branch

A check takes over a passed result whose inputs are exactly the same only when a run of the same change or a run on the protected default branch wrote it. A pull request and its merge onto the default branch are the same change, so the merge takes over the results its own pull request passed. A pull request never writes a result that another change takes over: a pull request's record is written by code its head controls, while a default-branch run executes only code that landed through the protected default branch. Reuse from the default branch spares every pull request a first run of work the default branch already passed, and after a move of `v1`, once one default-branch run has passed under the new shared digest, pull requests take over its results instead of each running them again.

## Considered Options

- Reuse only within one pull request (ADR 0002): every pull request pays its own first run, and a move of `v1` makes every pull request run every suite until its base passed under the new shared digest.
- Records of other pull requests, matched on the reuse key: the keys are computable from public content and the records are writable by each pull request's head, so one pull request could make another skip a unit that never passed for its content.
- Reuse only from default-branch runs: the merge of a pull request would run again what its pull request already passed on the same tree, which is the largest saving reuse gives.

## Consequences

Default-branch runs keep a record of their passed results that pull requests can read and cannot write. Whoever can push to a pull-request branch already controls what that branch's CI does; the merge's takeover grants nothing beyond that write access.
