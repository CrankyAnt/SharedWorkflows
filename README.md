# Shared Workflows

Shared Workflows holds the shared workflows, shared steps and tools that the repositories of `CrankyAnt` call at the tag `v1`: repository hygiene, Dependabot auto-merge, the suite selection with its recording step, and the Python language step.

## Use the shared parts

A repository that uses the shared parts, and its agent, starts from the [catalogue](catalogue.json), read at `v1`: one entry per shared part with its purpose, what it needs and when it applies. Each part is adopted through its consumer kit:

- [Repository hygiene](actions/repository-hygiene/consumer-kit/README.md), with the [ruleset template](actions/repository-ruleset/README.md) for the default branch
- [Dependabot auto-merge](actions/dependabot-auto-merge/consumer-kit/README.md)
- [Suite selection](actions/suite-selection/consumer-kit/README.md)
- [Python language step](actions/python-language-step/consumer-kit/README.md)

## Maintain this repository

For maintainers of this repository only, [docs/maintaining.md](docs/maintaining.md) describes what the workflows do and how the shared parts change, starting from its [recurring tasks](docs/maintaining.md#recurring-tasks). A repository that uses the shared parts needs none of it.
