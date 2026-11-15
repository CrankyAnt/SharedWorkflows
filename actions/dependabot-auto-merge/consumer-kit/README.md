# Dependabot auto-merge

The consumer kit of Dependabot auto-merge, a shared part of the [catalogue](../../../catalogue.json). A repository adopts it by switching the part `dependabot-auto-merge` on in its configuration file, `.github/ci-config.yml`, and through the Dependabot configuration [dependabot.yml](dependabot.yml). It holds no workflow of its own for the part: the fixed caller `automation.yml` of the [pipeline's consumer kit](../../pipeline/consumer-kit/README.md) calls the shared workflow `CrankyAnt/SharedWorkflows/.github/workflows/automation.yml@v1`, whose job `dependabot-auto-merge` runs it.

## Switching it on

State the part under `parts` in `.github/ci-config.yml`. With the suite selection, `suites` names the path of its [suite list](../../suite-selection/consumer-kit/README.md#the-suite-list); the path here is an invented example:

```yaml
parts:
  dependabot-auto-merge:
    suites: ci/suites.json
```

A repository without the suite selection states `dependabot-auto-merge: {}`.

The fixed caller starts on every completed run of the workflow `CI` and grants its one job `contents: write` and `pull-requests: write`, and the part's job takes both: the first for the merge, the second to disable an auto-merge. The job runs for a completed pull-request run whose actor is `dependabot[bot]`, or the updater App on a branch that starts with `dependabot/`. For every other run its job-level condition skips it without a runner, and with the part switched off it is skipped as well. It checks out nothing and runs no code from the pull request.

The job's check is `automation / dependabot-auto-merge`. It is no required check.

## Which updates merge on their own

The part merges a pull request on its own when all of these hold:

- Dependabot opened it, the first commit's signature is verified, and every commit on it is Dependabot's or a commit of the updater App that changes only the suite list that `suites` names, as the [additions workflow](../../suite-selection/consumer-kit/README.md#additions) of the suite selection makes it.
- Every updated dependency is a patch or a minor update. A major update stays manual.
- For GitHub Actions, every updated action comes from `actions/*`. An action from any other owner stays manual.
- An active ruleset rule on the default branch requires at least one status check.

It then requests a squash merge once the required checks have passed, bound to the head commit it read, and sends a `repository_dispatch` event of type `dependabot-merged` with `client_payload.sha` set to the merged commit, on which the fixed caller `ci.yml` runs the pipeline for that commit. Every other pull request is left to the user, and an auto-merge that it still has is disabled when a run of `CI` completes for it, so a commit that Dependabot did not make ends an auto-merge that someone enabled. The part never enables GitHub's auto-merge, because a merge that GitHub makes later for it would start no push run.

Dependabot security updates arrive without the cooldown of the configuration and follow the same rules. In a repository whose required checks do not build or test the product, an automatic merge is checked only by those required checks.

## The Dependabot configuration

A repository with the fixed callers states its ecosystems under `dependabot` in the configuration file; `.github/dependabot.yml` is generated from them, and repository hygiene fails while the file differs, as the [pipeline's consumer kit](../../pipeline/consumer-kit/README.md#what-repository-hygiene-checks-in-these-files) says.

A repository without the fixed callers copies [dependabot.yml](dependabot.yml) to `.github/dependabot.yml`. It holds the `github-actions` entry: weekly, a cooldown of 7 days, one group `minor-and-patch` for minor and patch updates, and an ignore entry for `CrankyAnt/SharedWorkflows`, so that Dependabot does not replace the moving `@v1` of the shared workflows. Add one entry per ecosystem of the repository's package files, each with the same schedule, cooldown and group, with `directory` for one directory or `directories` for several. When the repository has a configuration already, keep its entries and add what is missing.

## Repository settings

The user configures these settings; the workflow changes none of them:

- Dependabot alerts are enabled.
- Dependabot security updates are enabled and not paused.
- A ruleset on the default branch requires status checks. Classic branch protection alone is not recognised, so a repository that protects its default branch that way only is blocked until it has a ruleset; the job then fails naming the missing ruleset.

## When a repository adopts it

A repository adopts it when it has a dependency of its own: a package file that Dependabot updates, or a workflow of its own that uses an action from outside the repository, other than a caller of a shared workflow. A repository without such a dependency has no Dependabot pull request to merge: it leaves the part switched off and holds no Dependabot configuration.

## When a repository adopted it earlier

A repository that adopted Dependabot auto-merge earlier holds a caller of its own, `.github/workflows/dependabot-auto-merge.yml`, which calls `CrankyAnt/SharedWorkflows/.github/workflows/dependabot-auto-merge.yml@v1`. That shared workflow stays at `v1` while a repository calls it, so the caller keeps working, and the kit holds no template for it any more. In the pull request that brings the fixed callers, the repository switches the part on as [Switching it on](#switching-it-on) says, with the suite list that its caller passed as `suites`, and removes its caller.

The Dependabot configuration comes from one template:

| Template | Install target |
| --- | --- |
| [dependabot.yml](dependabot.yml) | `.github/dependabot.yml` |

A repository without the fixed callers compares its installed file with the template, read at `v1`, and takes over every difference that the template makes, apart from its own `updates` entries, one per ecosystem of its package files with its `directory` or `directories`, and each entry the configuration held before the adoption, each with its own settings. The template's `github-actions` entry, with its ignore entry for `CrankyAnt/SharedWorkflows`, comes from the template, as [The Dependabot configuration](#the-dependabot-configuration) states. The result works when hygiene passes on that pull request, where the Dependabot coverage check runs over the changed configuration.
