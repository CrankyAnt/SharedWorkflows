# Dependabot auto-merge

The consumer kit of Dependabot auto-merge, a shared part of the [catalogue](../../../catalogue.json). A repository adopts it by switching the part `dependabot-auto-merge` on in its configuration file, `.github/ci-config.yml`, and declaring its ecosystems under `dependabot` there. It holds no workflow of its own for the part: the fixed caller `automation.yml` of the [pipeline's consumer kit](../../pipeline/consumer-kit/README.md) calls the shared workflow `CrankyAnt/SharedWorkflows/.github/workflows/automation.yml@v1`, whose job `dependabot-auto-merge` runs it.

## Switching it on

State the part under `parts` in `.github/ci-config.yml`. With the suite selection, `suites` names the path of its [suite list](../../suite-selection/consumer-kit/README.md#the-suite-list); the path here is an invented example:

```yaml
parts:
  dependabot-auto-merge:
    suites: ci/suites.json
```

A repository without the suite selection states `dependabot-auto-merge: {}`. Optionally, `updater-paths` lists additional relative paths or prefixes ending with `/` that a commit of the updater App may change while the pull request stays automatic.

The fixed caller starts on every completed run of the workflow `CI` and grants its one job `actions: read`, `contents: write` and `pull-requests: write`, and the part's job takes both: the first for the merge, the second to disable an auto-merge, and `actions: read` to confirm that the dispatched default-branch run started. The job runs for a completed pull-request run whose actor is `dependabot[bot]`, or the updater App on a branch that starts with `dependabot/`. For every other run its job-level condition skips it without a runner, and with the part switched off it is skipped as well. It checks out nothing and runs no code from the pull request.

The job's check is `automation / dependabot-auto-merge`. It is no required check.

## Which updates merge on their own

The part merges a pull request on its own when all of these hold:

- Dependabot opened it, the first commit's signature is verified, and every commit on it is Dependabot's or a commit of the updater App that changes only the suite list that `suites` names, as the [additions workflow](../../suite-selection/consumer-kit/README.md#additions) of the suite selection makes it, or a path in `updater-paths`.
- Every updated dependency is a patch or a minor update. A major update stays manual.
- For GitHub Actions, every updated action comes from `actions/*`. An action from any other owner stays manual.
- An active ruleset rule on the default branch requires `ci / verdict`.

If Dependabot omits a pip update type, the job can classify the update from the verified first commit, including a dependency updated in several directories. Every changed declaration must be a plain numeric `==` pin or a `>=` and `<` range in a `requirements*.txt` file. The job compares both range bounds. It leaves the pull request manual if either bound changes major version, a later commit changes pip inputs, the metadata disagrees with the new pin or lower bound, or the first commit gives incomplete or unsupported evidence. Other ecosystems require explicit update types. The pull request title and group name supply no merge evidence.

It then disables any auto-merge the pull request still has before requesting a squash merge once `ci / verdict` has passed, bound to the head commit it read; if GitHub refuses the merge because a required check is pending or failed, the pull request is a draft or not mergeable, the head moved, or it is already merged, the run succeeds without merging. After a merge it made itself, it sends a `repository_dispatch` event of type `dependabot-merged` with `client_payload.sha` set to the merged commit and confirms that a `repository_dispatch` run for that commit started (retrying transient errors, and reporting the manual dispatch command if no run could be started), on which the fixed caller `ci.yml` runs the pipeline for that commit. Every other pull request is left to the user, and an auto-merge that it still has is disabled when a run of `CI` completes for it, so a commit that Dependabot did not make ends an auto-merge that someone enabled. The part never enables GitHub's auto-merge, because a merge that GitHub makes later for it would start no push run.

Dependabot security updates arrive without the cooldown of the configuration and follow the same rules. In a repository whose required checks do not build or test the product, an automatic merge is checked only by those required checks.

## The Dependabot configuration

A repository states its ecosystems under `dependabot` in `.github/ci-config.yml`. `.github/dependabot.yml` is generated from them with an ignore entry for `CrankyAnt/SharedWorkflows`, and repository hygiene fails while the file differs, as the [pipeline's consumer kit](../../pipeline/consumer-kit/README.md#what-repository-hygiene-checks-in-these-files) says.

## Repository settings

The user configures these settings; the workflow changes none of them:

- Dependabot alerts are enabled.
- Dependabot security updates are enabled and not paused.
- A ruleset on the default branch requires status checks. Classic branch protection alone is not recognised, so a repository that protects its default branch that way only is blocked until it has a ruleset; the job then fails naming the missing ruleset.

## When a repository adopts it

A repository adopts it when it has a dependency of its own: a package file that Dependabot updates, or a workflow of its own that uses an action from outside the repository, other than a caller of a shared workflow. A repository without such a dependency has no Dependabot pull request to merge: it leaves the part switched off and holds no Dependabot configuration.

## When a repository adopted it earlier

The kit holds no template: the fixed caller `automation.yml` of the [pipeline's consumer kit](../../pipeline/consumer-kit/README.md) runs the part, and `.github/dependabot.yml` is generated from `dependabot` in `.github/ci-config.yml`. A repository that switches the part on or updates its configuration keeps `parts.dependabot-auto-merge` and `dependabot` in `.github/ci-config.yml` in step with its ecosystems and suite list.
