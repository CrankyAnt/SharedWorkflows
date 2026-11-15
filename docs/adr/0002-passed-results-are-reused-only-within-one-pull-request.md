# Passed results are reused only within one pull request

The suite selection reuses a unit's passed result only from the record of the same pull request: one cache entry in the pull request's scope, `suite-results-<number>-run-<run id>-<attempt>`, restored only by that pull request's runs. A pull request's record is written by code that its head controls: the `units` output, the `needs` context and the `suite-results-*` artifacts all come from the pull request's merge commit, so a head can write any key into it. A reuse key is a SHA-256 over public content, the tree's reads, the command, the versions, the runner label and the shared digest, so anyone can compute the key of a unit in another pull request. A record taken across pull requests could therefore make another pull request skip a unit that never passed for its content. Each pull request instead pays its own first run, and after a move of `v1` that changes the shared digest, pull-request runs run every suite until a run on their base branch has passed under the new digest.

## Considered Options

- A record from another pull request, matched on the reuse key, would save the first full run of every pull request, but its keys and its content are writable by that pull request's head.
- Records stored by default-branch runs would come from code that no pull-request head controls, but the suite selection keeps no such record.
