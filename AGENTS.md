# SharedWorkflows

Shared Workflows hosts the shared workflows, shared steps and tools that consumers call at the tag `v1`. It develops no product of its own and tests its shared workflows and shared steps by local path.

## Agent skills

### Issue tracker

Issues are tracked in GitHub Issues of `CrankyAnt/SharedWorkflows`. See `docs/agents/issue-tracker.md`.

### Triage labels

Type, Triage, Ready, Status and Resolution labels with the prefixes `type:`, `triage:`, `ready:`, `status:` and `resolution:`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `GLOSSARY.md` and `docs/adr/` at the repository root. See `docs/agents/domain.md`.

### Other configuration

Research, delivery paths including direct-main, publication rules and CI: see `docs/agents/`.

Maintenance of the shared parts: start from [Recurring tasks](docs/maintaining.md#recurring-tasks) in `docs/maintaining.md`.

## Repository standard

Documentation goes to one of three places:

- `docs/guides/`: for the people who use the product.
- `docs/development/`: for whoever develops or extends the product, agents included: how a part works, how a developer uses it, how to extend it.
- `docs/sources/`: for the same readers, material from outside the repository, kept as `docs/agents/research.md` says.

The product's own technical reference, such as its APIs, commands, configuration or data model, lives with its audience in `docs/guides/` or `docs/development/`.

Each document in `docs/guides/` and `docs/development/` is one [Diataxis](https://diataxis.fr/compass/) type: tutorial, how-to guide, reference or explanation. Why a decision holds is an ADR; a term goes to `GLOSSARY.md`.

The top level of `docs/` holds only `adr/`, `agents/`, `guides/`, `development/`, `sources/`, `out-of-scope/` and the exceptions below. Inside `guides/` and `development/`, a folder holds one broad subject, such as `docs/development/skills/` or `docs/development/modules/`, never a single document. It comes with its first document, when that document clearly passes the Diataxis test and has no standard place yet.

Temporary documents, such as research, notes and plans, stay out of `docs/`.

A place outside this standard, such as another top-level folder in `docs/`, is an exception. It enters `### Exceptions` as one line with its path and its reason, only after the user approves it; a change that ends the exception removes its line.

A rule for the whole repository goes into `AGENTS.md` under the [agents.md](https://agents.md/) heading that fits its subject, such as `## Code style`, `## Security considerations` or `## PR instructions`, or under a heading of its own when none fits. A rule on the subject of a file in `docs/agents/`, such as a delivery rule or a CI choice, goes into that file.

When in doubt about a place, ask the user.

### Exceptions

- `docs/maintaining.md` stays at the top level of `docs/`: the maintenance documentation of the shared parts.
- `tests/README.md` stays beside the suites it describes.
- Each consumer kit's `README.md` under `actions/*/consumer-kit/` and `actions/repository-ruleset/README.md` stay with the shared part they explain; consumers read them at `v1`.

## Project overview

Every tracked top-level folder whose name does not start with a dot gets one line: the path and at most one short clause that names the folder's purpose, not its contents. No explanation, rules, status, subfolders, nested lists or file pointers. A new top-level folder adds its line; a removed one removes its line; a change that makes a clause untrue rewrites it in the same change.

- `actions/`: the shared parts
- `docs/`: the repository's documentation
- `LICENSES/`: license texts
- `tests/`: the tests of the shared parts

## Testing instructions

One line per validation or test command and per CI workflow the repository runs. A change that adds, removes or renames a command or workflow, or changes what it validates or tests, adds, removes or rewrites its line in the same change.

- `wsl -d Ubuntu-26.04-CI -u root -- ci-max python3 -m unittest -v <entry file>`: one Linux entry file, with its suite's `command` from the suite list `tests/suites.json`, on the hosted runner's tool versions
- `python actions/pipeline/plan.py schema --check`: the assembled schema of the configuration file against its base and fragments; without `--check` it writes the schema
- `python -m unittest -v <entry file>`: one entry file of the Windows suites `windows` and `etw-consumer-build`, on Windows
- `automation-caller` (local path): the automation caller; on a completed run of `CI`, weekly, by hand and on an opened issue it runs the parts that `.github/ci-config.yml` switches on: Dependabot auto-merge, the additions workflow, which commits the recorded additions of a failed `CI` run onto its pull request's branch, once per push, or comments with the manual route, the full recording, and issue-title normalisation on the fixture category file `tests/fixtures/issue_title/categories.json`
- `ci` (local path): the pipeline caller; checks the configuration file `.github/ci-config.yml`, runs repository hygiene, whose checks of a consumer's fixed callers, CI guide, local workflow register with its switch-over check and generated Dependabot configuration pass here without a check, the free job on `windows-2025` with the setups `python-3.13` and `python-3.14` and the fixture script `tests/fixtures/custom/pass.sh`, the parts `python-verify` and `python-test` on the fixture project `tests/fixtures/python_tests`, `dotnet-test` on the fixture project `tests/fixtures/dotnet_test` and, with `console-runner`, on the fixture console test program `tests/fixtures/dotnet_console`, `node-test` on the fixture project `tests/fixtures/node_test`, and `suite-selection` on the suite list `tests/suites.json` with the pinned tool `gitleaks`, which selects the units and runs them in Linux shards and one Windows job under the recording step, and reports the verdict `ci / verdict`
- `release-caller` (local path): the release caller; calls the release workflow on a pushed tag and on a start by hand, where this repository's configuration file states no release part, so the run publishes nothing and passes
- `suite-jobs`: the shared workflow of the suite jobs, called by `pipeline` and `automation`, so consumers reach it at `v1` through them: the Linux shards and the Windows job that run the setups of the suite list, the pinned tools that the configuration file names and the selected entry files under the recording step, and the full recording's jobs
- `pipeline`: the shared workflow of the pipeline, called by `ci` and by consumers at `v1`
- `automation`: the shared workflow of the parts that write, called by `automation-caller` and by consumers at `v1`: Dependabot auto-merge, issue-title normalisation, the additions workflow, and the full recording, which records every entry file every two weeks and proposes the changed stored lists and duration estimates as a pull request that merges itself once its required checks have passed
- `release`: the shared workflow of the release part `github-release`, called by `release-caller` and by consumers at `v1`; publishes a GitHub release only for a pushed tag that matches a configured pattern, carries a valid signature of a key that the default branch allows and still names the verified commit when the release is created
- `sync`: the shared workflow of the sync, called at `v1` by the private management repository alone through `actions/sync/caller.yml`. The suite `sync` tests file and settings checks and finding issue recovery against a GitHub API stand-in. The CI policy suite checks the workflow's permissions and the success and failure steps
- `measurement`: the shared workflow of the CI goal measurement, called at `v1` by the private management repository alone through `actions/measurement/caller.yml`. The suite `measurement` tests the reports and finding issue recovery against a GitHub API stand-in. The CI policy suite checks the workflow's permissions and the success and failure steps
- `promote-v1`: on a push to `main` whose changes compared with `v1` stay in the read class, or that is the updater's pins-only pull request merged by the owner, moves `v1` after the commit's CI on `main` passed; for any other change it states the question for a promotion by hand
- `rebuild-etw-consumer`: rebuilds the committed ETW consumer for a Dependabot pull request after a failed `CI` and commits it with the run's recorded additions to its branch, once per push, or comments with the manual route when a failed or cancelled job leaves a run it acts on without that commit
- `update-ci-pins`: proposes newer tool pins as a pull request, weekly
- `update-publication-spec`: proposes a newer Conventional Branch specification as a pull request, weekly

## Dev environment tips

- Linux entry files run in the WSL distribution `Ubuntu-26.04-CI` through `ci-max`, from a fresh clone of the Windows checkout made at the start of a session, checked with `git rev-parse HEAD` and removed afterwards, as the distribution's guide says (`wsl -d Ubuntu-26.04-CI -u root -- cat ~runner/CI-PROFILE.md`). Uncommitted changes run from the Windows tree instead.
- After a pins update lands on `main`, the local tools follow it before the next local run, as the distribution's guide says (`wsl -d Ubuntu-26.04-CI -u root -- cat ~runner/CI-PROFILE.md`).
- Recordings from that distribution are never stored: stored lists and duration estimates come only from hosted runs.
