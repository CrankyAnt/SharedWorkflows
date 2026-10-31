# Maintaining Shared Workflows

Shared Workflows serves every repository that calls the reusable workflows of `CrankyAnt/SharedWorkflows` at the tag `v1`. It has six parts. Each is a subfolder of `actions/` with a composite action `$/actions/<part>`. A shared workflow loads each part except the Python language step through that action, in the form that [Same-repository references](#same-repository-references) states. The action of the recording step and of the Python language step runs commands; the action of every other part is resource-only. A consumer's job calls the recording step and the Python language step at `v1`, and the consumer's full recording also calls `suite-selection` and `repository-hygiene` at `v1`. The parts share the `v1` channel, the pins file and the promotion of `v1`.

An agent in a consumer starts from the [catalogue](../catalogue.json), read at `v1`: one entry per shared part with its purpose, its shared workflows and shared steps, the checks a consumer requires, what it needs from a repository, when it applies and its consumer kit. A part's consumer kit holds its caller templates and their explanation for a consumer. This document is for changing the shared parts themselves: what the workflows do and how they are maintained.

| Part | Folder | Shared workflows | Entrypoint | Suite |
| --- | --- | --- | --- | --- |
| Publication checks | `publication-checks/` | `repository-hygiene.yml` runs its convention checks; `update-publication-spec.yml` | `publication-checks/update_spec.py` | `publication-checks` |
| Repository hygiene | `repository-hygiene/` | `repository-hygiene.yml`, `update-ci-pins.yml`, `promote-v1.yml`; a consumer's full recording calls `repository-hygiene/action.yml` | `repository-hygiene/hygiene.py` | `hygiene` |
| Dependabot auto-merge | `dependabot-auto-merge/` | `dependabot-auto-merge.yml` | `dependabot-auto-merge/auto_merge.py` | `dependabot-auto-merge` |
| Suite selection | `suite-selection/` | `suite-selection.yml`, `suite-verdict.yml`; `repository-hygiene.yml` loads it for the push lookup; a consumer's full recording calls `suite-selection/action.yml` | `suite-selection/suite_selection.py` | `suite-selection` |
| Recording step | `recording/` | `suite-selection.yml` loads it for the digest; a job that runs suites calls `recording/action.yml` | `recording/recording.py` | `suite-selection`, `windows`, `etw-consumer-build` |
| Python language step | `python-language-step/` | none; a job that installs Python calls `python-language-step/action.yml` | `python-language-step/python_language_step.py` | `python-language-step` |

The workflows live under `.github/workflows/`. Each part's suite is a suite of `tests/suites.json` that `validate.yml` runs when a change reaches its stored path list; the suite `ci-policy` holds the checks that span the parts. The part folders hold code and data only; this document describes them. `tests/README.md` describes this repository's own suites and how they run.

## Principles

Every change to the shared CI holds to these principles; how a change is built is left to the agent.

- Every check has a purpose in the repository as it is now: it has been seen red on the condition it guards and stays green otherwise; an obsolete, flaky or fully covered check goes.
- A check runs only on changes that can change its result, judged by what it actually reads; it runs on every change only when every change can, as for the secret scan.
- A change to prose that no code reads starts only light checks; a document that code reads as data starts that code's checks.
- Each suite has one subject and every test a place in a suite; the entry file is the smallest unit of selection.
- A job on another platform runs only where its result can differ there.
- What is meaningful for repositories in general belongs in the shared workflows, and nothing a caller binds to changes unnoticed.
- Documentation follows what is built; it never decides the design.

## What the workflows do

### Calling the workflows

A consumer calls a shared workflow from a caller under its own `.github/workflows/`, at `CrankyAnt/SharedWorkflows/.github/workflows/<workflow>.yml@v1`. The caller sets `permissions: {}` at the top and its rights on its job, passes no secrets and runs no step of its own. A required check for a job in a reusable workflow is named `<caller job> / <shared job>` and matches by that exact string. The caller templates of the consumer kits give every consumer the same names: `hygiene / hygiene`, `dependabot-auto-merge / auto-merge` with `dependabot-auto-merge / merge` and, with the suite selection, `validation / validation`, the verdict's check. A consumer's default-branch ruleset requires `hygiene / hygiene` and the verdict's check. A validation job in the validation caller takes the validation workflow's file name without `.yml` as its job id, so its check is `<file name> / <job>`.

This repository calls every shared workflow it offers by local path, with the inputs that a consumer passes through `v1`, so every commit that `v1` can move to has run them on `main`. `repository-hygiene-caller.yml` calls `repository-hygiene.yml` and `dependabot-auto-merge-caller.yml` calls `dependabot-auto-merge.yml`, each reusing the shared job id as its own, so their checks are `hygiene / hygiene`, `auto-merge / auto-merge` and `auto-merge / merge`. Its validation workflow `validate.yml` calls `suite-selection.yml` as its job `select` and `suite-verdict.yml` as its job `validation`, whose check `validation / validation` is the verdict's check. [Job layout](#job-layout) gives its jobs.

The tag `v1` moves only as [The v1 channel](#the-v1-channel) says. Callers keep `@v1` and ignore `CrankyAnt/SharedWorkflows` in Dependabot.

### Same-repository references

A `uses:` value that starts with `$/` is GitHub's self-repository form: it resolves to the running workflow's own repository at the commit that is running, without a checkout, wherever `./` works, reusable workflow calls included. It needs runner 2.336.0 or newer ([GitHub changelog, 2026-07-30](https://github.blog/changelog/2026-07-30-reference-same-repository-actions-with-self-repository-syntax/)). Every same-repository `uses:` of the shared CI takes `$/`, and none takes `./`:

| Place | Form |
| --- | --- |
| The parts that the shared workflows load | `$/actions/<part>` |
| The shared workflows that this repository's callers and `validate.yml` call | `$/.github/workflows/<workflow>.yml` |
| The validation workflows that the validation caller template calls | `$/.github/workflows/<validation-workflow>.yml` |

zizmor's `self-repository` audit reports `uses: ./...` and recommends `$/`, so no workflow here carries a `# zizmor: ignore[self-repository]` comment. actionlint 1.7.12, the pinned version, does not know `$/` and reports such a reference as malformed ([rhysd/actionlint#711](https://github.com/rhysd/actionlint/issues/711)). The shared hygiene check therefore ignores exactly two actionlint diagnostics for `$/`, passed as `-ignore` flags so that they apply in every consumer without an actionlint configuration file: `ACTIONLINT_IGNORE` for an action and `ACTIONLINT_WORKFLOW_IGNORE` for a reusable workflow call, both in [hygiene.py](../actions/repository-hygiene/hygiene.py). A consumer's own `.github/actionlint.yml` or `.yaml` stays in use. The exception falls away once the pinned actionlint supports `$/`: that pin update removes both patterns. The same check ignores one more diagnostic, `ACTIONLINT_LABEL_IGNORE` in [hygiene.py](../actions/repository-hygiene/hygiene.py): exactly `label "ubuntu-26.04" is unknown`, because actionlint 1.7.12 does not know the runner label `ubuntu-26.04` that GitHub runs ([rhysd/actionlint#682](https://github.com/rhysd/actionlint/issues/682)). That exception falls away once the pinned actionlint knows the label: that pin update removes the pattern.

### Job layout

`validate.yml` runs these jobs. Each Linux job runs on the pinned label `ubuntu-26.04`, except that a job off the critical path that needs only the system `python3`, `git`, `gh` and pinned binaries runs on `ubuntu-slim`:

| Job | Runner | What it does |
| --- | --- | --- |
| `select` | `ubuntu-26.04` | Calls `suite-selection.yml` with `suites: tests/suites.json`, `language-steps: python`, the runner labels and the setup versions. On a push and a `dependabot-merged` run it starts with the [push lookup](#push-lookup-and-reuse). |
| `linux` | `ubuntu-26.04` | One shard per item of the `shards` output, named `shard <number>`. It runs the setups of its entry files under `exec --setup`, then each entry file with its suite's command under `exec --entry`, two at a time, each as its own traced process, then the recording step's `check`. The first shard then runs the [Python language step](#python-language-step) outside the recording. |
| `windows` | `windows-2025` | One job without shards, only when the `windows` output is not empty. It runs its setups and then every selected Windows entry file under ETW, one after another, then `check`. |
| `validation` | `ubuntu-slim` | Calls `suite-verdict.yml` with the `needs` context of every other job, also when one failed or was skipped, under `if: always() && !(needs.select.result == 'success' && needs.select.outputs.identical == 'true')`. |

The recording step is the first step after the checkout, so that it writes the job's environment before any setup. Each setup installs into a location of its own, `$RUNNER_TEMP/setups/<setup>` or the tool cache for a `uses:` setup: `actions/setup-python` runs with `update-environment: false` and its `provide` names its interpreter with `--python`, which installs the [lookup report](#module-lookups); and `actions/setup-dotnet` installs into `DOTNET_INSTALL_DIR`. A `uses:` setup can still change the job's environment, as `actions/setup-node` and `actions/setup-dotnet` do; no entry file sees that change. A setup that a `uses:` action performs runs outside the recording; its repository reads are extra reads of the suites and entry files that state the setup.

After a setup that gives the entry files something, `provide` records it. Each entry file runs in the job's environment before its setups, with only what its stated setups provide: their tool directories on `PATH`, `DOTNET_ROOT` for .NET and `PUBLICATION_CHECKS_TOOLS` for commitlint. A variable that an entry file needs and no setup provides belongs in the job's `env`. What a test writes outside what its setups provide, such as `$HOME`, the shared checkout or per-user state on Windows, is no input of selection; a result that depends on such a write is a flaky result.

The recording step runs on the job's own `python3`, on Windows `python`. It runs `strace`, on Windows `dotnet`, as the job's environment before its setups finds them. A recording job restores no cache that holds build or test results and turns off build servers and node reuse: the ETW consumer's build in the `windows` job runs `dotnet publish` with `--disable-build-servers`, `-nodeReuse:false` and `-p:UseSharedCompilation=false`.

`repository-hygiene-caller.yml` runs the [hygiene job](#repository-hygiene) beside `validate.yml` in its own workflow, so a title edit does not start the suites again and a floor failure does not skip them.

What each event runs in `validate.yml` and `repository-hygiene-caller.yml`:

| Event | Jobs |
| --- | --- |
| A pull request | `select`, the selected shards and the Windows job side by side, then `validation`; the hygiene job beside them. A later head reuses the passed results whose key did not change. |
| An `edited` pull request | the hygiene job only; it runs the convention checks and Gitleaks again and reuses the other passed checks of the same tested tree. |
| A push to `main` whose tree equals the tested tree | `select` and the hygiene job; no suite, no check but Gitleaks and no verdict runs again. |
| A push whose pull request was behind | the suites and hygiene checks whose reads meet both the pull request's changed files and the files changed on `main` between the tested base and the landing base. |
| A push without a passed tested tree | the suites and checks that its own changed files select. |
| A `dependabot-merged` dispatch | as a push of `client_payload.sha`. |

`suite-selection.yml` runs `select` with `--compare direct` between `github.event.before` and `github.sha` on any other event.

On `pull_request` events, a run of `validate.yml` is cancelled when the workflow starts for a newer head of the same pull request; the hygiene job lets an `edited` run wait instead, as [Repository hygiene](#repository-hygiene) says. Push and `dependabot-merged` runs are never cancelled, and a cancelled run stores no result.

### The `dependabot-merged` event

`validate.yml` and `repository-hygiene-caller.yml` of this repository, and the hygiene and validation caller templates of the consumer kits, also run on a `repository_dispatch` event of type `dependabot-merged`, whose `client_payload.sha` names a merged commit on the default branch; the shared `repository-hygiene.yml` and `suite-selection.yml` handle it for their callers. On that event each checkout takes `client_payload.sha`, and each comparison runs from that commit's first parent to the commit, as a push run compares the previous head with the pushed commit; it selects the same suites and checks that a push of that commit would. On an event without `client_payload.sha`, the checkout and the push lookup take the default branch's tip, `github.sha`, and the run selects as for a push of that commit. A `repository_dispatch` run runs the workflow from the default branch. The `merge` job of [Dependabot auto-merge](#dependabot-auto-merge) sends the event after each merge it makes.

### Publication checks

The job of `repository-hygiene.yml` runs the convention checks on pull requests only, including `edited` events, with the scripts of `publication-checks/`; with Gitleaks they are the floor of the [hygiene job](#repository-hygiene). No separate publication-checks workflow or caller exists. It checks the branch name, every non-merge commit in the base-to-head range and the pull request title against these rules, from the same revision as the workflow:

- Branch names follow a subset of Conventional Branch: short prefixes, dots only in release versions, no agent prefixes, and the `research/` exception. GitHub's revert branches `revert-<number>-<branch>` are accepted. The vendored official regex of [spec.json](../actions/publication-checks/spec.json) is an additional check.
- Commit subjects and pull request titles follow Conventional Commits as [commitlint.config.cjs](../actions/publication-checks/commitlint.config.cjs) states; the title step turns on `header-no-issue-number`, the rule for titles only. Merge commits are skipped by parent count; a merge-looking message on an ordinary commit grants no exception. GitHub's revert subjects such as `Revert "feat: add a module"` are accepted.
- A pull request authored by exactly `dependabot[bot]` or `renovate[bot]` skips all three checks with a visible message per check, also on events that a human triggers. Authorship is the deciding identity when a human edits or pushes to that pull request.

The job sets up the pinned Node.js and installs the locked commitlint toolchain from the part's `package.json` and `package-lock.json` in runner temporary storage with `npm ci --ignore-scripts`; a consumer's `.npmrc` does not configure that installation. Its Node.js version comes from the pins file through `$/actions/repository-hygiene`. The specification is read from [spec.json](../actions/publication-checks/spec.json) and [spec-metadata.json](../actions/publication-checks/spec-metadata.json), which records its version, SHA-256 and versioned source; no run requests it. The specification is CC-BY-4.0; its attribution and source stay in the JSON and the metadata.

### Repository hygiene

`repository-hygiene.yml` runs one job, `hygiene`, on `ubuntu-26.04`, whose check in a caller is `hygiene / hygiene`. The job runs the floor, the [convention checks](#publication-checks) on pull requests and Gitleaks on every event, and the selected shared checks below. It holds `contents: read`, and `actions: read` with `pull-requests: read` for the [push lookup](#push-lookup-and-reuse), checks out the full history without persisted credentials and uses no secrets. A newer head of a pull request cancels the job of the older one; an `edited` run waits for it instead, and push and `dependabot-merged` runs are never cancelled.

On a push and on a `dependabot-merged` run, the step after the checkout runs `suite_selection.py lookup`. An `identical` case runs no check but Gitleaks. A `behind` case runs only the checks that both the landing base to the pushed commit and the tested base to the landing base select. The other cases decide over the landing base to the pushed commit.

On an `edited` run, the convention checks and Gitleaks run again. Every other check that passed on the same tested tree in an earlier run of the pull request is reported in the summary and as a notice as passed in that run instead of running. The record is the cache entry `hygiene-results-<number>-run-<run id>-<attempt>` that each pull-request run that was not cancelled writes with `hygiene.py record`. A re-run reuses nothing.

The job's step `changes` reads the changed files of the event, between the previous head and the pushed commit, between the merge base and the head of a pull request, or between the first parent of a [`dependabot-merged`](#the-dependabot-merged-event) commit and that commit, and decides which checks the change can affect. The Dependabot coverage check and the link check need no tool and run next. The job then installs the pinned tools that the running checks need; a download whose sha256 differs from the pin stops the tool checks before any tool runs. Every running check runs, even after an earlier one failed, and the job fails when any check failed:

| Check | What runs |
| --- | --- |
| actionlint | `actionlint -shellcheck <pinned ShellCheck>`, with the two ignored diagnostics for `$/` and the one for the label `ubuntu-26.04` that [Same-repository references](#same-repository-references) states. A missing or broken ShellCheck fails the job instead of running actionlint without it. |
| zizmor | Offline, over workflows and action definitions, never Dependabot configuration. The repository's own `.github/zizmor.yml` or `zizmor.yml` (either with a `.yaml` extension) applies when present; otherwise the shared [zizmor policy](#zizmor-policy). |
| Gitleaks | `gitleaks git --log-opts=--all` over every reachable commit, with the caller's configuration and baseline when given. |
| Dependabot coverage | Every tracked package file that [package-ecosystems.json](../actions/repository-hygiene/package-ecosystems.json) recognises needs an `updates` entry for its ecosystem whose `directory` equals, or whose `directories` glob matches, the file's directory. Each uncovered file fails the job with its path and ecosystem. A repository without Dependabot configuration passes only when it has no recognised package file. |
| Relative links | [link_check.py](../actions/repository-hygiene/link_check.py): every relative link in tracked Markdown, inline links, images and reference definitions outside fenced code and inline code, points to an existing file or folder and, with an anchor into a Markdown file, to an existing heading in GitHub's anchor form or an HTML anchor. Links with a scheme and protocol-relative links are not checked. Each dead link fails the job with its file, line and target, as an annotation and in the step summary. `link-exclude` takes Markdown files out as sources; a link into them is still followed. |
| REUSE | `reuse --root . lint` with the pinned reuse. Each tracked file without a copyright and licence assignment fails the job, and the report of `reuse lint` names it. |

When each check runs:

| Check | Runs when the change touches |
| --- | --- |
| Gitleaks | anything: it runs on every event |
| actionlint | a workflow, the metadata of a local action that a workflow uses as `uses: ./…`, the actionlint configuration, or the scripts and pins that install and run it |
| zizmor | a workflow, an action definition, the zizmor configuration, or the scripts, pins and shared policy that install and run it |
| Dependabot coverage | a recognised package file or the Dependabot configuration, the `coverage-exclude` input of the shared workflow or of the caller, the `uses:` reference of the caller, or its scripts |
| Relative links | a Markdown file, a file that the change removes (a rename removes the old path), the `link-exclude` input of the shared workflow or of the caller, the `uses:` reference of the caller, or its scripts |
| REUSE | only in a repository with a `REUSE.toml`: an added, renamed or removed file, `REUSE.toml`, a file under `LICENSES/`, or the scripts and pins that install and run it |

actionlint loads a local action's metadata only for a `uses: ./…` reference. It does not load an action referenced with `$/`, so a changed local `action.yml` changes nothing that actionlint checks; zizmor still runs on it. A workflow change that touches only ordinary comments starts neither actionlint nor zizmor; a changed linter directive counts as a change. A push, the first push of a branch included, and a `dependabot-merged` event decide over the landing base of the push lookup, which is the commit's first parent when the push has no previous head. Every check runs when the changed files cannot be determined, or when the change touches one of the three selected parts of the part's resource action: `#step:resources`, `#key:outputs.path.value` and `#key:runs.using` of `action.yml`. The shared workflow names the inputs it reads as `PATH#SELECTOR` definitions (`PATH#step:ID`, `PATH#key:DOTTED.PATH` or `PATH#INPUT`). When the file of a `step:` or `key:` selector differs between the two revisions and the selector names nothing in either, the step fails with the option name; a `PATH#INPUT` selector that names nothing passes. A `step:` selector on a caller without steps of its own reads the caller's `uses:` reference of the shared workflow instead, when either revision has such a reference; without one, the step fails as above. The step summary gives each decision and its reason; `workflows` remains as the union of the actionlint and zizmor decisions.

Inputs, all optional: `gitleaks-config` and `gitleaks-baseline` (paths relative to the repository root), `zizmor-persona` (`regular`, `pedantic` or `auditor`), `coverage-exclude` (paths, one per line, whose package files are not checked) and `link-exclude` (globs relative to the repository root, one per line, of Markdown files whose links are not checked, such as vendored documentation; a directory pattern ends in `/`). An omitted input leaves the tool on its own default. [hygiene.py](../actions/repository-hygiene/hygiene.py) runs each check as a subcommand and the decision as its `changes` subcommand, and the workflow passes the inputs to it through `env`.

The [consumer kit](../actions/repository-hygiene/consumer-kit/README.md) of repository hygiene holds the caller template, `consumer-kit/repository-hygiene.yml`, and explains to a consumer what the job checks, the inputs and repository files it reads, the merge settings and the default-branch ruleset that the shared parts rely on, with a link to the [ruleset template](../actions/repository-ruleset/README.md), and how a caller fixes a check that a move of `v1` adds. A change to the job's checks, inputs, reads or settings, or to the ruleset template, changes that explanation in the same change; `tests/test_ci_policy_shared_surface.py` fails when the consumer kit no longer leads to the ruleset template.

### Dependabot auto-merge

`dependabot-auto-merge.yml` takes no inputs and no secrets, uses no third-party action, checks out nothing and runs no code from the pull request. The [consumer kit](../actions/dependabot-auto-merge/consumer-kit/README.md) of Dependabot auto-merge holds the caller template, `consumer-kit/dependabot-auto-merge.yml`, and the Dependabot configuration template, `consumer-kit/dependabot.yml`, and explains to a consumer which updates merge on their own, the repository settings the part needs and when a repository adopts it. A change to the rules, the events, the rights of the calling job or the settings below changes that explanation and the templates in the same change. The calling job grants `contents: write` and `pull-requests: write`; the caller runs on `pull_request` and on `workflow_run`, and zizmor's `dangerous-triggers` finding on `workflow_run` carries an inline ignore in the template.

A reusable workflow receives the token rights of the calling job and can only reduce them, and a run that Dependabot triggers starts with a read-only token, so the calling job grants the rights that the two jobs of the workflow need between them. The workflow declares `permissions: {}` and gives each job its own rights; both use the run's `GITHUB_TOKEN` and nothing else, and neither holds `actions: write`:

| Job | Runs for | Rights | What needs the write right |
| --- | --- | --- | --- |
| `auto-merge` | a `pull_request` event whose author is exactly `dependabot[bot]`, judged by `github.event.pull_request.user.login` | `contents: write`, `pull-requests: write` | disabling an auto-merge that the pull request still has |
| `merge` | a `workflow_run` event of a `pull_request` run whose actor is exactly `dependabot[bot]`, or exactly the updater App `crankyant-publication-checks[bot]` on a head branch that starts with `dependabot/`, judged by `github.event.workflow_run.actor.login` and `github.event.workflow_run.head_branch` | `contents: write`, `pull-requests: read` | the merge (`PUT /repos/{owner}/{repo}/pulls/{number}/merge`) and the dispatch (`POST /repos/{owner}/{repo}/dispatches`) |

Every other event, author or actor skips the job by its job-level condition, without a runner; neither job judges by `github.actor`.

[auto_merge.py](../actions/dependabot-auto-merge/auto_merge.py) reads the pull request, its commits and the active rules of the base branch through `gh api`, and reads the update metadata from the pull request's first commit as `dependabot/fetch-metadata` v3.1.0 does:

- The pull request and its first commit are authored by `dependabot[bot]`, and that commit's signature is verified.
- The commit message holds the `updated-dependencies` block between a `---` line and a `...` line, and the head branch starts with `dependabot`. The block is a list of entries of `key: value` pairs, each with a non-empty `dependency-name`; a value in single or double quotes is read without them, and any other line in the block is an error.
- Each entry gives a dependency name and its `update-type`; the ecosystem is the second segment of the head branch, split at the character after `dependabot`. An entry without `update-type` takes the type derived from its previous and new version: a different major version is a major update, a different minor version or a version with fewer than two parts a minor update, any other difference a patch update, and a missing or unchanged version an empty type. The new version is the entry's `dependency-version`; otherwise both versions come from the message's ``Updates `<name>` from <old> to <new>`` line for that entry, and for the first entry from its `Bumps <name> from <old> to <new>.` line or the `Update <name> requirement from <old> to <new>` of its subject or of the pull request's title.

A failed check is an error. The decision is automatic only when all of these hold:

1. Every updated dependency has update type `version-update:semver-patch` or `version-update:semver-minor`. A major, empty or unknown type makes the pull request manual.
2. In the `github_actions` ecosystem, the owner segment of every dependency name is exactly `actions`. Any other owner makes the pull request manual; `someone/actions-tool` and `someone/actions` do not count.
3. Every commit on the pull request is authored by `dependabot[bot]`, or, in this repository only, is a commit by the updater App `crankyant-publication-checks[bot]` that changes only files under `actions/recording/etw-consumer/build/` and `tests/suites.json`, as the [rebuild workflow](#the-rebuild-workflow) of the ETW consumer makes it; a rename counts with its old and its new path. The decision reads the changed files of each such commit with `GET /repos/{owner}/{repo}/commits/{sha}`. Any other commit not by `dependabot[bot]`, another App's included, makes the pull request manual.
4. An active ruleset rule on the base branch requires at least one status check.

| Outcome | `auto-merge` job | `merge` job | Job result |
| --- | --- | --- | --- |
| Automatic (all four rules hold) | Nothing is enabled; auto-merge is disabled if it was enabled. | The squash merge is requested, then the dispatch. | Success, unless the merge or the dispatch fails |
| Manual (rule 1, 2 or 3 fails) | Auto-merge is disabled if it was enabled; the pull request is left to the user. | Nothing is merged or dispatched. | Success |
| Blocked (rules 1 to 3 hold, rule 4 fails) | Nothing merges; auto-merge is disabled if it was enabled. | Nothing is merged or dispatched. | Failure naming the missing ruleset |
| Error (the metadata is unverified or not in that form, or a read fails) | Auto-merge is disabled if it was enabled. | Nothing is merged or dispatched. | Failure with the reason |

Every run writes a summary that lists each updated dependency with its ecosystem and update type, and the decision with its reason. Running the `auto-merge` job again on an unchanged pull request gives the same result. That job never enables auto-merge: a merge that GitHub makes later for an auto-merge enabled with the run's `GITHUB_TOKEN` starts no `on: push` run, so the merged commit would go unchecked.

The `merge` job takes the head branch of the completed run and decides again for each open pull request of that branch by `dependabot[bot]`, on the pull request's current head commit; without such a pull request the run succeeds and merges nothing. On an automatic decision it requests a squash merge with `sha` set to the head commit that the decision read, so GitHub merges nothing once the head has moved; when the commits it read do not end at that head, it requests no merge. The run succeeds with the reason in its summary when GitHub refuses the merge because a required check is pending or failed (HTTP 405 naming a status check), because the head moved (HTTP 409), or because the pull request is already merged, as when two completed workflows reach the merge together. Any other failure of the merge or the dispatch fails the run, and the summary names the step and the response. After a merge it made itself, the run sends one `repository_dispatch` event of type `dependabot-merged` with `client_payload.sha` set to the merged commit; a `repository_dispatch` event caused by the run's token starts runs, and the push-time workflows run on it as [The `dependabot-merged` event](#the-dependabot-merged-event) says.

Rule 4 reads `GET /repos/{owner}/{repo}/rules/branches/{branch}`, which the run's token may read. Classic branch protection is not recognised: reading it needs an administration right the token does not have, so a repository that protects its default branch with classic branch protection alone is blocked until it has a ruleset. The auto-merge checks themselves need not be required: GitHub counts a skipped job as a success for required checks, and both jobs are skipped for every pull request that is not Dependabot's.

Limits:

- Dependabot security updates arrive without the cooldown that `dependabot.yml` sets for version updates. They follow the same rules as every other Dependabot pull request.
- In a repository whose required checks do not build or test the product, an automatic merge is checked only by those required checks. The trust decision is made when a dependency is added, not when it is updated.
- Merge queues, Dependabot alert lookup, and personal access tokens or App tokens for Dependabot runs are outside this workflow.

### Suite selection

The suite selection runs the validation suites of a repository only when a change can affect them. A repository states its suites in a suite list; the suite selection compares the changed files with each suite's stored path list and answers `run` or `skip` per suite, with the reason, as outputs and as a Markdown summary. The stored lists come from the [recording step](#recording-step); `select` reads them and the extra reads and never derives a list. A unit is a suite, or an entry file of a suite selected per entry file. `select` assigns the selected Linux entry files to as few shards as keep each shard's estimated traced test time on its two workers under 120 s; an entry file over the bound gets a shard of its own. It starts the Windows job only when it selects a Windows entry file, and reuses passed results whose key did not change. The suite selection gives the verdict of a validation run from the results of the selection and the jobs it gated. The suite selection depends on nothing specific to this repository: a repository passes its root, its suite list, and either its changed files or two commits.

#### The selection and verdict workflows

`suite-selection.yml` has one job, `select`, on `ubuntu-26.04`, with `contents: read`, and `actions: read` with `pull-requests: read` for the [push lookup](#push-lookup-and-reuse). Its inputs, all optional:

- `suites`: the suite list's path relative to the repository root, default `.github/suites.json`.
- `language-steps`: the language steps the caller runs in its first Linux shard, separated by spaces, such as `python`.
- `runner-labels`: the runner label of each system's job, as `<os>=<label>`, such as `linux=ubuntu-26.04 windows=windows-2025`; part of the reuse key.
- `setup-versions`: the version that a setup pins outside the recorded files, as `<setup>=<version>`, such as `python=3.13`; part of the reuse key.

The job checks out the caller's repository with its full history. On a push and on a [`dependabot-merged`](#the-dependabot-merged-event) run, its first step after the checkout is the push lookup, and `select` reads its case. On a pull request it restores the pull request's passed results and runs `select` between the merge base and the head with `--results`. It passes `github.workflow_ref`, the caller's workflow, so workflow and action reads use the [CI-definition](#the-ci-definition) rules. Its outputs:

- `suites`: one JSON object with `run` or `skip` for every suite of the suite list.
- `entries`: one JSON object with the selected entry files of every suite selected per entry file, empty when the list has none.
- `shards`: one JSON list with one object per Linux shard: its number, its estimate, its setups, and its entry files, each with its suite and that suite's command. Empty when nothing runs on Linux.
- `windows`: one such object for the Windows job, empty when no Windows entry file is selected. Its estimate is the sum of the duration estimates of its entry files, since the job runs them one after another, in the full recording as well.
- `language-steps`: one JSON object with `run` or `skip` for every language step named in the input.
- `units` and `reused`: on a pull request, the running and the reused units by reuse key.
- `shared-digest`: the digest of the shared recording step that `select` took, as [A move of v1 in a consumer](#a-move-of-v1-in-a-consumer) describes.
- `identical`: `true` on a push or `dependabot-merged` run whose tree equals a passed tested tree and whose shared digest did not move, so that it runs no suite; empty otherwise. `select` writes it last, so a step that fails leaves it unset. The verdict job of a caller does not run after a passed `select` with that output, and runs on every other run, also after a failed `select` or a failed or cancelled job.

Its job summary lists every suite once with its decision and reason.

`suite-verdict.yml` has one job, `validation`, on `ubuntu-slim`, with `contents: read` and no checkout. Its input `needs` is the `needs` context of the calling job as JSON, `${{ toJSON(needs) }}`. On a pull request that was not cancelled, the job first downloads the `suite-results-*` artifacts, writes the record with `store` and, when `store` reports `stored=true`, saves it to the pull request's cache. It then runs [`verdict`](#verdict) on that context and writes its summary.

A consumer calls both from its validation caller, `validation.yml`; the [consumer kit](../actions/suite-selection/consumer-kit/README.md) of the suite selection holds its template, the form of a validation workflow and the full recording, and explains them to a consumer. A change to the inputs and outputs of the two workflows, the suite list, the recording step's commands or the full recording changes that explanation in the same change. The caller passes `suites`, `entries`, `language-steps` and `shared-digest` to its validation workflows. A validation workflow runs or skips a suite selected as a whole by its `suites` result, and the step of a suite selected per entry file runs the entry files that `fromJSON(inputs.entries).<suite>` lists.

#### Suite list

JSON with the keys `always` and `suites`, and `module_lookups` once a recording stores a module lookup:

```json
{
  "always": [],
  "suites": [
    {
      "name": "unit",
      "entries": ["checks/test_*.py"],
      "command": ["python3", "-m", "unittest", "-v"],
      "setups": ["python"],
      "extra_paths": ["fixtures/**"]
    },
    {
      "name": "stored",
      "entries": ["checks/test_stored.py"],
      "command": ["python3", "-m", "unittest", "-v"],
      "setups": ["python"],
      "entry_durations": {"checks/test_stored.py": 4.2},
      "paths": ["checks/test_stored.py", "src/**", "config/settings.toml"]
    },
    {
      "name": "mod",
      "entries": ["checks/run.lua"],
      "command": ["lua"],
      "setups": ["lua"],
      "paths": ["checks/run.lua", "src/**/*.lua"]
    }
  ]
}
```

- `always`: repository-wide patterns whose changes can affect every suite. A repository lists only such reads; this repository's suite list has none.
- `name`: lowercase letters, digits and hyphens, unique in the suite list; it is the output key.
- `entries`: the suite's entry files, as repository paths or as glob patterns over the tracked files, in the pattern syntax below. A glob pattern stands for every tracked file it matches, in path order. An entry file or pattern that matches no tracked file makes the suite list invalid. An entry file is a file in any language, such as a test module, a script or a C# test project file.
- `os`: `linux`, the default, or `windows`. A Windows suite's entry files run in the Windows job, the others in the Linux shards.
- `command`: the program and its arguments that run one entry file, a non-empty list; the job appends the entry file's path. The shards and the Windows job carry it with each entry file.
- `setups`: the names of the setups that the suite needs, such as `python` or `node`. A job runs the setups of all its entry files before them, each under `exec --setup` unless it runs outside the recording, and each entry file sees only the setups it states.
- `entry_selection`: `true` for a suite selected per entry file. Each entry file then runs when a changed file matches its own list, and the suite's step runs only the selected entry files; the suite keeps its subject, its step and its gate. With `--github-output` the selection writes two more outputs for such a suite, `<suite>-entries` (the selected entry files, space-separated) and `<suite>-modules` (the selected Python entry files as module names), and the step runs them, for example `python -m unittest $MODULES` with `MODULES: ${{ steps.select.outputs.<suite>-modules }}` in its `env`. With `--json-output` it writes the one `entries` object, and a step that receives it as an input runs that suite's list, for example `python -m unittest $ENTRIES` with `ENTRIES: ${{ join(fromJSON(inputs.entries).<suite>, ' ') }}` in its `env`. A suite without the key is selected as a whole.
- `entry_setups`: with `entry_selection`, more setups per entry file, an object keyed by an entry file or by a file that a glob pattern of `entries` matches. An entry file needs the suite's setups and its own.
- `entry_durations`: the duration estimate of each entry file, in seconds of traced test time under the mid trace set on Linux and under the one ETW session on Windows, zero or more, keyed as `entry_setups` is. The full recording writes it. `select` counts an entry file without one at the mean of the stored estimates of its operating system, or at 0 s when the suite list stores none for it.
- `paths`: the suite's stored path list, the repository path patterns the suite reads. A suite without it runs on every change.
- `directory_memberships`: directories whose direct child membership the suite reads, or glob patterns over observed names. A trailing slash restricts a glob to directory names. A matching name added or removed selects the suite; changing content inside an existing child does not.
- `existence_reads`: objects with a repository `path` and `kind`, one of `file`, `directory` or `any`. They select when the named path appears or disappears as that kind, not on edits to existing file contents.
- `lookup_directories`: the checkout directories in which the suite looks up modules, as the [lookup report](#module-lookups) names them, the root as `.`. A recording writes them.
- `entry_paths`: with `entry_selection` and `paths`, the stored path list of each entry file alone, an object with one list per entry file. `paths`, `directory_memberships` and `existence_reads` of such a suite are the union of its entry files' lists.
- `entry_directory_memberships`: with `entry_selection`, the same membership metadata per entry file. It records which entry file reads a directory membership, separately from `entry_paths`, which records file-content reads.
- `entry_existence_reads`: with `entry_selection`, existence reads per entry file. Another entry file's existence read does not select this entry file.
- `entry_lookup_directories`: with `entry_selection`, the lookup directories per entry file.
- `extra_paths`: the extra reads of file content, as path patterns for reads no recording sees, each no wider than its read. They add to the suite's stored path list and never remove from it.
- `entry_extra_paths`: with `entry_selection`, extra reads per entry file, an object with a list for the entry files that need one, for a read no recording sees that only some entry files make. `extra_paths` apply to every entry file.
- `extra_directory_memberships`: glob patterns over repository paths whose membership the suite reads and no recording sees, such as the existence of a file whose path the suite takes from a data file, in the form of the glob patterns of `directory_memberships`; a pattern with `**` reaches paths at any depth. They add to the suite's stored directory memberships, and to those of every entry file of a suite selected per entry file, and never remove from them. A matching path added, removed or renamed selects the suite, and a content change does not. A recording never writes them. A value that is not a list, or an item that is not a non-empty string, makes the suite list invalid; the error names such an item by its position.
- `extra_existence_reads`: existence reads that no recording sees, as objects in the form of `existence_reads`, such as a path that a Windows test checks with `Test-Path`. They add to the suite's existence reads, and to those of every entry file of a suite selected per entry file, and never remove from them. A recording never writes them. A value that is not a list, or an item that is not an object with exactly a non-empty `path` and a `kind` of `file`, `directory` or `any`, makes the suite list invalid; the error names such an item by its position.
- `module_lookups`, a key of the suite list beside `suites`: per checkout directory, the module names that its entry files looked up there without finding them, outside the standard-library names. The entry files of the suite list share them; an entry file selects on the module lookups of each of its lookup directories. A value that is not an object of non-empty directories with lists of non-empty names makes the suite list invalid.

Any other key makes the suite list invalid. A suite with `paths` is selected on its stored lists with the `extra_paths`, `extra_directory_memberships` and `extra_existence_reads` added.

Patterns use GitHub path-filter glob syntax: `**` crosses directory separators, `*` and `?` do not. A file pattern is the file's path; a tree pattern is `<directory>/**`; `**` alone is the whole repository.

#### Selection and fallback

- Every suite runs when the changed files cannot be determined.
- With `--workflow`, changed parts of the [CI definition](#the-ci-definition) select their consuming suites and entry files.
- A change to an [unrecorded input](#unrecorded-inputs), such as the suite list's own fields, a part of a suite job or the recording step's code, selects the units it shapes.
- A changed file that matches an `always` pattern runs every suite.
- A change that adds, removes or changes a tracked `.gitignore` file runs every suite with every entry file, and so does a change that adds a path that the ignore rules of its head commit match, as `git add -f` does. With `--changed-file`, the ignore rules are the tracked `.gitignore` files of `HEAD`, every changed file that the index tracks counts as added, and a tracked `.gitignore` file whose staged or working-tree state differs from `HEAD` counts as changed.
- Otherwise a suite without a stored path list runs, and a suite with one runs when a changed file matches a pattern in it or in its `extra_paths`, changes a directory membership it reads, changes an existence read it makes, or adds a file or directory whose module name is a standard-library name or a stored module lookup of one of its lookup directories, in that directory. A suite selected per entry file runs the entry files whose own lists match.
- An added path's module name in a lookup directory is a file's name up to its first dot, or the name of a directory that the change adds; a path added in a directory that existed before counts only as its `__init__` file, which makes that directory a package. The standard-library names are the union of `sys.stdlib_module_names` over the Python versions that the shared CI supports, held in [stdlib_module_names.json](../actions/recording/stdlib_module_names.json), which [Renew the standard-library names](#renew-the-standard-library-names) renews. With `--changed-file` every changed file that exists counts as added.
- A glob directory membership selects on the same changes with a list of changed files as between two commits: an added, removed or renamed matching path, never a content change.
- A changed file that affects no content read, directory membership or existence read selects no suite. When every changed file is of that kind, no listed suite runs.
- An invalid suite list or a step gated on a suite that the suite list lacks fails `select`; it never broadens the selection. The caller decides whether the failed selection blocks the run.

With `--base` and `--head` the suite selection reads the changed files from Git. `--compare merge-base` is for a pull request: the files that differ between the merge base of the base and head commits and the head commit. `--compare direct` gives the files that differ between the two commits; `suite-selection.yml` uses it for any event other than a pull request, a push or a dispatch; a push and a dispatch go through `--lookup`. A renamed file counts under its old and its new path, a deleted file under its path. When the previous head is the all-zero commit, when either commit is absent from the checkout, or when the two commits have no merge base, the changed files cannot be determined.

#### The CI definition

With `--workflow`, the selector follows the calling workflow, reused workflows and local actions through their executable references. Repository paths in `run`, `with` and `working-directory` fields are attributed to their consuming gates. Local action paths resolve from `github.action_path` or an environment variable bound to it. A `run` path that starts with `..` resolves against the step's working directory: the step's `working-directory`, otherwise `defaults.run.working-directory` of the job, otherwise that of the workflow, otherwise the repository root. A working directory that names no repository path, such as `${{ runner.temp }}`, leaves such a path unresolved. Other `run` paths read from the repository root. `..` segments in any reference resolve after the action path is substituted, and a reference above the repository root names no file. Comments and shell assignment names are not reads. An unreferenced file in an action folder is not part of the definition. The reader reports an unreadable or malformed known definition as an error; that error does not become the changed-files-unknown fallback.

Each part carries its suite and entry-file gates. A gate is `<output>.<suite> == 'run'`, `fromJSON(<input or output>).<suite> == 'run'`, `contains(<output>.<suite>-entries, '<file>')`, `contains(<output>.<suite>-modules, '<module>')` or `contains(fromJSON(<input or output>).<suite>, '<file>')` on an input or output named `entries`, and several gates join only with `||`. A term `fromJSON(<input or output>).<language step> == 'run'` on an input or output named `language-steps` may join them as a control term and adds no suite. Any other condition with a selection term fails `select` with "unsupported suite-selection condition". A child step gate narrows an aggregate job gate. An action used by multiple gated steps retains all of their consumers. Between two commits, a changed step selects its old and new consumers; adding or removing a step does not select unchanged neighboring steps. Job configuration changes select that job's consumers. The other suites and entry files continue to decide from their content and metadata reads. An ungated selection-control field is not an automatic all-suite trigger.

The workflow reader covers the YAML that GitHub workflows use: mappings, sequences, plain and quoted scalars, block scalars and flow collections kept as text.

#### Unrecorded inputs

Some inputs shape how a unit runs although no recording sees them; `select` compares each of them between the two commits, or with a list of changed files between the checkout's `HEAD` and its working tree, and a change selects the units it shapes:

| Input | A change selects |
| --- | --- |
| A suite's `command`, `setups`, `os` or `entry_selection` in the suite list | every entry file of the suite |
| A suite's `entries`, or the `entry_setups` of one entry file | the entry files it adds or whose setups it changes |
| A removal from the stored lists or the extra reads of an entry file or a suite, such as the pull request of the full recording, a module lookup of one of its lookup directories included | that entry file, or the suite |
| `entry_durations`, an addition to a stored list or to the extra reads, or `always` | nothing by itself |
| A step of a suite job whose condition names setups, such as `contains(matrix.shard.setups, 'python')` or `contains(fromJSON(env.SETUPS), 'dotnet')`, its installed versions included | the entry files of that job that state any of those setups, in the Linux shards and in the Windows job |
| A step whose condition names a language step, `fromJSON(<input or output>.language-steps).<step> == 'run'` | that language step, and no unit |
| A step with a suite or entry-file gate | the units of its gate, as [The CI definition](#the-ci-definition) states |
| Any other part of a suite job that applies before or while its entry files run: the job's condition, `runs-on`, strategy, environment, time limit, the checkout, the recording step's steps and the step that runs the entry files | every unit that the job's gate names; every Linux unit for a job driven by the `shards` output, every Windows unit for one driven by the `windows` output |
| A job above a suite job, its `permissions`, inputs and outputs included; `permissions`, `env` or `defaults` of the workflow that holds suite jobs; `permissions` of the validation caller | every unit of the suite jobs below it, or of that workflow, or that the caller reaches |
| A file of a local recording step's folder, or a module that the recording step loads when it runs: the files of the shared digest | every unit of the jobs that use that recording step; a file of its ETW consumer, `etw-consumer/` or `etw_consumer_build.py`, only their Windows units |
| `on`, `concurrency`, names and comments | nothing |
| The rest of the suite selection, `suite-selection.yml`, `suite-verdict.yml`, the jobs that run `select` and the `validation` job, a step after the entry files that only stores or reports results, and `full-recording.yml` | the entry files whose stored lists read them |

The modules that the recording step loads when it runs are `recording.py` and the modules it reaches through imports: the module-level imports of each module it loads, outside `if TYPE_CHECKING:`, and the imports inside the functions of `recording.py` itself. A change to one of them can change the verdict of every entry file through the recording check. `suite_selection.py` imports `reuse.py` and `unrecorded.py` only inside the functions of `select`, the lookup and the store, which the recording step never calls, so they count with the rest of the selection Tool. [recording_modules.py](../actions/suite-selection/recording_modules.py) names this set for the selection and for the shared digest alike, and a test runs the recording step's commands and fails when a module of the shared CI outside the set is loaded.

A suite job is a job that runs entry files under the selection: in this repository a Linux shard or the Windows job, in a consumer a job of its validation workflow whose gate names its suites or entry files. Every suite job records its entry files, so `select` recognises a suite job as a job that uses the recording step; a test on the validation workflow template shows that the two agree. The jobs above it are the jobs it needs through `needs`, directly or through other jobs, other than the jobs that run `select`, and the job that calls the workflow holding it, with the jobs above that job. A step uses a local action with its action file's content, so a changed local action counts as a changed step; the recording step's own files count under their row. A part of a suite job that names a suite, an entry file, a setup or a language step selects by its own row.

#### Summary

The Markdown summary lists every suite once, as `run` or `skip`, each with its reason:

| Reason | Text |
| --- | --- |
| A changed file matches its path list or its extra reads | `` `<file>` matches `<pattern>` `` |
| A changed file changes a directory membership it reads or an existence read it makes, or adds a module of a lookup name | `` `<file>` matches directory membership `<dir>` ``, `` `<file>` matches <kind> existence `<path>` `` or `` `<file>` matches module lookup `<name>` in `<dir>` `` |
| A changed file matches the list of an entry file | `` `<file>` matches `<pattern>` of `<entry>` ``, with `and <n> more` for further entry files, and the running entry files after `; entries:` |
| A workflow step changes | `` its step in `<workflow>` changed `` |
| A job of a workflow, or a workflow's `permissions`, `env` or `defaults`, changes | `` the job `<job>` of `<workflow>` changed `` or `` `<key>` of `<workflow>` changed `` |
| A file of the recording step changes | `` the recording step's file `<file>` changed `` |
| The suite list changes a suite or an entry file | `its command, setups, operating system or selection per entry file in <suite list> changed`, `the suite is new in <suite list>`, `` `<entry>` is new in <suite list> ``, `the setups of <entry> in <suite list> changed`, `<suite list> removes reads from the lists of <entry or the suite>`, or `<suite list> could not be read at <commit>` |
| A changed file belongs to the CI definition and only steps gated on this suite name it | `` `<file>` belongs to the CI definition (<role>) and only steps gated on this suite name it `` |
| A changed file matches an always-run pattern | `` every suite runs: `<file>` matches the always-run pattern `<pattern>` `` |
| A tracked `.gitignore` file changes | `` every suite runs: the tracked ignore file `<file>` changed `` |
| The change adds a path that the ignore rules match | `` every suite runs: the added `<file>` is matched by the ignore rules of the head commit `` |
| The changed files could not be determined | `the changed files could not be determined: <why>` |
| The suite has no stored path list | `runs on every change: no stored path list` |
| No changed file matches its path list | `no changed file matches its path list` |
| No changed file matches the list of any of its entry files | `no changed file matches the path list of any of its entry files` |
| Every running unit of the suite reuses a passed result | `reuses the passed result of run <id>` |
| Some running entry files reuse a passed result | the reason of the run, then `; reuses the passed results of <n> entry files from run <id>` |

The summary names every changed file that no suite reads as read by no suite. A failed selection writes no summary; `select` prints the error and exits `2`.

#### Push lookup and reuse

On a push and on a [`dependabot-merged`](#the-dependabot-merged-event) run, the step after the checkout of `suite-selection.yml` runs `lookup` on the pushed commit, or on `client_payload.sha`. `lookup` finds the pull request through `commits/<sha>/pulls`, takes the last successful `pull_request` run of the calling workflow on the pull request's head, reads `HEAD is now at <abbreviated merge> Merge <head> into <base>` from the log of that run's first job with an `actions/checkout` step, takes the full id of that merge commit from the fetch line `+<merge>:refs/remotes/pull/<number>/merge` of the same log, and verifies the merge commit through `git/commits/<merge>`, which answers only a full id: its parents are the tested base and the head. It writes one of four cases, which `select --lookup` reads in place of `--base` and `--head`. In the `identical` case it also names that run and the digest of the shared recording step that it took, read from the log of its job with the step `Take the digest of the shared recording step`; a run without that step, or a log that cannot be read or names no digest, names none:

| Case | When | `select` |
| --- | --- | --- |
| `identical` | the pushed tree equals the tested tree | no suite runs, unless the shared recording step selects every suite ([A move of v1 in a consumer](#a-move-of-v1-in-a-consumer)) |
| `behind` | the tested base is an earlier commit of the landing base | a suite runs the entry files selected both over the landing base to the pushed commit and over the tested base to the landing base |
| `pull-request` | the pull request has no successful run, that run has no job with a checkout, its log names no merge commit, the merge commit does not verify, a request failed, or the pushed tree differs from the tested tree while the tested base is no earlier commit of the landing base | over the landing base to the pushed commit |
| `own` | no pull request, the pull request lookup failed, the pull request names no head commit, or `github.run_attempt` is above 1 | over the landing base to the pushed commit |

The landing base is the push's previous head, or the commit's first parent on a dispatch run. The job needs `actions: read` and `pull-requests: read` for the lookup; the hygiene job runs the same command.

On a pull request, `select --results` reuses passed results. Reuse applies only to a running unit whose suite has a stored path list; a suite without `paths` is never reused. A unit's key is the SHA-256 of the content of its reads in the checked-out tree (the tracked paths that its patterns and the `always` patterns match, with their mode and object, the names its directory memberships see, the value of its existence reads, the names in each of its lookup directories that carry a standard-library name or a stored module lookup of that directory, and the calling workflow when a changed step of it selected the suite), its command, the version `--setup-version` gives for each setup it needs, the `--runner-label` of its system, the digest of the shared recording step from `--shared-lookup`, and the tracked `.gitignore` files and the tracked paths that their rules match, with their mode and object, and the content of every [unrecorded input](#unrecorded-inputs) that selects it: its suite's fields and its entry files' setups, and the parts of the workflows and the files of the recording step that apply to it. A later head that changes one of them therefore reuses no earlier pass of that unit. A running unit whose key is in the record is skipped with ``reuses the passed result of run <id>``; with `--run-attempt` above 1 nothing is reused. The outputs `units` and `reused` name the running and the reused units by key.

The record is one cache entry of the pull request's scope, `suite-results-<number>-run-<run id>-<attempt>`. `select` restores the newest with the prefix `suite-results-<number>-run-`, once per run and not on a re-run. Each job that runs units uploads its passed entry files as an artifact `suite-results-*` when its recording check passed. The verdict job, unless the run was cancelled, runs `store`: the record holds the reused keys and every running unit all of whose entry files passed, and nothing when `select` did not succeed.

#### A move of v1 in a consumer

In a consumer, the recording step, the modules it loads and the ETW consumer come from the shared CI at `v1`, not from its own tree, so a move of `v1` changes them without a changed file. The `select` job therefore runs every suite on a consumer's first run after a move:

- The job loads the recording step through `$/actions/recording`, the revision of the running workflow, and its step `Take the digest of the shared recording step` runs `recording.py digest`, which prints `shared CI digest <digest>` and writes the output `shared-digest`. The digest is the SHA-256 over the path and the content hash of every file of the recording step's folder, its action and the committed ETW consumer with its build included, and of every module outside it that the recording step loads when it runs, as [Unrecorded inputs](#unrecorded-inputs) defines them. The files enter in the code-point order of their paths relative to the shared CI's folder, so a revision has the same digest on Linux and Windows, fetched at a tag or used by local path.
- `shared-lookup` then finds the last successful `push` or `repository_dispatch` run of the calling workflow on the base branch (the pull request's base, the default branch on a dispatch run, or the pushed branch) among its last 20 successful runs, takes its job with that step, and reads the digest from that job's log.
- `select --shared-lookup` runs every suite when the two digests differ, and also when that run, its step or its digest cannot be found, so that a consumer's first run, a failed request and a run from before the digest existed all run every suite. The reason names the run it compared with. The digest is part of the reuse key, so no pass from before the move is reused.
- On a push whose lookup is `identical` and names the digest of the passed pull-request run, that run decides in place of the last passed run on the base branch, since its result is the one the push relies on: the same digest runs no suite, also when the base branch took another one, and another digest runs every suite. The merge push of a pull request whose first run after a move ran every suite therefore runs no suite again. An `identical` lookup that names no digest falls back to the base branch.
- Every suite job of the consumer's validation workflows calls the recording step with `digest: ${{ inputs.shared-digest }}`. The action's step `digest` runs `recording.py digest --expect` on the files it fetched before any setup or entry file runs and fails the job when its digest differs from the one `select` took: `v1` moved during the run, and the whole run is started again, since a re-run of the failed jobs keeps the earlier `select`.

This repository's `select` runs the same steps, so its own runs use them before `v1` can move to them. Its recording step comes from its own tree, where a change to those files is also a changed file that runs every entry file of the jobs that use them, so the digest selects nothing more there in practice.

#### Verdict

`verdict` reads a file with the `needs` context of the job that needs the selection job and every job it gated, as `toJSON(needs)` renders it: one entry per job with its `result` (`success`, `failure`, `cancelled` or `skipped`) and its `outputs`. The verdict fails when the result of the job `select` is not `success`, or when the `needs` context has no `select` job, or when any other job ended `failure` or `cancelled`; otherwise it passes. A skipped job is a suite the selection did not run.

The command prints the verdict and, with `--summary`, appends a Markdown summary that states the verdict with its reasons, lists every suite with its decision from the `suites` output of `select` when that output is present, and lists every job of the context with its result.

#### Entrypoint

```text
python suite_selection.py select --root <repository> --suites <suite list> --base <commit> --head <commit> --compare merge-base|direct [--workflow <calling workflow>] [--language-step python] [--github-output <file>] [--json-output <file>] [--summary <file>]
python suite_selection.py select --root <repository> --suites <suite list> --base <commit> --head <commit> --compare merge-base --results <record file> [--run-attempt <n>] [--runner-label <os>=<label> ...] [--setup-version <setup>=<version> ...]
python suite_selection.py select --root <repository> --suites <suite list> --changed-file <path> [--changed-file <path> ...]
python suite_selection.py select --root <repository> --suites <suite list> --changed-list <file with one path per line>
python suite_selection.py select --root <repository> --suites <suite list> --lookup <file from lookup> [--json-output <file>] [--summary <file>]
python suite_selection.py select --root <repository> --suites <suite list> --full [--json-output <file>] [--summary <file>]
python suite_selection.py select ... [--shared-lookup <file from shared-lookup>]
python suite_selection.py lookup --root <repository> --sha <commit> [--before <commit>] --workflow <calling workflow> [--run-attempt <n>] --output <file> [--summary <file>]
python suite_selection.py shared-lookup --digest <digest> --branch <base branch> --workflow <calling workflow> --output <file> [--summary <file>]
python suite_selection.py store --needs <file with the needs context as JSON> --passed <directory> --run-id <id> --output <record file> [--github-output <file>]
python suite_selection.py verdict --needs <file with the needs context as JSON> [--summary <file>]
```

`select` prints one line per suite. With `--github-output` it appends `<suite>=run` or `<suite>=skip` per suite to that file, so a later step can carry `if: steps.<id>.outputs.<suite> == 'run'`. For a suite with `entry_selection` it also appends `<suite>-entries=` and `<suite>-modules=` with the selected entry files, space-separated, empty when the suite is skipped.

With `--json-output` it appends one line `suites=<JSON object>`, the object holding `run` or `skip` for every suite, so a job output can carry the whole selection to other jobs. When a suite has `entry_selection`, a second line `entries=<JSON object>` holds the selected entry files of each such suite. It also writes `shards=`, `windows=`, with `--language-step` `language-steps=`, with `--results` `units=` and `reused=`, and with `--lookup` of an `identical` case whose shared digest did not move `identical=true`, as [The selection and verdict workflows](#the-selection-and-verdict-workflows) describes them.

With `--summary` it appends the Markdown summary. With `--workflow` it applies the CI-definition rule for the calling workflow, given as its repository path or as `github.workflow_ref` (`<owner>/<repository>/<path>@<ref>`). `--full` selects every entry file of every suite for the [full recording](#full-recording) and takes no changed files. Its `shards` count each duration estimate under the wide trace set, at 1.65 times the stored estimate, and keep each shard under 300 s on its two workers; its `windows` lists every Windows entry file.

| Code | Meaning |
| --- | --- |
| `0` | Selection completed successfully, or `verdict` reports that the run passes. |
| `1` | `verdict` only: the run fails. |
| `2` | Invalid arguments or suite list, an entry file or pattern that matches no tracked file, a missing entry file, a gate on a suite the suite list lacks, a malformed or unreadable CI definition, or an unreadable `needs` context. |

### Recording step

The recording step records what a job's setups and entry files read and compares it with the suite list. It runs inside the job that runs the suites, on Linux under `strace -f --seccomp-bpf`, which needs no root and no BPF program, and on Windows through a live Kernel-File ETW session, which needs administrator rights. It installs nothing and stops when `strace`, or on Windows `dotnet`, is missing. A job calls [recording/action.yml](../actions/recording/action.yml) once for its path, as its first step after the checkout, and once with `check: "true"`, `suites` and an `artifact-name` unique in the run, after its entry files. The first call also runs `base`. In a consumer, the first call also takes `digest`, the digest of the shared recording step that `select` took, as [A move of v1 in a consumer](#a-move-of-v1-in-a-consumer) says.

```text
python3 recording.py base [--file <file>]
python3 recording.py start [--output <directory>] [--root <repository>] [--environment <file>] [--full]
python3 recording.py exec [--output <directory>] --setup <name> -- <command> ...
python3 recording.py exec [--output <directory>] --entry <file> --suites <suite list> [--root <repository>] [--suite <name>] -- <command> ...
python3 recording.py provide [--output <directory>] --setup <name> [--path <directory>] ... [--variable <name>=<value>] ... [--python <interpreter>] ...
python3 recording.py check --suites <suite list> [--output <directory>] [--root <repository>] [--additions <file>] [--summary <file>] [--github-output <file>]
python3 recording.py apply [--root <repository>] [--suites <suite list>] [--summary <file>] <additions or full-recording results> ...
python3 recording.py digest [--expect <digest>] [--github-output <file>]
```

- `base` writes the job's environment to `--file`, `$RUNNER_TEMP/recording-environment.json` by default; the action runs it before any setup, on Windows from `pwsh`.
- `start` prepares the output directory, `$RUNNER_TEMP/recording` unless `--output` names another. It takes the job's environment before its setups from `--environment`, or from `$RUNNER_TEMP/recording-environment.json` when that exists, and otherwise from its own environment, and keeps it in the output directory. It finds `strace`, on Windows `dotnet`, on that environment's `PATH`, runs the ETW consumer in it, and stops when it runs on another interpreter than that environment's `python3`, on Windows `python`. Pull-request and push runs record the mid trace set: opens, `execve`, listings, `access`, `faccessat*`, `newfstatat` and `statx`, with the calls that follow processes and working directories. `--full` selects the full-recording mode, which records the wide trace set, the mid set with `stat`, `lstat`, `readlink` and `readlinkat`. On Windows `start` starts the ETW consumer on the checkout (`--root`, the working directory by default, in its given and its resolved form) and the output directory, waits until its session runs, and records in both modes through that one session.
- `exec` runs one setup or one entry file as its own process, with its own empty temporary directory (`TMPDIR`, and on Windows `TEMP` and `TMP`), bytecode cache (`PYTHONPYCACHEPREFIX`) and directory of lookup reports (`SHARED_LOOKUPS`, under `lookups/` of the output directory), passes its output through and exits with its exit code. On Linux the process runs under `strace`, which prints the environment of each exec in full, and once the process ends `exec` keeps only the pathspec variables of each such environment in the trace; on Windows `exec` records the process id of the entry file's process and of `exec` itself. A setup runs in the current environment of the job, with the earlier setups, and a setup that exits with `0` counts as installed in the job. An entry file runs in the job's environment before its setups, as `start` kept it, with what the setups that the suite list `--suites` states for it provide, its suite's and its own: their tool directories first on `PATH`, in the order the suite list states them, and their variables. No other change of the job's environment reaches it, neither that of another setup nor a variable of the step that runs `exec`. `exec` refuses an entry file whose setup the job did not install. `--root` names the repository whose tracked files the suite list's entry patterns match, the working directory by default; `--suite` names the suite when the suite list names the entry file in several suites.
- `provide` adds to what one setup gives the entry files that state it: each `--path`, an absolute directory that must exist, and each `--variable`. Each `--python` installs the lookup report into the site directory of that interpreter. A job calls it only for a setup that gives something. A setup that a `uses:` action performs has no `exec` and counts as installed once `provide` names it.
- `check` parses each `strace` log, skipping system-path lines, classifies the reads, maps them to repository paths, gives each entry file the reads of the setups that its suite or the entry file itself states, and compares the result with the stored lists of that entry file. In a pull-request or push run it writes the additions to `additions.json` in the output directory, prints them, lists them in the job summary and exits with `1` when there are any; a suite without a stored path list always gets one. In the full-recording mode it writes `full-recording.json` with every entry file's classified reads, exit code, reported skip count, why its TAP report was incomplete when it has no skip count, and traced seconds, and exits with `0`. The action uploads either file as the artifact and then fails the job on additions.
- `apply` adds the additions of one or more runs to the suite list and never removes a stored read. It refuses to give a suite without a stored path list a list until every entry file of that suite is recorded in the files it is given. With full-recording results it replaces the recorded lists of an entry file that exited with `0` and whose test report in an accepted format reports no skipped test, and of a suite selected as a whole only when every entry file did. It keeps every other list, adds the recorded reads to it, and reports each entry file it kept with the reason. It writes each recorded Linux entry file's duration estimate as its traced seconds divided by 1.65, the wide-to-mid ratio, and each recorded Windows entry file's as its traced seconds, since the Windows job records under its one ETW session. Extra reads stay as they are. It writes every recorded list and every module lookup sorted, and each per-entry field in the order of the suite's entry files, so the suite list it writes does not depend on the order of the results it is given or of earlier applies.
- `digest` prints the digest of the shared recording step that [A move of v1 in a consumer](#a-move-of-v1-in-a-consumer) defines, and with `--github-output` writes it as `shared-digest`; with `--expect` it exits with `1` when the digest differs.

On Windows, `check` first stops the ETW session and waits until the consumer has drained it; it fails when the session lost events. It reads the one neutral event file of the job and attributes each line to an entry file or setup by process id: a process belongs to the entry file or setup whose root process, started by its `exec`, it descends from, each process id by its latest start before the line, and a root process without a start event forms the group of its own entry file or setup. It turns the lines into the records that the Linux parser produces, with the recorded checkout mapped to the checked one and each path spelled as Git tracks it, and classifies them with the same rules: an open is a content read, an open of a missing path an existence read, an enumeration with a wildcard pattern a listing, and an enumeration of one literal name a stat or a failed probe of that path. It prints and summarizes how long the stop and the parse took.

A test runner's skip count comes from the complete test reports in an accepted format in the entry file's output: a `python -m unittest` summary, a pytest summary, or a TAP report. A TAP report is a plan line `1..N`, before or after its test lines, and its test lines, each starting with `ok` or `not ok`. Its skip count is the number of test lines with a `# SKIP` directive: a `#` after the description, then the word `SKIP` in any case, then the end of the line or a reason. A plan of `1..0`, with or without a comment, skips the whole entry file and counts as one skipped test. A TAP report with more than one plan, or whose plan states another number than its test lines, is incomplete and counts as no report, since the run may have stopped early; the other reports in the output still count. An entry file without a complete report in an accepted format has no skip count, and the kept list of an entry file with an incomplete TAP report is reported with the reason.

Classification:

- A read or a stat of a path that exists in the checkout is a content read; a failed probe is an existence read of kind `any`, and a stat or an open of a directory is an existence read of kind `directory`.
- A listing is a directory membership, unless the lookup report of the listing process attributes it to the import system, as [Module lookups](#module-lookups) states. Every listing of a process without a report is a directory membership, an interpreter's listing of its own module search path and PowerShell's listing of its module path included.
- A path that the ignore rules match and that is not tracked is no read, and no path with a `.git` component is, so the Git directory of a nested repository drops as well. The ignore rules are the tracked `.gitignore` files of the checked commit, matched case-sensitively on every system against the repository path; a pattern that ends in `/` matches a path that the recording or the tracked set shows as a directory, and every path beneath it. `.git/info/exclude`, `core.excludesFile` and untracked `.gitignore` files have no effect, so a recording classified on a machine with local ignore settings reports the reads that only those settings match as additions.
- A `git` process, an executable `git` or `git-<name>`, that reads the index, objects or refs of the Git directory of the checkout or of a repository copy, the directory of a linked worktree in it included, reads what its command depends on, although the `.git` rule drops each such path. When the checkout is itself a linked worktree, its Git directories are the directory that the `gitdir:` line of its `.git` file names and the common directory that the `commondir` file there names; on Windows `start` adds both to the paths of the ETW session and records them in its settings. A `git ls-files` whose options keep its output to the names of tracked paths (`-z`, `-c`, `--cached`, `--full-name`, `--deduplicate`, `--error-unmatch`, `--sparse`, `--exclude-standard`, `--pathspec-from-file`, `--pathspec-file-nul`) and that passes no pathspec is a directory membership of `**`. One that passes a pathspec reads what that pathspec can list: a literal path is an existence read of that path, of kind `any`, and a directory membership `<path>/**`; a pathspec with `*` or `?` is a membership in which each of them is `**`, and `<pattern>/**` of that pattern; a `:(glob)` pathspec is a membership of its own pattern and `<pattern>/**`, and without `*` or `?` a literal path; an `:(exclude)` pathspec, also as `:!` or `:^`, adds nothing. Any other form is a directory membership of `**`: other magic, a bracket class or a backslash, a `.` or `..` part, a listing whose every pathspec is an exclude, a pathspec relative to a working directory other than the root of the checkout or of a repository copy, an option of Git's own before the command such as `-C`, a pathspec file, and a pathspec variable (`GIT_LITERAL_PATHSPECS`, `GIT_GLOB_PATHSPECS`, `GIT_NOGLOB_PATHSPECS` or `GIT_ICASE_PATHSPECS`) in the environment of the process. On Windows the recording has no environment of a process, so every pathspec there is a directory membership of `**`. A `git cat-file` without a `--batch` option whose every object argument is a full object id, with or without a `^{<type>}` suffix, is a content read of the tracked paths of the checked commit that hold each object: the paths of a blob, `<directory>/**` for the tree of a directory, and `**` for the checked commit and its tree; an id that no tracked path holds adds nothing. Every other such process is a content read of `**`, so any change to a tracked path selects it. Git reads `HEAD` whenever it finds the Git directory, so a command that needs only the configuration, such as `git remote get-url`, falls under that last case too; the read of the configuration itself stays dropped. `check` takes the command from the arguments of each `execve` on Linux, with the pathspec variables from its environment, and from the command line of each process start on Windows.
- An absent probe that a PowerShell process (`pwsh` or `powershell` by its executable) makes in its working directory for a name ending in `.ps1` or an extension of the default Windows `PATHEXT` (`.com`, `.exe`, `.bat`, `.cmd`, `.vbs`, `.vbe`, `.js`, `.jse`, `.wsf`, `.wsh`, `.msc`, `.cpl`) is its command search and no repository path. On Windows the recording knows only the working directory of an entry file's or setup's own process; a process that it starts has none, so that process's probes stay existence reads, which may over-select but never skips a suite. Every other absent probe stays an existence read: one by another program, one of a name that a started program shares, a whole command line probed as one name, and an absent path that a process tries to run.
- On Windows, a path that ends in `::$DATA` is a read of the file itself; a path that names another stream, such as `<file>:Zone.Identifier` or `<file>:<name>:$DATA`, is no repository path. On Linux a colon is an ordinary character of a path.
- Under the temporary directory of `exec`, a directory that a `git clone` or `git worktree add` with the checkout as source creates is a repository copy; its paths map back by their relative path, tracked paths and absent probes alike. `check` finds such a command in the arguments of each `execve` on Linux and in the command line of each process start in the neutral event file on Windows. A relative source or destination resolves against the process's working directory where the recording knows it: on Linux always, on Windows only for an entry file's or setup's own process, from the record that its `exec` wrote. `check` fails and names the process and its command when a `git clone` or `git worktree add` has a relative source or destination in a working directory that the recording does not know, or when a `git` process's start has no command line; the command then gets absolute paths, `git worktree add` with `-C <checkout>`. A directory filled by copying files is no repository copy: the copier's own reads in the checkout name the copied files. A fixture repository that is no clone or worktree of the checkout is none either, so its reads and probes stay out of the path lists. On Windows a local `git clone`, `fetch`, `push`, `ls-remote` or `archive --remote` runs with a working directory outside the checkout, so that it makes no probe there, just as a Git call that ends its revision arguments with `--` in the next bullet makes none. Git starts its transport helper through an MSYS2 shell, and the shell looks up the quoted command text as a name in its working directory. With an absolute source those names change on every run, and no classification rule drops a name because of its shape. On Windows the inner clone of `git submodule add` with a local source runs in the submodule's working directory, which Git chooses, so `check` reports its names as additions, like any name that changes on every run.
- A failed probe of a name that is no path gets no rule of its own: Git's check whether a revision argument also names a file, made with the checkout as working directory, is an existence read, since a file of that name changes what Git does. A probe name that changes on every run shows as an addition; the call then ends its revision arguments with `--`, which stops the probe. On Windows the probe is an attribute query, which the recording does not see, so it is the accepted miss of an existence read through an attribute query.
- A start of another language's program counts through the process tree; the suite list has no field for edges between languages.

Toolchain probe rules: [toolchain_probes.json](../actions/recording/toolchain_probes.json) holds, per toolchain, the names that the toolchain looks for to configure its run and the directories it looks in, each rule with its source. A toolchain is a language toolchain of the shared CI's supported languages that an entry file or setup runs; a tool that a setup installs, such as ruff, mypy, gitleaks or zizmor, is none. When a recording shows a process of a toolchain's program, `check` adds to the reads of that entry file or setup an existence read of kind `any` of every name of every rule in every directory the rule names, unless the recording already holds that path; it does so on Linux and Windows, in every trace set. A rule starts from one or more anchors, and an upward rule also covers every directory above it up to the checkout root:

- project or solution: the directory of each project file (`.csproj`, `.fsproj`, `.vbproj`, `.proj`) or solution file (`.sln`, `.slnx`, `.slnf`) that a process of the toolchain reads in the checkout or that a start of its program names as an argument;
- argument: a directory that a start of the toolchain's program names as an argument;
- working: the working directory of each process of the toolchain.

An argument is placed when it is absolute or when the recording knows the working directory of the start that names it. On Linux the recording knows the working directory and the arguments of every process; a process starts in its parent's working directory also when `strace` writes its own lines before the parent's clone or fork return, as it can for a `vfork` child. On Windows it knows the working directory only of the process that `exec` starts: a toolchain process that another process starts places only its absolute arguments, and its working-directory rules start from the working directory of the recorded command and from every checkout directory in which that process read.

- The .NET SDK with MSBuild and NuGet (`dotnet`): `Directory.Build.props`, `Directory.Build.targets` and `Directory.Packages.props` upward from the project and argument directories; `Directory.Build.rsp` upward from the project, solution and argument directories; `Directory.Solution.props` and `Directory.Solution.targets` upward from the solution and argument directories; `before.<solution file>.targets` and `after.<solution file>.targets` in the solution directory; `nuget.config`, `NuGet.config` and `NuGet.Config`, and `global.json`, upward from the project, solution, argument and working directories.
- Python, PowerShell, Node.js with npm, and Lua have no rules: their configuration lies outside the checkout or the recording sees it as an open, and their other probes are module lookups or existence reads of the code.

A toolchain gets its rules when a repository that the shared CI supports first runs it.

The classification rules are tested on stored strace recordings and on recorded neutral ETW event files, and the tests of the recording step start no tracer of their own: they run under the recording like every other entry file, so real `strace` output is parsed in every recorded run. [Renew the stored strace recordings](#renew-the-stored-strace-recordings) says how the stored recordings are made.

These misses are accepted: a branch that no run takes; in pull-request and push runs on Linux, a read whose only calls are `stat`, `lstat`, `readlink` or `readlinkat`, existence and content reads alike, which the next full recording adds; on Windows, any other existence read through an attribute query or a stat call, present or absent, outside a toolchain's probe rules, and a Node process's absent lookup in a checkout directory in which it opened nothing; the reads of a test skipped for a missing tool; a history rewrite that removes an object that an entry file names by a full id, which makes that entry file fail without selecting it until the full recording runs it; and any read that no recording sees. The change that adds a read no recording sees states it as an extra read.

A read is inside the stored lists when a stored pattern or an always-run pattern matches it; an existence read also when the same path is stored with the same kind or kind `any`, its parent directory is a stored membership, a stored `<directory>/**` pattern covers it, or, for a directory, a stored pattern matches a tracked file beneath it; a membership when the same directory, `<directory>/*` or `<directory>/**` is stored as a membership or covered by a stored `<directory>/**` pattern.

| Code | Meaning |
| --- | --- |
| `0` | The command succeeded; `check` found every read inside the stored lists or wrote a full-recording result; `exec` passes the command's own exit code. |
| `1` | `check`: the recording reads outside the stored lists; `digest --expect`: the digest differs from the expected one. |
| `2` | Invalid arguments, an entry file whose stated setup the job did not install, a missing tool directory for `provide`, an interpreter for `provide --python` that names no site directory, a `start` on another interpreter than the job's, no `strace` on Linux, no `dotnet`, no administrator rights or no committed consumer on Windows, or another system for `start`, an ETW session that lost events or whose consumer did not stop, no recording in the output directory, an unreadable suite list or input file, an entry file that the suite list lacks or names in several suites without `--suite`, or a suite without a stored path list whose entry files are not all recorded. |

#### Module lookups

A Python interpreter's path finder looks for a module file only when its name is in its cached listing of the directory, so a lookup that finds nothing in a directory leaves a listing and no failed open. The lookup report [shared_lookup_report.py](../actions/recording/shared_lookup_report.py) names these lookups. `provide --python` copies it into the site directory of the interpreter and writes `shared-lookups.pth` there, whose one import line loads it and adds no directory to the module search path. It loads into every process of that interpreter that runs `site`, `-I` and `-E` included, and into no process started with `-S` or in a virtual environment without the system site directory. While `SHARED_LOOKUPS` names a directory, it appends to `<process id>.tsv` there, as the lookups happen, every directory whose listing the import system makes and every module name it looks up in a directory, found, absent or as a namespace portion, with the file suffixes the path finder tries. `check` reads the reports beside the recording:

- A listing of a directory by a process or thread counts against the listings of that directory that its report names; each listing beyond them is a directory membership. A listing is one `getdents` sequence on a descriptor since its open on Linux, and one enumeration line of the neutral event file on Windows, where the consumer writes the first enumeration of each open of a directory however often the process lists it.
- A module name looked up in a checkout directory without being found there, or found there as a namespace portion, is a module lookup. The entry file's lookup directories gain the directory, and the suite list's module lookups of that directory gain the name unless it is a standard-library name.
- A found module in a checkout directory is a content read of its file and makes the directory a lookup directory; `check` adds an existence read of kind `directory` of its package directory and of kind `any` of its name with each suffix that the path finder tries before the found file's suffix, such as its extension-module names for a source file, unless the recording already holds them. A found package has none.
- Node has no lookup report. On Windows, the entry file of a Node process, `node` or `tsc` by its program, including a process of a setup whose reads reach that entry file, reads as a directory membership every checkout directory in which that process opened a file, except a directory that the ignore rules match. An absent lookup in a checkout directory in which the process opened nothing, such as a `node_modules` directory that does not exist, is not seen. On Linux, `strace` records Node's candidate and extension probes as existence reads.

`apply` adds the lookup directories and the module lookups. With full-recording results the module lookups of a directory become those that the recorded entry files report, and keep the stored ones while an entry file whose lists stay looks up modules there.

#### ETW consumer

The ETW consumer is a C# program in `recording/etw-consumer/` on TraceEvent, framework-dependent on the .NET runtime that its project names; the project file, `global.json` and `packages.lock.json` hold the versions. It only reads the live session, with the Kernel-File keywords `0xF0` (file names, file I/O, operation ends and creates), the Kernel-Process keyword `0x10` and the kernel process events, which carry a process's command line, and writes the neutral event file: one line per event in stream order, with the process id, the parent process id, the operation and the NT device path, separated by tabs. Its operations are `start` (a process start, with its image and, in a fifth column, its command line with backslashes, tabs and line breaks escaped as `\\`, `\t`, `\n` and `\r`; a start written before its command line came, because another line of the process or of a child came first, has no fifth column), `open` and `opendir` (an open of an existing file or directory that neither creates nor overwrites it), `absent` (such an open of a missing path), and `enum` and `enum-absent` (a directory enumeration, with the directory, a backslash and the queried pattern). It writes only file events under the paths that `start` names, and each process's repeated event once, except an enumeration: the first enumeration of each open of a directory, the one that starts a listing, every time, and none of the queries that continue it. On stop it prints the time it took to drain the session, the counts of events and lines, and the events the session lost.

The built consumer is committed in `recording/etw-consumer/build/`, which a consumer's job receives with the checkout of the step at `v1`; no recording job builds or downloads it. The SDK is pinned in `etw-consumer/global.json` and the packages in `etw-consumer/packages.lock.json`. The build is byte-reproducible on Windows: the same source gives the same files on any Windows machine with the pinned SDK, while on Linux the SDK writes other line endings into its generated files and the assembly differs. `.gitattributes` keeps Git from converting the committed files.

The check runs once, as the Windows suite `etw-consumer-build` of this repository's `tests/suites.json`, whose entry file `tests/test_etw_consumer_build.py` runs `etw_consumer_build.py check` in the `windows` job of `validate.yml` after the `dotnet` setup, `actions/setup-dotnet` with the SDK that `global.json` pins. `check` publishes the source with the pinned SDK from the locked packages and compares the result file by file and byte by byte with the committed build; it fails and names each differing, missing or extra file. The job keeps no rebuilt consumer: the repair is a local rebuild, as [Change the ETW consumer](#change-the-etw-consumer) describes, or on a Dependabot pull request the [rebuild workflow](#the-rebuild-workflow). The suite is selected on its recorded reads, and its result reaches `validation / validation` through that job, so a change of the consumer's source without a matching build fails the required check.

```text
python recording/etw_consumer_build.py build
python recording/etw_consumer_build.py check
```

#### The rebuild workflow

`rebuild-etw-consumer.yml` commits the rebuilt consumer to a Dependabot pull request that changes the consumer's source, so that an update of its package or its SDK passes CI on its own branch. It runs on `workflow_run` when `Validate` completes, one run per head branch at a time, and is the only workflow that runs `etw_consumer_build.py build`; `ci-policy` keeps it so. Its jobs:

| Job | Runner | Rights | What it does |
| --- | --- | --- | --- |
| `plan` | `ubuntu-slim` | `contents: read`, `pull-requests: read` | Runs only for a failed `pull_request` run of a `dependabot/` branch of this repository. It acts when the open pull request by `dependabot[bot]` at the run's head changes a file under `etw-consumer/` outside `etw-consumer/build/`. |
| `build` | `windows-2025` | `contents: read` | Checks out the run's head without credentials, sets up the SDK that its `global.json` pins, runs `python actions/recording/etw_consumer_build.py build` and uploads `etw-consumer/build/` as the artifact `etw-consumer-build`. It holds no App token. |
| `push` | `ubuntu-slim` | `actions: read`, `contents: read` | Runs the workflow's own tools on a checkout of the run's head: applies the run's `recording-additions-*` artifacts to the suite list with `recording.py apply` and replaces `etw-consumer/build/` with the rebuild. When nothing changed, it commits nothing. A changed file other than `etw-consumer/build/` and `tests/suites.json` fails the job. Otherwise it commits `chore: rebuild the ETW consumer` as the [GitHub App](#github-app), scans the commit with the pinned Gitleaks and pushes it to the pull request's branch, unless the branch moved since the run. |

The push uses the App's installation token, so it starts `Validate` on the new head. A rebuilt consumer can read other files, so that run can report new additions and fail again; the workflow then acts again, until `Validate` passes. When the head's build equals the rebuild and the run uploaded no additions, it commits nothing, whatever else failed. The App's key is a repository secret and no Dependabot secret, so only a `workflow_run` run reaches it. [Dependabot auto-merge](#dependabot-auto-merge) accepts the App's commit.

Once the App's commit is on the branch, Dependabot no longer rebases the pull request, also not on `@dependabot rebase`. `@dependabot recreate` builds the pull request again from the default branch, and the workflow then runs on it again.

#### Full recording

The full recording is the safety net of the stored lists and the only route that removes a stored read. Its shared part is the full-recording mode of the recording step. Every repository with stored path lists runs it for its own suites in a scheduled workflow of its own, with its own setups, shards and closing job, from the template `full-recording.yml` of the [consumer kit](../actions/suite-selection/consumer-kit/README.md). This repository runs the full recording of its own suites in `full-recording.yml`. Its schedule starts the workflow on Mondays, and the `plan` job records only in even ISO weeks, so a scheduled recording runs every two weeks on the default branch. A run started by hand (`gh workflow run full-recording.yml --ref <branch>`) records the branch it starts on in any week. The jobs:

| Job | Runner | What it does |
| --- | --- | --- |
| `plan` | `ubuntu-26.04` | Runs `select --full` and passes its `shards` and `windows` outputs on. |
| `linux` | `ubuntu-26.04` | One job per shard, with the steps of the `linux` job of `validate.yml` and the recording started with `start --full`; its `check` uploads `full-recording-shard-<number>`. |
| `windows` | `windows-2025` | Every Windows entry file under ETW, with the steps of the `windows` job of `validate.yml` and `start --full`; its `check` uploads `full-recording-windows`. |
| `close` | `ubuntu-slim` | Downloads every `full-recording-*` artifact, runs `apply` over all of them and uploads the suite list and the report as `full-recording-joined`. |

The setups and the run of the entry files of `linux` and `windows` are those of `validate.yml`, and `ci-policy` fails while they differ. `close` runs unless the run was cancelled, also after a failed shard: `apply` keeps the lists of every entry file that failed or was not recorded and names each one in the report. On the default branch, when the suite list changed, `close` commits it as the [GitHub App](#github-app), scans the unpublished commits with the pinned Gitleaks, pushes `chore/update-recorded-lists` with `--force-with-lease` on the remote tip it read, and opens or updates the pull request `chore: update the recorded path lists` from `chore/update-recorded-lists`, with the report as its body; the pull request starts CI like any other. A run on another branch makes no pull request; its result is the artifact `full-recording-joined`. When the App's token, the push or the pull request fails, the run stops with an error: the workflow uses the App's existing installation and credentials only, and any other setting is a question to the user (`docs/agents/publication.md`).

### Python language step

The Python language step runs the shared Python checks in a consumer's job that installs Python, on that job's `python3` (`python` on Windows); it installs no interpreter. The step is [python-language-step/action.yml](../actions/python-language-step/action.yml); it installs its pinned tools from the pins file into the runner's temporary directory with that interpreter.

- `ruff check` runs over the repository with the repository's ruff configuration, or ruff's default rules without one, and writes no cache into the checkout.
- `mypy` runs only when the repository configures it at its root: a `mypy.ini` or `.mypy.ini`, a `[tool.mypy]` table in `pyproject.toml` or a `[mypy]` section in `setup.cfg`. It runs on the job's own `python3`, not on a Python that a setup installed outside the job's environment, and checks the configured `files`, or the root without them, against the packages of that interpreter.
- `ruff format --check` runs when the job passes `format-check: "true"`.

Every check runs; the step fails when one fails or when a tool is missing. `select` decides whether a caller's step runs: the caller names it in the `language-steps` input of `suite-selection.yml`, and the `language-steps` output holds `run` when a changed file is a Python file or Python configuration. When the step runs and no Linux suite does, `select` returns one shard without suites for it.

This repository calls the step in its first Linux shard, after the recording's `check` and outside the recording, so that its reads join no suite's path list. Its `ruff.toml` selects the rule sets `F` and `E9`, and it configures no mypy. The [consumer kit](../actions/python-language-step/consumer-kit/README.md) of the Python language step holds the step's template, `consumer-kit/python-language-step.yml`, and explains to a consumer what the step runs, which repository configuration turns each check on and how a validation workflow calls it. A change to the checks, their configuration or the step's inputs changes that explanation in the same change. The `language-steps` input of a validation workflow names no suite in the [CI definition](#the-ci-definition).

```text
python3 python_language_step.py [--root <repository>] [--tools <directory> [--install <pins file>]] [--format-check]
```

The shared Python code supports the oldest system `python3` of the images that shared jobs and steps run on, Python 3.12 today. The `python-floor` suite checks the code under `actions/` with the pinned mypy against 3.12 and fails on each error that mypy reports under 3.12 and not under the interpreter that runs mypy, such as a call of `PurePosixPath.full_match`.

### Repository prerequisites

The user configures these settings; the workflows change none of them. The consumer kit of a part states the settings that part needs; for the parts without a consumer kit, they are:

- Dependabot alerts and security updates are enabled in each consumer repository with a Dependabot configuration.
- A ruleset on the default branch requires status checks with GitHub Actions as the source of each check, in each repository that calls the auto-merge workflow. A check can be selected only after it has succeeded in that repository within the past 7 days.

## Maintaining the shared parts

Each procedure lists the files to change in order and how the change reaches callers.

### Validation

CI is the only gate. While building, run the narrow validation that the ticket or Work Brief needs: the entry files of the changed behaviour, each with its suite's `command` from `tests/suites.json`, as `tests/README.md` says, together with the tools some entry files need. No local copy of the CI's selection runs before a push; the required validation is the one that `docs/agents/delivery.md` states. The pull request's CI then runs the selected suites under the recording, and its `hygiene / hygiene` check runs the hygiene checks over the changed workflows, Markdown and package files. A recording check that fails lists the additions in its job summary and uploads them as the artifact `recording-additions-shard-<number>` or `recording-additions-windows`; download it with `gh run download <run id> -n <artifact>`, apply it with `python3 actions/recording/recording.py apply --suites tests/suites.json <additions file>`, and push the changed suite list in the same change.

The tests fetch no specification and install no package; the updater and move-decision tests run offline against mocked responses and local fixture repositories. They prove policy and update behaviour; cross-repository event delivery, App authentication, repository settings and tag promotion show only in a caller's live runs.

### The v1 channel

The shared surface is [shared-surface.json](../actions/repository-hygiene/shared-surface.json): the reusable workflows that callers use through `v1`, the part folders they load through `$/`, the pins file and the updater branch. The catalogue is on it, and every consumer kit lies in a part folder, so they are part of it, as is `actions/repository-ruleset`, the ruleset template that the consumer kit of repository hygiene leads to. Callers receive a change under a path of the shared surface only when `v1` moves, and creating or moving `v1` changes the later runs of every caller; moving it back does not undo those runs. This repository's own callers run the changed workflows from the pull request on. A consumer reads the catalogue, the consumer kits and the ruleset template at `v1`, so a change to them reaches consumers only when `v1` moves.

`v1` moves only to a commit whose CI on `main` passed, by a manual promotion and by the automatic move alike. Because this repository calls every shared workflow by local path, that CI has run the workflows the commit carries. The automatic move of a pins-only commit is the exception to the question that [Who moves v1](#who-moves-v1) states; it checks the conditions under [Automatic v1 move](#automatic-v1-move), the commit's CI on `main` among them. A move gets no separate check afterwards: the consumers' ruleset and caller runs right after the move are the check, each consumer's first validation run after it runs every suite ([A move of v1 in a consumer](#a-move-of-v1-in-a-consumer)), and `v1` moves back, as [Roll v1 back](#roll-v1-back) says, when one of them fails on the moved workflows. A `promote-v1.yml` run writes the old and the new commit to its summary, and a caller's run records the commit that `v1` resolved to; no other record of a move is kept.

A move that changes check names ships in one coordinated move. When `v1` moves, no consumer has an open pull request. Right after the move, each consumer updates its default-branch ruleset to the new check names and then its callers, before any other work. A Dependabot pull request that opens in that window is recreated after the cut-over. The ruleset and caller update of each consumer is a question to the user (`docs/agents/publication.md`).

#### Who moves v1

Creating or moving the shared `v1` tag, every time, including the first creation and a rollback, is a question to the user: one promotion changes every caller's subsequent runs, and moving the tag back does not undo those runs' effects. The one exception is automatic: merging a pins-only pull request that the updater App opened from `chore/update-ci-pins`, as the repository owner, is the answer for `.github/workflows/promote-v1.yml` to move `v1` to that merged commit when the shared surface otherwise matches the current `v1` and the commit's CI on `main` passed, as [Automatic v1 move](#automatic-v1-move) states. Every other creation or move of `v1` still needs the question.

#### Automatic v1 move

`promote-v1.yml` runs on every push to `main` that changes `pins.json`. [promote_v1.py](../actions/repository-hygiene/promote_v1.py) moves `v1` to the pushed commit only when all of these hold:

- the commit is the merge commit of a merged pull request that the updater App opened from `chore/update-ci-pins`;
- the repository owner's account merged it;
- the commit changes only `pins.json`;
- the shared surface differs between the current `v1` and the commit in `pins.json` only, and the current `v1` is an ancestor of the commit;
- the commit's CI on `main` passed: for each of `validate.yml` and `repository-hygiene-caller.yml`, the newest push run on `main` for the commit concluded `success`. A validation run whose verdict job was skipped for an identical tree concludes `success` too.

The CI condition is checked last. The `decide` job polls both runs every 30 seconds through the GitHub API with the job's token, which holds `actions: read`. Each request times out after 20 seconds, and a poll starts only while it ends within 10 minutes of the start of the wait, even when every request takes its full timeout. A read that fails counts as a run that has not completed, and polling goes on. The job has `timeout-minutes: 14`, under the 15-minute limit of `ubuntu-slim` ([GitHub changelog, 2026-01-22](https://github.blog/changelog/2026-01-22-1-vcpu-linux-runner-now-generally-available-in-github-actions/)), so the wait ends and the summary is written before the runner stops the job. Any conclusion other than `success`, a run that has not completed when the wait ends, or a read that still fails on the last poll keeps `v1`.

When any condition fails, `v1` stays. When the paths of the shared surface hold identical content at the current `v1` and at the commit, the run summary says that `v1` stays and that no promotion is needed. Any other stay gives the reason and says that a manual promotion is needed. The move pushes with `--force-with-lease=refs/tags/v1:<previous tag object>`, so a tag that moved after the decision rejects the push and keeps its newer target; the summary records the old and new SHA. Only the `move` job holds `contents: write`; the `decide` job reads the current tag through the GitHub API with the job's token, because its checkout holds no credentials, and mints a metadata-only App token to learn the App's login. Merging a pins-only updater pull request as the repository owner is the approval for this move. A change under any other path of the shared surface keeps `promote-v1.yml` from moving `v1` until a manual promotion has carried that change.

#### Promote v1 by hand

Every other creation or move of `v1`, including the first promotion of a commit that carries other shared changes, is a question to the user (`docs/agents/publication.md`).

1. Select the exact merged commit on `main`, reviewed and with passed CI on `main`. When `promote-v1.yml` reported that `v1` stays, its run summary names the current `v1` and, when a manual promotion is needed, the reason; when it reported that its push was rejected, `v1` moved after its decision, and step 2 shows where to.
2. Read the current remote tag object with `git ls-remote origin refs/tags/v1`. For an annotated tag, the line that ends in `^{}` names the commit, and the other line the tag object.
3. List what changes for callers with `git diff --stat <current v1 commit> <reviewed-merged-commit> -- <paths>`, where `<paths>` are the `paths` in [shared-surface.json](../actions/repository-hygiene/shared-surface.json).
4. Ask the user before the push. Name the current `v1` commit, the commit to promote and the changed shared-surface files, and say that the move changes the later runs of every caller. Only an answer to that question approves the move.
5. The push gate in `docs/agents/delivery.md` covers this tag push.
6. Move the tag with an explicit lease, substituting the reviewed merged commit and the tag object from step 2:

   ```text
   git push --force-with-lease=refs/tags/v1:<previous-tag-object> origin <reviewed-merged-commit>:refs/tags/v1
   ```

   For the first creation, when the remote tag does not exist:

   ```text
   git push --force-with-lease=refs/tags/v1: origin <reviewed-merged-commit>:refs/tags/v1
   ```

   A rejected push means that the tag moved after step 2; start again at step 2.

Later workflow resolutions in all callers use the new version without repository edits; jobs already running keep the commit they resolved.

#### Roll v1 back

A rollback is another one-way move of `v1`, to a previously validated commit. It does not undo the runs that used the newer commit.

1. Select the earlier commit: the old commit in the summary of a `promote-v1.yml` run, or the commit that `v1` resolved to in a caller's run before the move.
2. Ask the user again before the push, naming the current `v1` commit and the earlier commit. The answer to an earlier promotion does not cover the rollback.
3. Read and push as steps 2, 5 and 6 of [Promote v1 by hand](#promote-v1-by-hand) say, with the earlier commit in place of the merged commit.
4. While the shared surface at `v1` differs from the merged commits on `main` in more than `pins.json`, `promote-v1.yml` leaves `v1` in place; the next move is a manual promotion.

### Add a part

A part is a subfolder of `actions/` with the scripts and files of one shared workflow or of several that belong together, loaded through a resource-only composite action `uses: $/actions/<part>`; the parts are grouped by kind once there are many. A part folder holds no `TOOL.md` and no `README.md`: this document describes it. Its consumer kit, the subfolder `consumer-kit/`, holds the caller templates and a `README.md` for consumers.

1. Add the folder with its `action.yml`. The existing ones, such as [dependabot-auto-merge/action.yml](../actions/dependabot-auto-merge/action.yml), output their own path from `github.action_path` and check out nothing. A package manifest in the folder needs an `updates` entry in `.github/dependabot.yml`, or the Dependabot coverage check fails.
2. Add the workflow, `.github/workflows/<name>.yml` with `on: workflow_call`, `permissions: {}` at the top and its rights on its jobs, loading its files only through `$/actions/<part>`.
3. Add the workflow's path and the folder's path to `paths` in [shared-surface.json](../actions/repository-hygiene/shared-surface.json), and to the expected list in `tests/test_ci_policy_shared_surface.py`, which fails while a shared workflow loads a `$/` resource outside the list. Add the workflow's file name to `SHARED_WORKFLOWS` there and in `tests/test_setup_ci_install_by_need.py`, and `CrankyAnt/SharedWorkflows/.github/workflows/<name>.yml: ref-pin` to the shared [zizmor policy](#zizmor-policy). The `ci-policy` check compares the policy with the shared workflows in `shared-surface.json` and names any missing or extra exception.
4. Add the local caller `.github/workflows/<name>-caller.yml`, with the inputs that a consumer passes through `v1`. It calls the workflow by local path, `uses: $/.github/workflows/<name>.yml`, as [Same-repository references](#same-repository-references) says, sets `permissions: {}` at the top and the rights on its one job, uses the shared job id as its job id, and never names `@v1`.
5. Add the part's entry to the [catalogue](../catalogue.json), which `tests/test_ci_policy_shared_surface.py` keeps in step with `shared-surface.json`, and its consumer kit `actions/<part>/consumer-kit/` with the caller template `<name>.yml`, which calls `CrankyAnt/SharedWorkflows/.github/workflows/<name>.yml@v1`, and a `README.md` that explains what the part does, what it needs from a repository and when it applies, with no path, setting or value of this repository's own; a part of several workflows has one template named after the part. Test the templates in the `setup-ci-templates` suite.
6. Give the part a section under [What the workflows do](#what-the-workflows-do) and a row in the parts table, and give its tests a suite in `tests/suites.json`, as `tests/README.md` says. The part's tests read only its own folder and its own workflows; a check that spans the parts goes in the matching `tests/test_ci_policy_*.py` entry file of `ci-policy`.
7. Validate as [Validation](#validation) says.
8. Callers call the workflow at `v1`, so a caller works only once the workflow exists there. The part reaches callers through [Promote v1 by hand](#promote-v1-by-hand) after the merge.

### Change a naming rule

The rules are the naming conventions that the agents of every consumer write by: Conventional Branch for branch names and Conventional Commits for commit subjects and pull request titles. The checks keep a subset of them.

1. Change the convention with the user first; it binds every consumer.
2. Change the check:
   - branch names: the patterns in [check-branch.cjs](../actions/publication-checks/check-branch.cjs). A branch must also match the official regex of the vendored [spec.json](../actions/publication-checks/spec.json), unless the script accepts it before that check, as it does for `research/` and GitHub's revert branches.
   - commit subjects and pull request titles: the rules in [commitlint.config.cjs](../actions/publication-checks/commitlint.config.cjs), which both checks use. [check-commits.cjs](../actions/publication-checks/check-commits.cjs) chooses the commits to check and skips genuine merge commits.
   - the bot authors that skip all three checks: the step `Decide whether the PR author skips the convention checks` in `repository-hygiene.yml`.
3. Update [Publication checks](#publication-checks) here, and the [consumer kit](../actions/repository-hygiene/consumer-kit/README.md) of repository hygiene when it names the changed rule.
4. Add accepted and rejected cases to `test_branch_policy_accepts_the_selected_subset_and_rejects_other_prefixes` in `tests/test_publication_branch.py` or `test_commitlint_accepts_and_rejects_representative_titles` in `tests/test_publication_titles.py`. Provision the commitlint toolchain as `tests/README.md` says, so that the commitlint cases do not skip, and validate as [Validation](#validation) says.
5. This repository's own caller checks the new rule from the pull request on, so that pull request's branch, commits and title must pass it. Callers check it after [Promote v1 by hand](#promote-v1-by-hand).

### Change a hygiene check

Each check is a subcommand of [hygiene.py](../actions/repository-hygiene/hygiene.py) and a step of `repository-hygiene.yml` that runs it with the installed tools and passes the workflow inputs through `env`.

1. Change the check:
   - actionlint: `actionlint` in `hygiene.py`, with the three ignored diagnostics `ACTIONLINT_IGNORE`, `ACTIONLINT_WORKFLOW_IGNORE` and `ACTIONLINT_LABEL_IGNORE`, and the ShellCheck it passes.
   - zizmor: `zizmor` in `hygiene.py` and the shared [zizmor policy](#zizmor-policy). The policy applies only to a repository without its own configuration: the first of `.github/zizmor.yml`, `.github/zizmor.yaml`, `zizmor.yml` and `zizmor.yaml` that exists replaces it whole (`CONSUMER_ZIZMOR_CONFIGS`). This repository has its own `.github/zizmor.yml`, so its own hygiene run never reads the shared policy; only the tests do.
   - Gitleaks: `gitleaks` in `hygiene.py`. The configuration and baseline are the caller's, passed through `gitleaks-config` and `gitleaks-baseline`; this repository's own follow [Change this repository's Gitleaks configuration or baseline](#change-this-repositorys-gitleaks-configuration-or-baseline).
   - Dependabot coverage: [dependabot_coverage.py](../actions/repository-hygiene/dependabot_coverage.py), and [package-ecosystems.json](../actions/repository-hygiene/package-ecosystems.json) for the recognised package files.
   - the link check: [link_check.py](../actions/repository-hygiene/link_check.py).
   - REUSE: `reuse` in `hygiene.py`, and its selection in `decide_changes`.
2. Change the step in `repository-hygiene.yml`. A new input goes under `on.workflow_call.inputs` with a description, `required: false` and an empty default, reaches the step through `env`, and is listed under [Repository hygiene](#repository-hygiene) and in the [consumer kit](../actions/repository-hygiene/consumer-kit/README.md). A new check whose result only a known kind of file can change gets its decision in `decide_changes` of `hygiene.py`, an output of the `changes` step and a step condition on that output, and its tool joins the conditional part of the step that installs the pinned tools; a check that any file can affect runs unconditionally, as Gitleaks does. `test_the_hygiene_workflow_holds_the_contract` and `test_the_hygiene_workflow_runs_each_check_only_when_the_change_can_affect_it` in `tests/test_ci_policy_hygiene_core.py` assert the inputs, the steps and their conditions; `ChangeDecisionTests` in `tests/test_hygiene_changes.py` asserts the decisions, and `WorkflowInvocationTests` in `tests/test_hygiene_workflow.py` parses every `hygiene.py` and `install_tools.py` invocation of the workflow with the scripts' own parsers (`build_parser`), so a step that names an option the script lacks fails the suite.
3. Update the check's rows in [Repository hygiene](#repository-hygiene) and in the [consumer kit](../actions/repository-hygiene/consumer-kit/README.md).
4. Install the pinned tools as `tests/README.md` says, so that the tool-dependent cases do not skip, and validate as [Validation](#validation) says.
5. `repository-hygiene.yml` and the part folder are part of the shared surface, so callers run the changed check after [Promote v1 by hand](#promote-v1-by-hand). A change to the shared zizmor policy does not reach a caller with its own zizmor configuration.

### Add or remove a pinned tool

To add a tool:

1. Add an entry under `tools` in [pins.json](../actions/repository-hygiene/pins.json), keyed by the tool's executable name in lowercase: `install_tools.py` places the executable under that name, and `hygiene.py` looks it up by it. The fields are those of [Pins](#pins). Take each `sha256` from the upstream checksums file and check it against your own download of the asset; with `checksums: null`, compute it from the download. Then let `install_tools.py` download both assets and verify them against the pins:

   ```text
   python actions/repository-hygiene/install_tools.py --pins actions/repository-hygiene/pins.json --platform linux-x64 --tool <name> --destination <temporary directory>
   python actions/repository-hygiene/install_tools.py --pins actions/repository-hygiene/pins.json --platform windows-x64 --tool <name> --destination <temporary directory>
   ```

2. Run the Windows executable from the second destination with `--version` and check that it reports the pinned version.
3. Add the code that runs the tool, and the tool to each step that installs it. The step of `repository-hygiene.yml` that installs the pinned tools names every tool it installs, gated on the checks that need it. In `validate.yml` and `full-recording.yml`, which `ci-policy` keeps equal, a tool that a suite needs is a setup: add its name to the setup loop and the condition of the shard job's step that runs those setups, and to `setups` or `entry_setups` of the suites in `tests/suites.json` that need it. A new hygiene check follows [Change a hygiene check](#change-a-hygiene-check).
4. Add the name to the tool lists of the tests: the expected set in `test_pins_file_validates_and_every_tool_has_a_linux_entry` in `tests/test_repository_hygiene.py`.
5. Validate as [Validation](#validation) says. The changed pins file selects the entry files that read it; it is not a repository-wide `always` path.
6. `pins.json` is part of the shared surface, so the tool reaches callers through [Promote v1 by hand](#promote-v1-by-hand) after the merge. From the merge on, `update-ci-pins.yml` proposes the tool's newer releases.

To remove a tool:

1. Remove every place that runs or installs the tool by name; `git grep -n <name>` finds them:
   - its check in `hygiene.py`, the step that runs the check and its `--tool` in `repository-hygiene.yml`, its setup in `validate.yml`, `full-recording.yml` and `tests/suites.json`, its rows in [Repository hygiene](#repository-hygiene), and the tests of that check. A check that stays but stops using the tool follows [Change a hygiene check](#change-a-hygiene-check).
   - for Gitleaks, also the steps `Install the pinned Gitleaks` and the scan before the push in `update-ci-pins.yml`, `update-publication-spec.yml`, `rebuild-etw-consumer.yml` and the `close` job of `full-recording.yml`.
2. Remove its entry from `tools` in `pins.json`, and its name from the expected set in `tests/test_repository_hygiene.py` and from `TOOL_NAMES` in `tests/test_local_ci_tools_pins.py`.
3. Validate as [Validation](#validation) says.
4. `pins.json` and the hygiene workflow are part of the shared surface, so callers stop running the tool after [Promote v1 by hand](#promote-v1-by-hand).

A tool that pip installs, such as `reuse`, `ruff` or `mypy`, is an entry under `python` in `pins.json`, keyed by its normalized package name, with the fields that [Pins](#pins) describes:

1. To add one, add its entry with `requirement`, `version`, `build` and `packages`, each locked package with the sha256 of each of its distributions that installs on `linux-x64` or `windows-x64` from CPython 3.12 on. Install it with `install_tools.py --tool <name>` into a temporary directory, which fails when a file's sha256 is not in the lock.
2. Add the code that runs it and the tool to each step that installs it, as step 3 of adding a tool says.
3. To remove one, remove every place that runs or installs it by name and its entry under `python`.
4. Validate as [Validation](#validation) says. `pins.json` is part of the shared surface, so the change reaches callers through [Promote v1 by hand](#promote-v1-by-hand). From the merge on, `update-ci-pins.yml` proposes the tool's newer PyPI releases.

### Change this repository's Gitleaks configuration or baseline

`.gitleaks.toml` and `.gitleaks-baseline.json` at the repository root are this repository's own. Its hygiene caller passes them through `gitleaks-config` and `gitleaks-baseline`; the scans before the push in `update-ci-pins.yml`, `update-publication-spec.yml`, `rebuild-etw-consumer.yml` and the `close` job of `full-recording.yml` read them too.

1. Change the rules or allowlists in `.gitleaks.toml`. Keep the allowlist for `.gitleaks-baseline.json`, which holds the accepted findings themselves.
2. To accept findings that are not secrets, write a report of every finding that the current baseline does not cover, with the pinned Gitleaks, after `git fetch --prune origin`. `HEAD --remotes=origin` scans the branch and every pushed branch. Locally, `--all` would also scan branches that were never pushed; CI's `--all` runs in a fresh checkout, where it covers the pushed branches and tags.

   ```text
   gitleaks git --config .gitleaks.toml --baseline-path .gitleaks-baseline.json --log-opts="HEAD --remotes=origin" --redact --report-format json --report-path <temporary file> --no-banner --no-color
   ```

   Review each entry. A real secret never goes into the baseline; report it to the user. Add only the reviewed entries, unchanged, to the array in `.gitleaks-baseline.json`.
3. Check that the same history passes, with the scan above without the report options, and validate as [Validation](#validation) says. The pull request's `hygiene / hygiene` check scans the full history again with the changed files.
4. Neither file is part of the shared surface, and every caller passes its own files, so the change reaches no caller. The updaters check out `main`, so they use the change from the merge on.

### Change an auto-merge rule

1. Change the decision in [auto_merge.py](../actions/dependabot-auto-merge/auto_merge.py): rules 1 and 2 in `dependency_problems` with `AUTOMATIC_UPDATE_TYPES`, `ACTIONS_ECOSYSTEM` and `ACTIONS_OWNER`, rule 3 in `commit_problems`, rule 4 in `required_checks`, and the outcome in `decide`. `read_metadata` and `parse_update_metadata` read the update metadata, and `MERGE_METHOD` holds the merge method.
2. When the rule needs other metadata or rights, change `dependabot-auto-merge.yml` and the caller contract under [Dependabot auto-merge](#dependabot-auto-merge). A right that the calling job must grant also changes the caller template `actions/dependabot-auto-merge/consumer-kit/dependabot-auto-merge.yml`, its tests in the `setup-ci-templates` suite, and every caller already installed.
3. Update the numbered rules and the outcome table under [Dependabot auto-merge](#dependabot-auto-merge), and the summary of the rules in the [consumer kit](../actions/dependabot-auto-merge/consumer-kit/README.md).
4. Add cases to `DecisionTests`, `PullRequestRunTests` and `WorkflowRunTests` in `tests/test_dependabot_auto_merge.py`, and to `MetadataTests` for a change to the metadata reader, and validate as [Validation](#validation) says.
5. `dependabot-auto-merge.yml` and the part folder are part of the shared surface, so the changed rule decides the Dependabot pull requests of callers after [Promote v1 by hand](#promote-v1-by-hand). This repository's own caller runs the workflow by local path, so its own Dependabot pull requests follow the rule on every event after the merge.

### Change a selection rule

1. Change the rule under [Suite selection](#suite-selection) first; `tests/README.md`, the [consumer kit](../actions/suite-selection/consumer-kit/README.md) and every caller build on it.
2. Change the code: a selection rule in [suite_selection.py](../actions/suite-selection/suite_selection.py) (`select`, `bases` and the summary); the CI-definition rule in [ci_definition.py](../actions/suite-selection/ci_definition.py); a rule of the unrecorded inputs in [unrecorded.py](../actions/suite-selection/unrecorded.py).
3. Add the case to the `suite-selection` suite, on its fixture repositories.
4. Validate as [Validation](#validation) says.
5. Callers run the changed rule after [Promote v1 by hand](#promote-v1-by-hand). This repository's `validate.yml` runs it from the pull request on.

### Change the ETW consumer

1. Change the source in `actions/recording/etw-consumer/`, on Windows with the SDK that `global.json` pins.
2. Run `python actions/recording/etw_consumer_build.py build` from the repository root, and commit the source and `etw-consumer/build/` in the same change.
3. A Dependabot update of the package or the SDK changes the source without the build: the `etw-consumer-build` suite fails on it, and with it `validation / validation`. The [rebuild workflow](#the-rebuild-workflow) then commits the rebuilt build and the run's recording additions to that pull request's branch, and the next run of `Validate` checks them.
4. Renew the recorded neutral event files of the tests when the consumer's output changes: run `python -m tests.record_etw_fixture <directory>` on Windows with administrator rights and copy its `classification`, `linked` and `node` directories over those in `tests/fixtures/recording_etw`; naming runs after the directory, such as `node`, records only those.

### Renew the stored strace recordings

The tests of the recording step replay strace recordings stored in `tests/fixtures/recording_strace/`:

- `classification/first` and `classification/second` each hold one full recording of the classification fixture of `tests/test_recording_classification.py`, and `linked` one of its fixture whose checkout is a linked worktree, as `start --full` and `exec` write it: `recording.json`, `units/<unit>.json`, `traces/<unit>.strace` and the lookup reports `lookups/<unit>/<process id>.tsv`. The entry files run on a virtual environment of the recording machine's `python3` that `provide --python` gave the lookup report. The classification tests copy it into a fixture's output directory and run `check`, as they do with the recorded neutral ETW event files in `tests/fixtures/recording_etw/`.
- `commands/<fixture>/<command>.strace` holds the trace of one command of a stored fixture of `tests/test_recording_check.py` or `tests/test_recording_full.py`, recorded under the wide trace set, and `commands/<fixture>/<command>.lookups/` the lookup reports of its Python processes, which ran on such a virtual environment. These tests run the recording step against a stand-in `strace`, `tests/strace_stand_in.py`, first on the fixture's `PATH`: it records its arguments, runs the command, exits with the command's exit code and writes the command's stored trace with only the lines of the calls in its `-e trace=` set, and its stored lookup reports into the directory that `SHARED_LOOKUPS` names.
- In the stored files the fixture's base directory reads `${BASE}`. In a command's trace its bytecode cache reads `${PYTHONPYCACHEPREFIX}`, and its temporary directory `${TMPDIR}` wherever the command uses it; no stored command uses it yet. The replay puts the paths of its own run in their place; the tokens are listed in `TOKENS` of `tests/strace_stand_in.py`. The lines of opens, stats and probes of system paths, which `check` skips, are left out, and so are the lookup report's lines of directories outside the base directory; no stored file names the checkout that recorded it.

Renew the recordings when `strace`'s output changes, or when a stored fixture's files or commands or the classification fixture change:

1. On Linux with `strace`, run `python3 -m tests.record_strace_fixture <directory>` from the repository root. It runs every command with the standard system `PATH`, a `HOME` inside the fixture and no inherited `PWD`, and fails when a recording names the home directory, the user name or the checkout.
2. Replace `tests/fixtures/recording_strace/` with the contents of `<directory>`, and commit them with the change that needs them.

### Renew the standard-library names

`recording/stdlib_module_names.json` holds the union of `sys.stdlib_module_names` over the Python versions that the shared CI supports: from the floor that [Python language step](#python-language-step) names, the oldest system `python3` of the images that shared jobs and steps run on, through the newest CPython feature release, prereleases included. Renew it when a CPython feature release or prerelease comes out or the floor moves:

1. Run `python3 actions/recording/stdlib_names.py <interpreter> ...` with one interpreter of each of those versions; it writes the file with the versions it read.
2. Commit the file. It belongs to the shared digest, so the change runs every entry file of the jobs that record.

### Change a selection workflow

1. Change `.github/workflows/suite-selection.yml` or `.github/workflows/suite-verdict.yml`. A new input goes under `on.workflow_call.inputs` with a description, its type and, unless it is required, a default; it reaches the step through `env`. A new output of the selection goes under `on.workflow_call.outputs`, mapped to an output of the `select` job and from there to a step output. Both workflows keep `permissions: {}` at the top; the `select` job holds `actions: read`, `contents: read` and `pull-requests: read` for the push lookup, and the `validation` job of `suite-verdict.yml` holds `contents: read`. Both load the part only through `$/actions/suite-selection`.
2. When the step's command changes, change the selection's command line in `suite_selection.py` and under [Entrypoint](#entrypoint) with it.
3. Change the contract tests in `tests/test_suite_selection_workflow.py` and `tests/test_suite_selection_verdict_workflow.py`.
4. When an input or output changes, change the caller template `suite-selection/consumer-kit/validation.yml` and the [consumer kit](../actions/suite-selection/consumer-kit/README.md)'s explanation and their tests in the `setup-ci-templates` suite.
5. Validate as [Validation](#validation) says. The pull request's `hygiene / hygiene` check runs actionlint and zizmor over the changed workflow, and `validate.yml`, which calls both workflows by local path, runs the changed workflow in the pull request.
6. Callers run the changed workflow after [Promote v1 by hand](#promote-v1-by-hand). A changed input with a default, or a new output, needs no change in a caller; a new required input needs a change in every caller before the promotion.

### Change the suite-list format

1. Change the format under [Suite list](#suite-list): the field, what it holds and what makes the suite list invalid.
2. Change `parse_suite_list`, `Suite` and `Suite.entry` in `suite_selection.py`, and `render_suite_list` where the field is rendered, so that a suite list parses and renders back the same.
3. Add the case to `SuiteListTests` in `tests/test_suite_selection.py`.
4. Change `tests/suites.json` where the format change touches it, and the procedures in `tests/README.md` where they name the field. Change [The suite list](../actions/suite-selection/consumer-kit/README.md#the-suite-list) of the consumer kit.
5. Validate as [Validation](#validation) says.
6. Callers parse the changed format after [Promote v1 by hand](#promote-v1-by-hand). A field that a consumer's suite list must hold from then on needs the consumer's suite list changed before the promotion; an optional field needs nothing.

### A skipped suite in a consumer

A consumer that sees a run skip a suite that had to run reports a read that the recording should see by its rules as an issue here, with the summary of the `select` job of that run.

1. Read the summary in the issue: the changed files, each suite with its decision and reason, and the files read by no suite.
2. Find the read the stored lists lack. In a clone of the consumer, compare the suite's stored lists with the paths the suite reads, and check what the missed file selects:

   ```text
   python <SharedWorkflows>/actions/suite-selection/suite_selection.py select --root . --suites .github/suites.json --changed-file <the file whose change was missed>
   ```

3. Resolve it at the right place:
   - a read no recording sees, such as an accepted miss of the [recording step](#recording-step): state it as an extra read of the suite, in `extra_paths`, `entry_extra_paths`, `extra_directory_memberships` or `extra_existence_reads`. Use `always` only when evidence shows that every suite reads the path, in the consumer's suite list.
   - a read the recording should see by its rules but does not: a defect of the recording step. Add the recording's output to the issue, then follow [Change a selection rule](#change-a-selection-rule).
4. Once the correction reaches the consumer, check that its `select` summary shows the suite as `run` for a change to that file.

### Act on an updater pull request or a failed updater run

Two updaters open review pull requests with the token of the [GitHub App](#github-app): the [pins updater](#the-pins-updater) from `chore/update-ci-pins`, which changes only `pins.json`, and the [specification updater](#the-specification-updater) from `chore/update-conventional-branch-spec`, with the title `chore: update Conventional Branch specification to <version>`, which changes only `spec.json`, `spec-metadata.json` and `tests/fixtures/publication_checks/specification-cases.json`.

A specification update pull request is reviewed before it merges:

1. Review the specification for semantic changes, not just its version or checksum: compare `spec.json` with the vendored version, and read the versioned document that `spec-metadata.json` names as `source`.
2. Read the diff of `tests/fixtures/publication_checks/specification-cases.json`. Each case records whether the official regex of the new version accepts a fixed branch name, so a changed `matches` value is a rule change upstream. The fixture shows the change; it approves nothing.
3. Compare the changes with the local subset in `check-branch.cjs`. A branch that the subset accepts but the new regex rejects fails every caller's check after the promotion. A change to the subset follows [Change a naming rule](#change-a-naming-rule) in a pull request of its own; a commit added to the review branch is lost when the next updater run pushes that branch again.
4. Merge the pull request when its CI is green and the subset still holds. A closed pull request leaves the vendored version in place, and the next weekly run proposes the version again. `spec.json` is part of the shared surface, so callers check against the new specification after [Promote v1 by hand](#promote-v1-by-hand). A merged pins update moves `v1` by itself, as [Automatic v1 move](#automatic-v1-move) says.

For a failed run, `gh run list --workflow <update-ci-pins.yml or update-publication-spec.yml>` lists the runs and `gh run view <run-id> --log-failed` prints the log of the failed step. The next step depends on the step that failed:

- `actions/create-github-app-token`, the first step of `propose`: check the repository variable `PUBLICATION_CHECKS_APP_CLIENT_ID`, the repository secret `PUBLICATION_CHECKS_APP_PRIVATE_KEY`, and the App's installation with Contents and Pull requests read and write, as [GitHub App](#github-app) describes. Changing any of them is a question to the user (`docs/agents/publication.md`).
- The check or refresh step of the pins updater, `Check the upstream releases of every pin` or `Refresh the pins file`: a checksum mismatch is listed in the run summary under "Checksum failures, nothing proposed", with the tool, the version, the asset and both sha256 values. Nothing from that release is pinned by hand; report the mismatch to the user, and the next weekly run checks again. Exit code 2, with an `::error::` line that names the problem, means that `pins.json` on `main` does not satisfy `pins.schema.json`; correct it in a pull request, which reaches callers through [Promote v1 by hand](#promote-v1-by-hand). An unreachable upstream does not fail the run: it is listed under "Unreachable upstreams, pins left unchanged".
- The check or refresh step of the specification updater, `Check the official Conventional Branch specification` or `Refresh the vendored specification`: `update_spec.py` ends without an error for every outcome it handles. A newer version that differs from its versioned document or whose regex does not compile in Node.js leaves the step green: its log says `Specification refresh skipped:` with the reason, and the run proposes nothing. For a difference from the versioned document, the next weekly run checks again; report a regex that does not compile to the user. The same version with changed content logs `Specification refresh needs review:`; report it to the user. A site outage logs `Specification refresh skipped:` with the error and needs nothing. The step itself fails only on an error that `update_spec.py` does not handle; record it as an issue with the run link.
- `Check the changed files`: `Unexpected changed file: <path>` means the refresh changed a file outside those it may change, a defect in the updater script or the workflow. Record it as an issue with the run link; the allowed list stays.
- `Install the pinned Gitleaks`: the download failed, or its sha256 differs from the pins file. Nothing was pushed. Run the updater again after a download failure; report a sha256 mismatch to the user.
- The scan before the push, `Scan the commit before pushing` of the pins updater or `Scan unpublished commits before pushing` of the specification updater, each of which scans only the commits that no remote branch holds: Gitleaks reported a finding. Nothing was pushed. The redacted log names the rule, the file and the line. A false positive follows [Change this repository's Gitleaks configuration or baseline](#change-this-repositorys-gitleaks-configuration-or-baseline); a real secret goes to the user.
- `Push the review branch`: the lease rejected the push because the review branch changed between the fetch and the push. Read the branch and its open pull request (`gh pr list --head <branch>`). Run the updater again when that change may be replaced; otherwise merge or close that pull request first. A push refused for permission points to the App's Contents permission.
- `Create or update the review pull request`: the branch was pushed, but the pull request was not created or edited. Check the App's Pull requests permission and run the updater again; it pushes the branch again under a new lease and creates or edits the pull request. A pins pull request opened by hand does not come from the App, so `promote-v1.yml` does not move `v1` after its merge.
- Any other step, such as `Configure the GitHub App identity` or the commit step: record it as an issue with the run link.

To run an updater again, `gh run rerun <run-id> --failed` repeats the failed jobs of that run, and `gh workflow run update-ci-pins.yml -f apply=true -f cooldown_days=7` or `gh workflow run update-publication-spec.yml -f apply=true` starts a new run that proposes; without `apply=true` a run only previews. The scheduled run on the next Monday also tries again.

### Act on the full recording's pull request or a failed `close`

The [full recording](#full-recording) opens or updates the pull request `chore: update the recorded path lists` from `chore/update-recorded-lists` with the token of the [GitHub App](#github-app). In this repository it changes only `tests/suites.json`, and its body is the report of `apply`.

1. Read the report: it names each entry file whose lists `apply` kept, with the reason.
2. Merge the pull request when its CI is green. A commit added to the review branch is lost when the next full recording pushes that branch again.

For a failed `close`, `gh run view <run-id> --log-failed` prints the log of the failed step:

- `Check the changed files`: `Unexpected changed file: <path>` means that `apply` changed a file other than the suite list. Record it as an issue with the run link.
- The App token, `Push the review branch` or `Create or update the review pull request`: the step `Name the App setting this run would need` fails the run. Changing any App or repository setting is a question to the user (`docs/agents/publication.md`); the joined result is the artifact `full-recording-joined`. A push that the lease rejected means that the review branch changed after the run read it.
- `Scan the commit before pushing`: Gitleaks reported a finding and nothing was pushed. A false positive follows [Change this repository's Gitleaks configuration or baseline](#change-this-repositorys-gitleaks-configuration-or-baseline); a real secret goes to the user.
- Any other step: record it as an issue with the run link.

`gh workflow run full-recording.yml --ref main` starts a new run in any week.

## Reference

### Pins

[pins.json](../actions/repository-hygiene/pins.json) holds every exact pin the shared workflows use: per tool its upstream repository, version, the name of the upstream checksums asset (or `null`), and one entry per platform with the asset name, the executable's path inside the archive and the asset's sha256; plus the Node.js version. [pins.schema.json](../actions/repository-hygiene/pins.schema.json) documents the format and [pins.py](../actions/repository-hygiene/pins.py) enforces it. The fields of a tool entry:

- `repository`: the GitHub repository, `owner/name`. Its release tags must be `v{version}`; `pins.py` builds every download URL from that tag.
- `version`: the exact `major.minor.patch` release.
- `checksums`: the name of the upstream checksums asset with `{version}` in place of the version, such as `actionlint_{version}_checksums.txt`, or `null` when the release publishes none.
- `platforms`: a `linux-x64` entry for CI and a `windows-x64` entry for installs on Windows, each with `asset` (the release asset name with `{version}`), `binary` (the executable's path inside the archive) and `sha256` (the asset's sha256). The `windows-x64` asset is a `.zip` and its `binary` is `<name>.exe`.

`python` holds the tools that pip installs, keyed by their normalized package name: `reuse`, `ruff` and `mypy`. An entry holds `requirement` (the tool with its extras, such as `reuse[charset-normalizer]`), `version`, `build` (the packages that build the tool from its sdist) and `packages`. `packages` locks every package of the closure, the tool and its build packages included, with its version and the sha256 of each of its distributions that installs on `linux-x64` or `windows-x64` from CPython 3.12 on.

[install_tools.py](../actions/repository-hygiene/install_tools.py) downloads the assets of one platform for the tools named with `--tool`, verifies every sha256 before extracting anything and places the executables in one directory. The hygiene workflow, the setups of `validate.yml` and `full-recording.yml`, the Python language step and the pre-push scan of the two updaters use it. `repository-hygiene.yml` and `update-publication-spec.yml` take their Node.js version from the composite action's output; the `node` setup of `validate.yml` and `full-recording.yml` reads it from the pins file under the recording.

For a tool under `python`, `install_tools.py` creates a virtual environment of the interpreter that runs it in `<destination>/<name>-venv`. pip installs the build packages there first and then the other locked packages with `--require-hashes`, `--no-deps` and `--no-build-isolation`, and the installer copies the tool's console script into the destination. A file whose sha256 the lock lacks stops pip, and the installer removes the environment.

### The pins updater

`update-ci-pins.yml` runs weekly on Monday at 17:36 UTC and by hand through `workflow_dispatch`; manual runs preview unless `apply: true`, and take `cooldown_days` (default 7) as the minimum release age. [update_pins.py](../actions/repository-hygiene/update_pins.py) proposes, per tool, the newest stable GitHub release that is at least 7 days old and newer than the pin, and for Node.js the newest release of the pinned major line that is at least 7 days old; a younger release waits, also on manual runs, and a new Node.js major line is never proposed. A proposal refreshes every platform entry of that tool: each asset is downloaded, its sha256 is taken from the upstream checksums file and checked against the download, or computed from the download when the upstream publishes none (ShellCheck and zizmor), which the pull request body states per tool. A checksum mismatch fails the run, so that run proposes nothing, not even for the other tools; an unreachable upstream leaves that pin unchanged and is listed in the run summary.

For a tool under `python`, the updater proposes the newest stable PyPI release under the same rule. pip, in a temporary virtual environment of the updater's interpreter, resolves the tool's requirement at that version and the build requirements from the `pyproject.toml` of its sdist, with `--uploaded-prior-to` where that pip has it. The updater locks each resolved package with the sha256 values that PyPI lists. A resolved package with a distribution younger than the cooldown leaves the pin unchanged and is listed in the summary.

One run proposes all its changes in one pull request from `chore/update-ci-pins`, updated in place with `--force-with-lease` while it is open, with the token of the [GitHub App](#github-app). The pull request changes only `pins.json`; any other changed file stops the run. The pinned Gitleaks scans the commit before the push.

### The specification updater

`update-publication-spec.yml` checks the official latest endpoint of the Conventional Branch specification weekly on Monday at 17:06 UTC. Manual dispatch previews by default; `apply: true` proposes a pull request. A site outage leaves all files unchanged and creates no pull request that week. Before the updater force-pushes an existing review branch, it reads that branch's exact remote tip and uses it in `--force-with-lease`, so a later scheduled run cannot replace a newer review branch without first reading it.

A newer version must match its versioned upstream document and compile in Node.js. The fixture of observed cases records observed official-regex outcomes for a fixed corpus, so the pull request exposes rule changes; it does not approve them or change the local subset. Same-version content drift is reported for review without replacement.

This repository's `.github/dependabot.yml` checks weekly for changes to pinned GitHub Actions, the npm lockfile of the publication checks, and the NuGet packages and the .NET SDK (`nuget` and `dotnet-sdk`) of `actions/recording/etw-consumer`. Commitlint dependency updates are reviewed in this repository; no consumer lockfile needs refreshing. Dependabot pull requests follow the rules under [Dependabot auto-merge](#dependabot-auto-merge); scheduled update pull requests never auto-merge, and no scheduled pull request moves `v1` except a merged pins-only update.

### GitHub App

The user creates one GitHub App and installs it on this repository; a consumer that runs the full recording gets an installation when it is set up again, and each installation is a question to the user. The App has repository **Contents: read and write** and **Pull requests: read and write**, and no other requested permissions. GitHub's mandatory metadata read access is implicit. No account permission or webhook subscription is needed.

The repository variable `PUBLICATION_CHECKS_APP_CLIENT_ID` holds the App Client ID and the repository secret `PUBLICATION_CHECKS_APP_PRIVATE_KEY` its private key. The user supplies these credentials; the workflows create neither the App nor its repository credentials or settings, and changing any of them is a question to the user. The App has four users. Three propose changes as pull requests: the [pins updater](#the-pins-updater) (`update-ci-pins.yml`), the [specification updater](#the-specification-updater) (`update-publication-spec.yml`) and the [full recording](#full-recording) (`full-recording.yml`). The [rebuild workflow](#the-rebuild-workflow) (`rebuild-etw-consumer.yml`) commits the rebuilt ETW consumer to a Dependabot pull request's branch. Each mints a token through SHA-pinned `actions/create-github-app-token`, scoped to the current repository and those two permissions, the rebuild workflow to `contents: write` alone, only when it has a change to propose; the installation token lets the pull request or the push trigger CI, and the normal workflow token stays read-only. `promote-v1.yml` mints only a metadata-only token, to learn the App's login, as [Automatic v1 move](#automatic-v1-move) says. See [GitHub App tokens in Actions](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/making-authenticated-api-requests-with-a-github-app-in-a-github-actions-workflow).

### Runner choice

A job of this repository's CI that is off the critical path and needs only the system `python3`, `git`, `gh` and pinned binaries runs on `ubuntu-slim`, with the system `python3` and no `setup-python`: the jobs of `dependabot-auto-merge.yml`, `promote-v1.yml`, `update-ci-pins.yml` and `update-publication-spec.yml`, the jobs `plan` and `push` of `rebuild-etw-consumer.yml`, the verdict job `validation`, and the closing job `close` of the full recording. Those jobs cost less there. The hygiene job runs on `ubuntu-slim` only when a measurement shows it finishing within one minute there, so that it never bills more minutes on a smaller runner; the measurement so far keeps it on `ubuntu-26.04`.

### zizmor policy

The shared policy, [zizmor-policy.yml](../actions/repository-hygiene/zizmor-policy.yml), states the pinning rule: `actions/*`, the shared workflow references listed by `shared-surface.json` and the shared actions that the consumer kits call, such as `CrankyAnt/SharedWorkflows/actions/recording`, may use a moving ref; every other external reference needs a hash pin. The `ci-policy` check rejects missing or extra exceptions for shared workflows and shared actions. It applies to a caller without a zizmor configuration of its own. This repository's own `.github/zizmor.yml` requires full SHA pins for every external action reference; a `$/` reference resolves to the running commit, as [Same-repository references](#same-repository-references) says, and needs no pin.
