# Dependabot auto-merge

The consumer kit of Dependabot auto-merge, a shared part of the [catalogue](../../../catalogue.json). A repository adopts it through two files: the caller [dependabot-auto-merge.yml](dependabot-auto-merge.yml), which calls the shared workflow `CrankyAnt/SharedWorkflows/.github/workflows/dependabot-auto-merge.yml@v1`, and the Dependabot configuration [dependabot.yml](dependabot.yml).

## The caller

Copy [dependabot-auto-merge.yml](dependabot-auto-merge.yml) to `.github/workflows/dependabot-auto-merge.yml` and replace `<required-check-workflows>` with the `name:` of every workflow that produces a check the default-branch ruleset requires, comma-separated, such as `Repository hygiene, Validation`. The caller sets `permissions: {}` at the top and `contents: write` and `pull-requests: write` on its one job, passes no inputs and no secrets and runs no step of its own. It runs on `pull_request` events of the types `opened`, `synchronize` and `reopened`, and on `workflow_run` events of the type `completed` of each workflow it names. zizmor reports `workflow_run` as a dangerous trigger; the inline ignore on it states why the finding does not apply, because the shared workflow checks out nothing and runs no code from the pull request.

The job id `dependabot-auto-merge` gives the checks `dependabot-auto-merge / auto-merge` and `dependabot-auto-merge / merge`. Neither is a required check: GitHub counts a skipped job as a success for required checks, and both jobs are skipped for every pull request that is not Dependabot's.

## Which updates merge on their own

The shared workflow merges a pull request on its own when all of these hold:

- Dependabot opened it and every commit on it is Dependabot's, and the first commit's signature is verified.
- Every updated dependency is a patch or a minor update. A major update stays manual.
- For GitHub Actions, every updated action comes from `actions/*`. An action from any other owner stays manual.
- An active ruleset rule on the default branch requires at least one status check.

It then requests a squash merge once the required checks have passed, bound to the head commit it read, and sends a `repository_dispatch` event of type `dependabot-merged` with `client_payload.sha` set to the merged commit. Every other pull request is left to the user, and an auto-merge it still has is disabled. The workflow never enables GitHub's auto-merge, because a merge that GitHub makes later for it would start no push run.

Dependabot security updates arrive without the cooldown of the configuration and follow the same rules. In a repository whose required checks do not build or test the product, an automatic merge is checked only by those required checks.

## The Dependabot configuration

Copy [dependabot.yml](dependabot.yml) to `.github/dependabot.yml`. It holds the `github-actions` entry: weekly, a cooldown of 7 days, one group `minor-and-patch` for minor and patch updates, and an ignore entry for `CrankyAnt/SharedWorkflows`, so that Dependabot does not replace the moving `@v1` of the shared workflows. Add one entry per ecosystem of the repository's package files, each with the same schedule, cooldown and group, with `directory` for one directory or `directories` for several. When the repository has a configuration already, keep its entries and add what is missing.

## Repository settings

The user configures these settings; the workflow changes none of them:

- Dependabot alerts are enabled.
- Dependabot security updates are enabled and not paused.
- A ruleset on the default branch requires status checks. Classic branch protection alone is not recognised, so a repository that protects its default branch that way only is blocked until it has a ruleset; the job then fails naming the missing ruleset.

## When a repository adopts it

A repository adopts it when it has a dependency of its own: a package file that Dependabot updates, or a workflow of its own that uses an action from outside the repository, other than a caller of a shared workflow. A repository without such a dependency has no Dependabot pull request to merge and adopts neither file.
