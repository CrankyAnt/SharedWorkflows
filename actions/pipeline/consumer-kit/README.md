# Pipeline

The consumer kit of the pipeline, a shared part of the [catalogue](../../../catalogue.json). A repository holds three fixed callers and the CI guide `docs/agents/ci.md`, copied from this kit, and one configuration file, `.github/ci-config.yml`. The callers hold no choice of the repository: every choice is in the configuration file.

## The fixed callers

Copy each template to its install target without changing it. A fixed caller equals its template byte for byte in every repository, so it holds no placeholder; the default branch is `main`.

| Template | Install target | Starts on | Calls |
| --- | --- | --- | --- |
| [ci.yml](ci.yml) | `.github/workflows/ci.yml` | pull requests, pushes to `main` and the dispatch `dependabot-merged` | `CrankyAnt/SharedWorkflows/.github/workflows/pipeline.yml@v1` |
| [automation.yml](automation.yml) | `.github/workflows/automation.yml` | a completed `CI` run, a weekly schedule, a start by hand with the part to start, and an opened issue | `CrankyAnt/SharedWorkflows/.github/workflows/automation.yml@v1` |
| [release.yml](release.yml) | `.github/workflows/release.yml` | a pushed tag and a start by hand | `CrankyAnt/SharedWorkflows/.github/workflows/release.yml@v1` |

Each caller sets `permissions: {}` at the top, holds one job named after its file and runs no step of its own.

- `ci.yml` is the read caller. Its job `ci` holds `actions: read`, `contents: read`, `issues: read` and `pull-requests: read` and passes no secret. The pipeline's verdict reports as the check `ci / verdict`. The part `dependabot-auto-merge` merges with the workflow's own token, and such a merge starts no push run, so it sends a `repository_dispatch` of type `dependabot-merged` with the merged commit: on it the pipeline runs for that commit as for a push of it, after it checked that the commit is on the default branch.
- `automation.yml` is the write caller. Its job holds `actions: read`, `contents: write`, `issues: write` and `pull-requests: write` and passes the secret `PUBLICATION_CHECKS_APP_PRIVATE_KEY` under its own name. [The parts of the automation workflow](#the-parts-of-the-automation-workflow) names what it runs.
- `release.yml` is the release caller. Its job holds `attestations: write`, `contents: write` and `id-token: write` and passes no secret. Without release configuration it publishes nothing, as [The release part](#the-release-part) says.

No caller starts on `pull_request_target`, and none inherits secrets.

## The CI guide

Copy [docs/agents/ci.md](docs/agents/ci.md) to `docs/agents/ci.md`. Its fixed part holds the pointer to the catalogue and the schema of the configuration file at `v1`, and the CI principles. The principles apply to the script of the free job and the registered local workflows. The fixed part ends with the heading `## Repository choices`. Up to and including that heading it is equal byte for byte in every repository, as a fixed caller is. Below the heading the repository records, each with its reason, the choices that no file states. The ruleset and the merge settings are no choice of the repository: the sync keeps both equal to their templates, as [Default-branch ruleset](#default-branch-ruleset) says. A choice that a shared part reads is a value in the configuration file, and work that no shared part covers follows the decision rule in the catalogue; neither goes below the heading.

## The configuration file

`.github/ci-config.yml` matches the schema [ci-config.schema.json](../schema/v1/ci-config.schema.json), which describes every key. The smallest file is the line `version: 1`. It holds the shared parts that the repository switches on under `parts`, its Dependabot ecosystems under `dependabot` and its local workflow register under `local-workflows`.

## Repository hygiene

The part `repository-hygiene` runs in every run of the pipeline and cannot be switched off. The configuration file may state it under `parts` with two optional settings, for files that its Dependabot coverage check and its link check leave out:

```yaml
version: 1
parts:
  repository-hygiene:
    coverage-exclude:
      - tests/fixtures/package.json
    link-exclude:
      - vendor/**/*.md
```

- `coverage-exclude`: the package files that are test fixtures, which nothing installs, or folders of such files, as paths relative to the repository root. The coverage check leaves out a package file at or under a listed path, so it needs no entry under `dependabot`, and the generated `.github/dependabot.yml` holds none for it.
- `link-exclude`: globs, relative to the repository root, of Markdown files whose links the repository does not maintain, such as vendored documentation. The link check checks no link in a matching file, and still checks a link from another file into one.

Each setting is a list of at least one string. Without the part, or with `repository-hygiene: {}`, both checks read every file. The hygiene job reads the two settings from the configuration file itself; when the file cannot be used, both checks leave no file out and the plan reports the file. A change to `coverage-exclude` starts the coverage check, and a change to `link-exclude` starts the link check.

## The parts of the automation workflow

The write caller runs four parts, each only when the configuration file states it under `parts`:

| Part | Values | Runs on | What it does |
| --- | --- | --- | --- |
| `dependabot-auto-merge` | `suites`, optional: the suite list's path | a completed pull-request run of `CI` for a Dependabot pull request | merges the pull request when its updates qualify and its required checks have passed, as the kit of [Dependabot auto-merge](../../dependabot-auto-merge/consumer-kit/README.md) says |
| `issue-title` | `categories`: the path of the repository's category file | an opened issue | gives the issue the title prefix of its category, as [Issue titles](#issue-titles) says |
| `additions` | `suites`: the suite list's path | a failed pull-request run of `CI` from a branch of the repository; a start by hand | commits the recorded additions of the run onto the pull request's branch, as [Additions](../../suite-selection/consumer-kit/README.md#additions) of the suite selection says |
| `full-recording` | `suites`; `validation-workflow`: the path of the workflow that runs the suites on a pull request | every second week on the schedule; a start by hand | records every entry file and proposes the changed lists as a pull request that merges itself, as [The full recording](../../suite-selection/consumer-kit/README.md#the-full-recording) of the suite selection says |

Every trigger of the caller starts a run. A job without work on the event, and every job of a part that is switched off, is skipped without a runner; on an event that no part has work on, nothing runs at all. The configuration file that decides is the one on the default branch for a completed run, the schedule and an issue, so a pull request cannot switch a part on for itself.

A start by hand names the part: `gh workflow run automation.yml -f part=full-recording`, with `-f verify-token=true` to check the App's key, or `gh workflow run automation.yml -f part=additions -f run-id=<run id>`.

`additions` and `full-recording` commit and propose as a GitHub App: the repository holds the App's Client ID in the variable `PUBLICATION_CHECKS_APP_CLIENT_ID` and its private key in the secret that the caller passes, as the [repository settings](../../suite-selection/consumer-kit/README.md#repository-settings) of the suite selection state.

### Issue titles

`issue-title` reads the repository's category file, a JSON object with two lists. The values here are an invented example:

```json
{
  "product_categories": [
    {
      "key": "widget",
      "source_directory": "widgets",
      "marker": "WIDGET.md",
      "title_prefix": "[widget]",
      "dynamic_title_prefix": "[widget: {component}]",
      "component_label": "Widget",
      "required_metadata": ["name"],
      "labels": ["area:widget"]
    }
  ],
  "generic_forms": [
    {"key": "question", "title_prefix": "[question]", "labels": ["type:question"]}
  ]
}
```

A category of `product_categories` has components: each direct subfolder of `source_directory` that holds the file `marker`. A marker starts with `key: value` lines between two `---` lines and holds each key of `required_metadata`; its `name`, when present, equals the folder's name, and a required `entrypoint` names a file in the folder. A category of `generic_forms` has no components. The part ignores every other key of the file, so the file may also serve the repository's own tools.

The category of an opened issue is the one category whose label the issue carries, otherwise the category whose `component_label` heads a filled section of the body, as an issue form writes it. The title becomes the category's prefix and then the title without an earlier prefix:

- a category without components gives its `title_prefix`;
- a section that names a component gives `dynamic_title_prefix` with that component, spelled as its folder is;
- `Not sure`, a component the repository does not hold, or an empty section gives the prefix with `unsure`;
- without a component section, a title that already starts with the prefix of a current component keeps it;
- an issue without a category keeps its title.

The job changes the title of the opened issue and nothing else. It fails, with the reason in its log, on a category file or a marker that it cannot read.

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
| `notes` | Optional. A tracked file whose content becomes the release notes, such as `docs/release-notes.md`. It is read at the commit that the tag names, so `script` cannot write it, and it holds UTF-8 text of at most 125000 characters. Without it, the release notes are GitHub's generated list of changes, filtered as below. |

A release tag is an annotated tag with an SSH signature, made with `git tag -s` and `gpg.format` set to `ssh`. A matching tag that is not annotated, carries no signature or carries one of a key that the file on `main` does not hold publishes nothing and fails the run, with the reason in the job summary of the job `plan`.

For a tag that passes, the files are collected at the commit that the tag names, their build provenance is attested in a repository that is not private, and the release is created under the tag with the files. A tag that already has a release fails the run and changes nothing, and so does a tag that was moved or removed after its signature was verified: the release is created only while the tag still names the verified commit. Without the part in the configuration file, a pushed tag publishes nothing and the run passes.

Without `notes`, the body of the release is the list of merged pull requests that GitHub generates for the tag at the verified commit, filtered by the part:

- The list starts at the previous release: the nearest earlier tag that the released commit reaches, that matches a pattern of `tags` and that has a published release. Without such a tag the list is the one GitHub gives for a tag without a previous tag, which for a first release is the whole history. A commit pushed straight to `main` has no pull request and no line.
- A line stays when its title has the type `feat`, `fix`, `perf` or `revert`, marks a breaking change with `!` before the colon, or starts with no Conventional Commits type, such as `deps: ...`. A line goes when its title has the type `docs`, `style`, `refactor`, `test`, `build`, `ci` or `chore` without `!`, in any case and with or without a scope, and when every author of it is a bot, a login that ends in `[bot]`.
- A line keeps "by" only for its external contributors: authors that are not the owner of the repository, not a bot and not `Copilot`. In a repository of an organisation every person is an external contributor. "New Contributors" keeps the lines of external contributors and goes when none stays.
- With a `.github/release.yml` of the repository, the same rules hold for the lines under each category heading, and a heading without lines goes.
- When no change line stays, the body is "No user-facing changes are listed for this release." The line "Full Changelog" stays as the last line.
- A body longer than 125000 characters loses change lines from the end of the list until it fits, and says so above "Full Changelog".

A request for the list, for the previous release or for the owner of the repository that fails, or a list without a body, fails the run before the release is created, with the step that failed in the job summary.

## What repository hygiene checks in these files

Repository hygiene runs in every run of the pipeline and checks the files of this kit on every event:

| Check | Fails when |
| --- | --- |
| Fixed callers | a fixed caller is missing or differs from its template |
| CI guide | `docs/agents/ci.md` is missing or differs from its template up to and including the heading `## Repository choices` |
| Local workflows | a workflow file under `.github/workflows/` is no fixed caller and has no entry under `local-workflows` |
| Register | a temporary entry names a `request` that is no issue of the repository, or a permanent entry names a `declined-request` that is no closed issue of the repository; a pull request is no issue; a temporary entry names a `request` that was closed as not planned, so the entry becomes permanent; `parts.custom` names `request` or `waits-for` without the other, or a `request` that is no issue of the repository or was closed as not planned |
| Switch-over | the part that a temporary entry, or the free job under `parts.custom`, names under `waits-for` exists at `v1`, or for `<part>.<setting>` the part knows that setting there: switch the part on under `parts` or state the setting, remove the workflow with its entry, and close the request |
| Dependabot configuration | `.github/dependabot.yml` differs from the file generated from `dependabot`, or exists while the configuration file states no ecosystems |

The generated `.github/dependabot.yml` holds one `updates` entry per ecosystem and directory, in the order of the configuration file, each with a weekly schedule, a cooldown of seven days and one group for minor and patch updates. The entry of `github-actions` ignores `CrankyAnt/SharedWorkflows`, which the callers follow at `v1`. When the check fails, the job summary holds the expected content of the file.

A finding fails a pull request. On a pull request whose author is `dependabot[bot]`, and on a push, a finding is a notice in the job summary and the check passes.

A workflow that fails the local-workflows check follows the decision rule in the [catalogue](../../../catalogue.json): a shared part, the free job with a request, or a registered local workflow.

## Requests for shared CI

A request asks for a shared part, for an input that an existing part lacks, or for the repair of a fault of a shared part, as steps 2 to 4 of the decision rule in the [catalogue](../../../catalogue.json) say. It is an issue in the repository itself with the label `shared-ci`; an agent in the repository writes nothing into `CrankyAnt/SharedWorkflows`. A request for a new part proposes the part's name, `<ecosystem>-<phase>`, where the ecosystem is a language or the platform that the part serves, as in `github-release`. A temporary entry of the register names the part under `waits-for`, and so does the free job that carries the work meanwhile: `request` and `waits-for` under `parts.custom` hold the number of the request and the name of the part. A request for an input names the existing part and proposes the name of the setting, and its temporary entry names both as `<part>.<setting>`.

The repository's own triage leaves a request alone. The repository creates the label `shared-ci` and adds this vocabulary entry to its `docs/agents/triage-labels.md`:

```markdown
## Shared CI vocabulary

This label belongs only to requests for shared CI, which the triage of Shared Workflows collects and answers. Such issues are not triaged here and never carry a Type, Triage, Ready, Status or Resolution label; triage discovery skips them as another workflow's vocabulary.

| Default label | Colour | Meaning |
| --- | --- | --- |
| `shared-ci` | `bfd4f2` | Request for shared CI: a shared part, a missing input or a shared part's fault |
```

The triage of Shared Workflows collects the open requests and answers each in a comment on the request. For a request that it takes up, the comment links a public issue in general terms and states the final name of the part, and of the setting for a missing input: keep `waits-for` equal to them. A request that it declines it closes as not planned, with the decline in the comment. When the part exists at `v1`, the switch-over check fails the next pull request: the agent that switches the part on removes the temporary workflow with its entry and closes the request. A workflow stays in the repository for good only with a permanent entry, which names a closed request that was declined.

## A corrected title

`ci.yml` does not start on `edited`, so correcting a pull request's title or body starts no run. The title check and the reference check read the current title and body from GitHub: after a correction, re-run the failed job.

## The sync App

The sync keeps the fixed files of a repository equal to their templates: it opens a pull request with auto-merge when a fixed caller, the CI guide or the generated Dependabot configuration differs. It reaches a repository through the installation of the sync App, a GitHub App that is installed on selected repositories, one by one.

Only the owner adds a repository to the sync App. An agent in a repository does not install it and asks the user instead. A repository outside the installation receives no sync updates: no sync pull request arrives after `v1` moves, its fixed files stay as they are until someone brings them in line by hand as [When a repository adopted it earlier](#when-a-repository-adopted-it-earlier) says, and the hygiene job of its pipeline fails on a fixed file that differs from its template.

## Default-branch ruleset

The one check that the ruleset on `main` requires is `ci / verdict`. The [ruleset template](../../repository-ruleset/README.md) holds it with three checks of callers that a repository with the fixed callers no longer holds; drop those three. The sync compares the ruleset with the ruleset template holding `ci / verdict` as its one required check, and the merge settings with those that the [consumer kit of repository hygiene](../../repository-hygiene/consumer-kit/README.md#merge-settings) states. A difference in either is no choice of the repository and is not kept: the sync reports it on every run, and its local mode replaces it with the template's value.

## When a repository adopted it earlier

A change to a template reaches a repository through a pull request that replaces the installed caller with the current template:

| Template | Install target |
| --- | --- |
| [ci.yml](ci.yml) | `.github/workflows/ci.yml` |
| [automation.yml](automation.yml) | `.github/workflows/automation.yml` |
| [release.yml](release.yml) | `.github/workflows/release.yml` |

Nothing of the installed caller carries over. The CI guide follows its template, [docs/agents/ci.md](docs/agents/ci.md), the same way, up to and including its heading `## Repository choices`; what the repository recorded below that heading carries over. After the move of `v1` that changes a template, a caller or a CI guide that equals the previous version of that template passes for one month, with a notice in the job summary. After that month a pull request fails on it, with a link to the repository's open pull request that brings the fixed callers up to date; a pull request of `dependabot[bot]` still passes with a notice.

## When `v1` moves

A move of `v1` changes the later runs of the callers without a change in the repository. When it changes a template, the previous section applies. When a check of repository hygiene that the move adds fails on what the repository already holds, fix it in the same pull request: repair a dead link, or name Markdown that the repository does not maintain under `link-exclude`; add the ecosystem and directory of an uncovered package file under `dependabot`, or name a test fixture under `coverage-exclude`, as [Repository hygiene](#repository-hygiene) says. A failure that the repository cannot fix in its own files is a fault of the shared part: report it to the user and file a request, as [Requests for shared CI](#requests-for-shared-ci) says.
