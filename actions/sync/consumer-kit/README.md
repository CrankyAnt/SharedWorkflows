# Management repository

The consumer kit of the sync, a shared part of Shared Workflows: the management repository kit. A private repository built from this kit runs the sync every twelve hours and on a start by hand. The sync compares every repository that the sync App is installed on with the templates at `v1`, opens a pull request with auto-merge where fixed files differ, and opens an issue in the management repository for what needs a person.

The management repository holds four things and nothing else:

| Template | Install target |
| --- | --- |
| [sync.yml](sync.yml) | `.github/workflows/sync.yml` |
| [AGENTS.template.md](AGENTS.template.md) | `AGENTS.md` |
| this README | `README.md` |
| the environment `sync` | repository settings, as [The environment](#the-environment) says |

Copy `sync.yml` without changing it, and copy `AGENTS.template.md` without changing its text into the management repository as `AGENTS.md`. No file of the management repository names a repository that the sync reaches: the sync asks GitHub for the repositories of the App's installation.

## The caller

`sync.yml` starts on a schedule at 05:23 and 17:23 UTC and on `workflow_dispatch`, sets `permissions: {}` at the top and holds one job, `sync`, that calls `CrankyAnt/SharedWorkflows/.github/workflows/sync.yml@v1` with `issues: write` and passes the secret `SYNC_APP_PRIVATE_KEY` under its own name. The job runs no step of its own. The shared workflow runs only on `main`, reads the key in the environment `sync` and pins its actions to full commits.

`issues: write` is for the token of the run, with which the sync opens issues in the management repository. The App's token never reaches the management repository.

## The sync App

The sync App is a GitHub App of its own, used for nothing else.

| Permission | Level | Use |
| --- | --- | --- |
| Contents | write | push the sync's branch |
| Pull requests | write | open the sync pull request and request auto-merge |
| Workflows | write | change the fixed callers under `.github/workflows/` |
| Issues | read | find open issues with the label `shared-ci` |
| Metadata | read | read a repository, its merge settings and its rulesets |

The App holds no Administration permission, so its key cannot change a setting or a ruleset. It subscribes to no event and needs no webhook.

Install the App on the repositories that the sync keeps equal, chosen one by one. Never install it on `CrankyAnt/SharedWorkflows`: a key that reaches that repository could change what every consumer runs. The sync fails before any write when it finds that repository in the installation. The App is not installed on the management repository either.

## The environment

The key is an environment secret, so that only a run on `main` can read it.

1. Create the environment `sync` in the management repository.
2. Under its deployment branches and tags, choose selected branches and add `main` as the only branch.
3. Add the environment variable `SYNC_APP_CLIENT_ID` with the App's Client ID.
4. Generate a private key for the App and add it as the environment secret `SYNC_APP_PRIVATE_KEY`. Delete the downloaded key file afterwards.

The repository holds no repository secret and no repository variable. Protect `main` against pushes by anyone but the owner, since code on `main` can use the key.

To rotate the key, generate a new private key for the App, replace the environment secret, start the workflow by hand, and delete the old key from the App once that run passed.

## What a run does

For every repository of the installation that holds `.github/ci-config.yml` and is not archived, the sync compares:

- the fixed callers `ci.yml`, `automation.yml` and `release.yml` and the CI guide `docs/agents/ci.md` with their templates, the CI guide up to and including its heading `## Repository choices`, below which a repository's own choices stay;
- `.github/dependabot.yml` with the file generated from `dependabot` in the configuration file;
- the default-branch ruleset with the ruleset template, with `ci / verdict` as its one required check;
- the merge settings with the values that the shared parts rely on.

Where files differ, it opens one pull request on the branch `chore/sync-shared-workflows` with the templates' content and requests auto-merge. It waits for nothing. A later run keeps the same pull request and moves its branch when the templates changed again.

A repository without `.github/ci-config.yml` has not adopted the pipeline. The run summary says so and the sync leaves it alone.

## The issues

A run opens one issue in the management repository for each finding. An issue carries a marker in its body by which a later run finds it again.

| Issue | Means | What it asks for |
| --- | --- | --- |
| Sync pull request not merged after six hours | a sync pull request is still open six hours after it was opened | look at its checks; a repository's own failing CI or a missing auto-merge setting holds it |
| Request for shared CI | a repository holds an open issue with the label `shared-ci` | the triage of Shared Workflows takes the request up |
| Settings differ | a ruleset or a merge setting differs from the template | correct it with the sync's local mode, below |
| The sync failed | the run failed | read the run's log; the issue links it |

A pull request and a request get one issue ever, also after the issue is closed. A settings difference and a failed run get no second issue while one is open, and a new one when the finding is still there after the issue was closed. A run without findings opens no issue.

## The local mode

The sync changes no setting with the App's token. The owner corrects settings, and adopts a repository, from a checkout of `CrankyAnt/SharedWorkflows` at `v1` with the owner's own login:

```text
GH_TOKEN="$(gh auth token)" python3 actions/sync/sync.py sync --local --repository <owner>/<repository>
GH_TOKEN="$(gh auth token)" python3 actions/sync/sync.py adopt --repository <owner>/<repository>
```

`sync --local` does for the named repositories what a run does and also corrects each settings difference. `adopt` makes the adoption pull request of a repository that holds no configuration file yet and reports what it could not derive from the repository's workflows. An agent asks the user before it runs either command, because both change another repository.

## When a repository adopted it earlier

A change to a template reaches the management repository through a commit on `main` that replaces the installed file with the current template: [sync.yml](sync.yml) at `.github/workflows/sync.yml` and [AGENTS.template.md](AGENTS.template.md) at `AGENTS.md`. Nothing of the installed file carries over. The sync does not reach the management repository, so this replacement is done by hand, after a promotion that changes a template of this kit.
