# CI

The CI of this repository runs through [Shared Workflows](https://github.com/CrankyAnt/SharedWorkflows), which its fixed callers call at the tag `v1`. Down to and including the heading `## Repository choices`, this file is a fixed pointer, equal byte for byte in every repository that does so: it holds no rule of its own, and repository hygiene fails a pull request that changes that part.

- The catalogue, read at `v1`, is the one entry: [catalogue.json](https://github.com/CrankyAnt/SharedWorkflows/blob/v1/catalogue.json). It holds the decision rule for CI work, every shared part with its configuration, the local workflow register and the way to request shared CI.
- The choices of this repository that a shared part reads are in its configuration file, `.github/ci-config.yml`. Its schema, read at `v1`, describes every key: [ci-config.schema.json](https://github.com/CrankyAnt/SharedWorkflows/blob/v1/actions/pipeline/schema/v1/ci-config.schema.json).

Before adding or changing a check, a job or a workflow, read the catalogue and follow its decision rule. A choice of this repository that no file states, such as a deviation of its ruleset from the template, is recorded with its reason below the next heading.

## Repository choices
