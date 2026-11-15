# Pipeline

The consumer kit of the pipeline, a shared part of the [catalogue](../../../catalogue.json). A repository holds three fixed callers, copied unchanged from this kit, and one configuration file, `.github/ci-config.yml`. The callers hold no choice of the repository: every choice is in the configuration file.

## The fixed callers

Copy each template to its install target without changing it. A fixed caller equals its template byte for byte in every repository, so it holds no placeholder; the default branch is `main`.

| Template | Install target | Starts on | Calls |
| --- | --- | --- | --- |
| [ci.yml](ci.yml) | `.github/workflows/ci.yml` | pull requests and pushes to `main` | `CrankyAnt/SharedWorkflows/.github/workflows/pipeline.yml@v1` |
| [automation.yml](automation.yml) | `.github/workflows/automation.yml` | a completed `CI` run, a weekly schedule, a start by hand and an opened issue | `CrankyAnt/SharedWorkflows/.github/workflows/automation.yml@v1` |
| [release.yml](release.yml) | `.github/workflows/release.yml` | a pushed tag and a start by hand | `CrankyAnt/SharedWorkflows/.github/workflows/release.yml@v1` |

Each caller sets `permissions: {}` at the top, holds one job named after its file and runs no step of its own.

- `ci.yml` is the read caller. Its job `ci` holds `actions: read`, `contents: read`, `issues: read` and `pull-requests: read` and passes no secret. The pipeline's verdict reports as the check `ci / verdict`.
- `automation.yml` is the write caller. Its job holds `actions: read`, `contents: write`, `issues: write` and `pull-requests: write` and passes the secret `PUBLICATION_CHECKS_APP_PRIVATE_KEY` under its own name. A part that the configuration file does not switch on does nothing.
- `release.yml` is the release caller. Its job holds `attestations: write`, `contents: write` and `id-token: write` and passes no secret. Without release configuration it publishes nothing, as [The release part](#the-release-part) says.

No caller starts on `pull_request_target`, and none inherits secrets.

## The configuration file

`.github/ci-config.yml` matches the schema [ci-config.schema.json](../schema/v1/ci-config.schema.json), which describes every key. The smallest file is the line `version: 1`. It holds the shared parts that the repository switches on under `parts`, its Dependabot ecosystems under `dependabot` and its local workflow register under `local-workflows`.

## The release part

The part `github-release` publishes a GitHub release with files of the repository for a signed tag. The release caller starts on every pushed tag; the part decides whether the tag is released. Switch it on under `parts`:

```yaml
parts:
  github-release:
    tags: ["v*"]
    allowed-signers: .github/allowed_signers
    files: [dist/tool.zip]
    script: ci/build-release.sh
    setups: [dotnet-10]
```

| Setting | Meaning |
| --- | --- |
| `tags` | The patterns of the tags that are released. A tag that matches none publishes nothing, and the run passes. |
| `allowed-signers` | The file with the SSH keys that may sign a release tag, in the format of ssh-keygen's allowed signers. It is read from `main`, never from the tagged commit: a new key signs releases once the change that adds it is on `main`. |
| `files` | The files that the release publishes, each under its file name. Without `script` they are tracked files of the tagged commit. |
| `script` | Optional. A script of the repository that writes the files. It runs with Bash from the repository root at the tagged commit, with read permission only and without secrets, as the free job runs its script. |
| `setups` | Optional, with `script`. The known setups that are installed before the script, in the listed order. |
| `runner-label` | Optional, with `script`. The label of the GitHub-hosted runner of the script's job; `ubuntu-26.04` without it. |
| `title` | Optional. The title of the release, in which `{tag}` stands for the tag and `{version}` for the tag without a leading `v`; the tag without it. |

A release tag is an annotated tag with an SSH signature, made with `git tag -s` and `gpg.format` set to `ssh`. A matching tag that is not annotated, carries no signature or carries one of a key that the file on `main` does not hold publishes nothing and fails the run, with the reason in the job summary of the job `plan`.

For a tag that passes, the files are collected at the commit that the tag names, their build provenance is attested in a repository that is not private, and the release is created under the tag with the files. A tag that already has a release fails the run and changes nothing. Without the part in the configuration file, a pushed tag publishes nothing and the run passes.

## What repository hygiene checks in these files

Repository hygiene runs in every run of the pipeline and checks the files of this kit on every event:

| Check | Fails when |
| --- | --- |
| Fixed callers | a fixed caller is missing or differs from its template |
| Local workflows | a workflow file under `.github/workflows/` is no fixed caller and has no entry under `local-workflows` |
| Register | a temporary entry names a `request` that is no issue of the repository, or a permanent entry names a `declined-request` that is no closed issue of the repository; a pull request is no issue |
| Dependabot configuration | `.github/dependabot.yml` differs from the file generated from `dependabot`, or exists while the configuration file states no ecosystems |

The generated `.github/dependabot.yml` holds one `updates` entry per ecosystem and directory, in the order of the configuration file, each with a weekly schedule, a cooldown of seven days and one group for minor and patch updates. The entry of `github-actions` ignores `CrankyAnt/SharedWorkflows`, which the callers follow at `v1`. When the check fails, the job summary holds the expected content of the file.

A finding fails a pull request. On a pull request whose author is `dependabot[bot]`, and on a push, a finding is a notice in the job summary and the check passes.

A workflow that fails the local-workflows check follows the decision rule in the [catalogue](../../../catalogue.json): a shared part, the free job with a request, or a registered local workflow.

## A corrected title

`ci.yml` does not start on `edited`, so correcting a pull request's title or body starts no run. The title check and the reference check read the current title and body from GitHub: after a correction, re-run the failed job.

## Default-branch ruleset

The one check that the ruleset on `main` requires is `ci / verdict`. The [ruleset template](../../repository-ruleset/README.md) holds it; drop each other check of the template whose caller the repository does not hold.

## When a repository adopted it earlier

A change to a template reaches a repository through a pull request that replaces the installed caller with the current template:

| Template | Install target |
| --- | --- |
| [ci.yml](ci.yml) | `.github/workflows/ci.yml` |
| [automation.yml](automation.yml) | `.github/workflows/automation.yml` |
| [release.yml](release.yml) | `.github/workflows/release.yml` |

Nothing of the installed caller carries over. After the move of `v1` that changes a template, a caller that equals the previous version of that template passes for one month, with a notice in the job summary. After that month a pull request fails on it, with a link to the repository's open pull request that brings the fixed callers up to date; a pull request of `dependabot[bot]` still passes with a notice.

## When `v1` moves

A move of `v1` changes the later runs of the callers without a change in the repository. When it changes a template, the previous section applies. A failure that the repository cannot fix in its own files is a fault of the shared part: report it to the user.
