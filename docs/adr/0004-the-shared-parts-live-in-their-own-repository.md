# The shared parts live in their own repository, and every other repository is a consumer

The shared parts that several repositories use, from the CI checks to Dependabot auto-merge, the scheduled updaters and the ruleset template, live in this public repository, in which no product is developed. Every other repository is a consumer: its callers call the shared parts through the `v1` channel, and it holds only its own callers and configuration, while the shared parts are maintained and tested only here. Being public, the repository holds nothing secret, runs its own CI on standard runners without Actions minutes, and can be called by public repositories.

## Considered Options

- Keeping the shared parts in a repository that develops a product of its own makes that repository host, maintainer and a consumer of its own parts at once, so the maintenance of the shared parts, that repository's own CI and every other consumer run through one repository and its documents.
- A repository per shared part splits the `v1` channel, the pins file and the promotion per part.
- Workflow templates in an organisation's `.github` repository need an organisation and are copied into each consumer, where they drift.
