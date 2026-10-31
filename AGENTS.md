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

When in doubt about a place, ask the user.

### Exceptions

- `docs/maintaining.md` stays at the top level of `docs/`: the maintenance documentation of the shared parts.
- `tests/README.md` stays beside the suites it describes.
- Each consumer kit's `README.md` under `actions/*/consumer-kit/` and `actions/repository-ruleset/README.md` stay with the shared part they explain; consumers read them at `v1`.

## Project overview

Every tracked top-level folder whose name does not start with a dot gets one line: the path and at most one short clause. No explanation, rules, status, subfolders, nested lists or file pointers. A new top-level folder adds its line; a removed one removes its line.

- `actions/`: the shared parts with their shared steps and consumer kits
- `docs/`: maintenance documentation, ADRs and agent configuration
- `tests/`: the suites of the shared parts and their fixtures

## Testing instructions

One line per validation or test command and per CI workflow the repository runs. Whoever adds a workflow or caller adds its line.

- `wsl -d Ubuntu-26.04-CI -u root -- ci-max python3 -m unittest -v <entry file>`: one Linux entry file, with its suite's `command` from the suite list `tests/suites.json`, on the hosted runner's tool versions
- `python -m unittest -v <entry file>`: one entry file of the Windows suites `windows` and `etw-consumer-build`, on Windows
- `repository-hygiene-caller` (local path): the convention checks of branch names, commit subjects and pull-request titles, the credential scan, workflow validation, Dependabot coverage and the Markdown link check; `reuse lint` only with a `REUSE.toml`, which this repository lacks
- `dependabot-auto-merge-caller` (local path): auto-merge of qualifying Dependabot pull requests
- `validate` (local path): the validation caller; selects the units from the stored lists of the suite list, runs them in Linux shards and one Windows job under the recording step and reports the verdict `validation / validation`
- `full-recording`: records every entry file every two weeks and proposes the changed stored lists and duration estimates as a pull request
- `repository-hygiene`: the shared workflow of repository hygiene, called by `repository-hygiene-caller` and by consumers at `v1`
- `dependabot-auto-merge`: the shared workflow of Dependabot auto-merge, called by `dependabot-auto-merge-caller` and by consumers at `v1`
- `suite-selection`: the shared workflow of the suite selection, called by `validate` and by consumers at `v1`
- `suite-verdict`: the shared workflow of the verdict, called by `validate` and by consumers at `v1`
- `promote-v1`: moves `v1` after a pins-only commit on `main` whose CI passed
- `rebuild-etw-consumer`: rebuilds the committed ETW consumer for a Dependabot pull request after `Validate` and commits it to its branch
- `update-ci-pins`: proposes newer tool pins as a pull request, weekly
- `update-publication-spec`: proposes a newer Conventional Branch specification as a pull request, weekly

## Dev environment tips

- Linux entry files run in the WSL distribution `Ubuntu-26.04-CI` through `ci-max`, from a fresh clone of the Windows checkout made at the start of a session, checked with `git rev-parse HEAD` and removed afterwards, as the distribution's guide says (`wsl -d Ubuntu-26.04-CI -u root -- cat ~runner/CI-PROFILE.md`). Uncommitted changes run from the Windows tree instead.
- Recordings from that distribution are never stored: stored lists and duration estimates come only from hosted runs.
