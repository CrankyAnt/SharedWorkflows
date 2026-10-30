# CI

## Principles

Every agent that adds or changes a check, a suite or a workflow holds to these principles; how it builds the change is its own choice.

- Every check has a purpose in the repository as it is now: it has been seen red on the condition it guards and stays green otherwise; an obsolete, flaky or fully covered check goes.
- A check runs only on changes that can change its result, judged by what it actually reads; it runs on every change only when every change can, as for the secret scan.
- A change to prose that no code reads starts only light checks; a document that code reads as data starts that code's checks.
- Each suite has one subject and every test a place in a suite.
- A job on another platform runs only where its result can differ there.

## Shared callers

This repository calls its own shared workflows by local path, `uses: $/.github/workflows/<file>`, from its callers and its validation caller:

| Caller | Check | Inputs |
| --- | --- | --- |
| `repository-hygiene-caller.yml` | `hygiene / hygiene` | `gitleaks-config: .gitleaks.toml`, `gitleaks-baseline: .gitleaks-baseline.json` |
| `dependabot-auto-merge-caller.yml` | `auto-merge / auto-merge`, `auto-merge / merge` | none |
| `validate.yml` | `validation / validation` | on `select`: `suites: tests/suites.json`, `language-steps: python`, `runner-labels: linux=ubuntu-26.04 windows=windows-2025`, `setup-versions: python=3.13` |

- `repository-hygiene-caller.yml` calls `repository-hygiene.yml` and runs the floor and the selected shared checks in the one job `hygiene / hygiene`. The floor is the convention checks on pull requests, of the branch name, every commit subject and the pull-request title, and the credential scan of every commit reachable from any ref on every event. The convention checks skip genuine merge commits by parent count. A pull request authored by exactly `dependabot[bot]` or `renovate[bot]` skips them with visible messages, also on events that a human triggers, and a branch name grants no exception. The selected shared checks are the validation of the workflow definitions, the check that every tracked package file has a Dependabot entry, the link check, which checks that every relative link in tracked Markdown points to an existing file or folder and, with an anchor, to an existing heading, and `reuse lint` with a `REUSE.toml`. Each runs only when the change can affect it: the link check, for example, when a change touches a Markdown file, removes or renames a file, or changes the caller's `link-exclude` or its `uses:` reference. On an `edited` event the convention checks and the credential scan run again. On the first attempt of an `edited` run, each other check that passed on the same tested tree reports that result with the run it came from instead of running.
- `dependabot-auto-merge-caller.yml` calls `dependabot-auto-merge.yml`. It merges a Dependabot pull request whose updates are all patch or minor and, for GitHub Actions, all from `actions/*`, once its required checks have passed; every other update stays manual. It needs a default-branch ruleset that requires checks. It runs on the pull request, where it enables no auto-merge and disables one that the pull request still has. It also runs on `workflow_run` when a workflow it names under `workflows:` completes; those workflows are every workflow that produces a check the default-branch ruleset requires, here `Repository hygiene` and `Validate`. There it decides again, requests a squash merge bound to the head commit it read, and after its own merge sends a `dependabot-merged` dispatch with the merged commit. Its job holds `contents: write` and `pull-requests: write`: a run that Dependabot triggers starts with a read-only token, and a shared workflow can only reduce the rights of the job that calls it; the merge job keeps `contents: write` for the merge and the dispatch and reads pull requests only. Its checks are no required checks; they are skipped for every pull request that is not Dependabot's.
- `validate.yml` is this repository's own validation caller: it selects the units from the stored lists of the suite list `tests/suites.json`, runs them under the recording step and reports the one required check `validation / validation`, as [Suite selection](#suite-selection) states.

The hygiene caller and the validation caller also run on a `repository_dispatch` event of type `dependabot-merged`, whose `client_payload.sha` names a merged commit on the default branch: they check out that commit and select from its first parent to it, as a push of that commit would. Without `client_payload.sha` they take the default branch's tip in its place and select as for a push of that commit.

The checks to require in a ruleset on the default branch are `hygiene / hygiene` and `validation / validation`.

## Inputs

The hygiene caller may set `zizmor-persona` to `pedantic` or `auditor`, for a stricter zizmor persona than the default, and `link-exclude` to the globs, one per line, of Markdown files the repository does not maintain, such as vendored documentation, whose relative links the link check then leaves alone. A caller that sets either records the input with its value and its reason under [Repository choices](#repository-choices). No other input is the repository's own choice.

The other inputs of the hygiene caller follow from the repository: `gitleaks-config` and `gitleaks-baseline` name the files that the Gitleaks command in `docs/agents/delivery.md` passes with `--config` and `--baseline-path`, and `coverage-exclude` lists the path of each package file that is a test fixture, one per line.

## Dependabot

A new package file that Dependabot supports gets an `updates` entry in `.github/dependabot.yml`. When no entry for its ecosystem covers its directory through `directory` or `directories`, extend the ecosystem's entry or add one, with `schedule`, and `cooldown` and `groups` where present, copied from an existing entry, preferring the `github-actions` entry. A package file that is a test fixture, which nothing installs, gets no entry; its path goes into the hygiene caller's `coverage-exclude`.

## Suite selection

`validate.yml` is the validation caller, which calls `suite-selection.yml` and `suite-verdict.yml` by local path on the suite list `tests/suites.json`, runs the selected entry files in shards on `ubuntu-26.04` and the Windows job on `windows-2025` under the recording step, and reports the required check `validation / validation`. `full-recording.yml` is its full recording. `tests/README.md` describes the suites and how they run, and `docs/maintaining.md` the shared workflows and shared steps themselves.

## Repository choices

None.
