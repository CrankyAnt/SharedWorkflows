# Repository hygiene

The consumer kit of repository hygiene, a shared part of the [catalogue](../../../catalogue.json). A repository adopts it through one caller, [repository-hygiene.yml](repository-hygiene.yml), which calls the shared workflow `CrankyAnt/SharedWorkflows/.github/workflows/repository-hygiene.yml@v1`. Repository hygiene applies to every GitHub repository, a Markdown-only one included.

## The caller

Copy [repository-hygiene.yml](repository-hygiene.yml) to `.github/workflows/repository-hygiene.yml` and replace `<default-branch>` with the repository's default branch. The caller sets `permissions: {}` at the top and `actions: read`, `contents: read` and `pull-requests: read` on its one job, passes no secrets and runs no step of its own. It runs on pull requests, `edited` included, on pushes to the default branch, and on the `repository_dispatch` event `dependabot-merged`, which Dependabot auto-merge sends after each merge; on that event the job checks out the commit that `client_payload.sha` names, or the default branch's tip without one.

The job id `hygiene` gives the check `hygiene / hygiene`. It is the one check that repository hygiene adds to the checks the default-branch ruleset requires.

## What the job checks

The one job `hygiene` runs the floor on every repository:

- On pull requests, the convention checks: the branch name, every commit subject between the merge base and the head, and the pull request title. A branch name is `feat/`, `fix/`, `hotfix/`, `chore/` or `release/` with a description of lowercase letters, digits and single hyphens, `release/v<version>`, or `research/<name>`; agent prefixes such as `claude/` or `codex/` fail. A commit subject and a pull request title follow Conventional Commits, with a lowercase description and no trailing period, and a title carries no issue number. Merge commits, GitHub's revert branches and revert subjects pass, and a pull request authored by `dependabot[bot]` or `renovate[bot]` skips the convention checks with a visible message.
- On every event, Gitleaks over every commit reachable from any ref.

It then runs each shared check that the change can affect:

| Check | Runs when the change touches |
| --- | --- |
| actionlint, with ShellCheck | a workflow, the metadata of a local action that a workflow uses as `uses: ./…`, or the actionlint configuration |
| zizmor, offline over workflows and action definitions | a workflow, an action definition or the zizmor configuration |
| Dependabot coverage: every recognised package file has an `updates` entry for its ecosystem and directory | a package file, the Dependabot configuration, or the caller's `coverage-exclude` or `uses:` reference |
| Relative links: every relative link in tracked Markdown points to an existing file or folder and, with an anchor, to an existing heading | a Markdown file, a removed or renamed file, or the caller's `link-exclude` or `uses:` reference |
| `reuse lint` | only with a `REUSE.toml`: an added, renamed or removed file, `REUSE.toml` or a file under `LICENSES/` |

Every check runs when the changed files cannot be determined. A push whose tree passed on its pull request runs only Gitleaks. On an `edited` event the convention checks and Gitleaks run again, and each other check that passed on the same tree reports that result. Every running check runs even after an earlier one failed, and the job fails when one did.

## Inputs and the files it reads

Every input is optional, and an omitted input leaves its tool on its own default:

| Input | Value |
| --- | --- |
| `gitleaks-config` | the Gitleaks configuration file, such as `.gitleaks.toml` |
| `gitleaks-baseline` | the Gitleaks baseline report, such as `.gitleaks-baseline.json` |
| `zizmor-persona` | `pedantic` or `auditor`, for a stricter zizmor persona than the default |
| `coverage-exclude` | the package files that are test fixtures, one path per line |
| `link-exclude` | globs, one per line, of Markdown files the repository does not maintain, such as vendored documentation; a link into them is still followed |

Pass `gitleaks-config` and `gitleaks-baseline` with the files that the repository's own Gitleaks command passes with `--config` and `--baseline-path`, so the local scan before a push and CI scan alike. An example with inputs:

```yaml
    uses: CrankyAnt/SharedWorkflows/.github/workflows/repository-hygiene.yml@v1
    with:
      gitleaks-config: .gitleaks.toml
      gitleaks-baseline: .gitleaks-baseline.json
      coverage-exclude: |
        tests/fixtures/package.json
```

Beside the inputs, the job reads these files of the repository when they exist: the workflows under `.github/workflows/` and every `action.yml`; `.github/actionlint.yml` or `.yaml`; `.github/zizmor.yml` or `zizmor.yml`, either with a `.yaml` extension, in place of the shared zizmor policy; `.github/dependabot.yml` or `.yaml` and every tracked package file; every tracked Markdown file; and `REUSE.toml` with `LICENSES/`. A recognised package file is a package manifest of an ecosystem that Dependabot updates, such as `package.json`, `pyproject.toml` or `global.json`; a workflow is none. Without a Dependabot configuration, the coverage check passes only when the repository has no recognised package file.

## Repository settings

The user configures these settings; the job changes none of them:

- The squash and merge-commit subjects are the pull request title with an empty body: `squash_merge_commit_title` and `merge_commit_title` are `PR_TITLE`, and `squash_merge_commit_message` and `merge_commit_message` are `BLANK`.
- Rebase merge is disabled.
- A ruleset on the default branch requires `hygiene / hygiene` with GitHub Actions as its source. The ruleset editor lists a check only after it succeeded in the repository within the past 7 days, so the caller reaches the default branch and reports on a pull request before the ruleset names it.

## When `v1` moves

A move of `v1` changes the later runs of the caller without a change in the repository. A check that the move adds runs on the repository's first change that starts it, as the table above says. When that change fails on what the repository already holds, fix it in the same pull request:

- A dead relative link: repair the link, or, for Markdown the repository does not maintain, pass its globs in `link-exclude`.
- An uncovered package file, such as a `global.json` once its ecosystem is recognised: add an `updates` entry for its ecosystem and directory to the Dependabot configuration, or, for a test fixture, name the file in `coverage-exclude`.

A failure that the repository cannot fix this way is a fault of the shared part: report it to the user.
