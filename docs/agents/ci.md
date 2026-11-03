# CI

## Principles

Every agent that adds or changes a check, a suite or a workflow holds to these principles; how it builds the change is its own choice.

- **Purpose.** Every check guards a condition in the repository as it is now: seen red on that condition, green otherwise. An obsolete, flaky or fully covered check goes; a test that writes outside what its setup provides is flaky, is no input of selection, and is fixed or goes.
- **Selection.** A check runs only on changes that can change its result: what it reads, the files it looks for and does not find, and how it runs (command, setup, OS, tool versions, the CI definition that starts it). It runs on every change only when every change can, as the secret scan does. Prose that no code reads starts only light checks; data that code reads starts that code's checks. A test sees only the setup it states and reads only what its result depends on, so neither its job grouping nor an unrelated change shapes it.
- **Sound selection.** Selection costs far less than it saves, its upkeep included, or it is not used. What it cannot observe selects more, never less; each remaining gap is a named accepted miss. A mapping of tests to the files they read is generated from what runs read and checked by every run that uses it; only a read no run can see is stated by hand, by the change that adds it. Runs only add to it; a periodic full run of every test is the safety net and alone removes from it, and only on complete evidence that no test was skipped. A rule that selects a check by its fixed inputs, such as a link check on Markdown changes, is part of that check and tested with it.
- **Running everything.** A test with unknown inputs runs on every change until they are known. Everything runs when the changed files are unknown or a change reaches what decides every result, such as the rules for what counts as a read; any other change to the selection tooling runs only the tests that read it, and an invalid definition fails instead of running everything.
- **Reuse.** Only a passed result with exactly the same inputs, the way it runs included, written by a run of the same change, is reused by its later heads; a manual re-run reuses nothing. Accepted risk: a reused flaky pass does not rerun until its inputs change. A push whose tree passed on its pull request runs only the every-change checks; a push whose pull request was tested on an older base runs only what both the change and the newer base can affect. Accepted risk: a conflict through a read outside the mapping turns the default branch red until the next change fixes it. A newer push cancels the older run of the same pull request; a default-branch run is never cancelled, and a cancelled result is never reused.
- **One verdict.** One verdict gates the merge: it fails when selection failed, or a started job failed or was cancelled; a job that selection skipped counts as not run.
- **Jobs.** Prefer fewer, larger jobs: each pays a fixed cost to start, check out and set up, plus rounding where billing rounds per job. Split long work by measured duration, not by test count; run a language check in a job that has its toolchain. Run checks that an event without a code change (such as a title edit) can need apart from the test suites, so such an event does not restart the suites and neither failure blocks the other. Use another platform only where the result can differ there. Pin the OS label of every job that installs a toolchain or runs tests, so an OS release never arrives unannounced; use a cheaper runner, pinned or not, only for toolchain-free jobs off the critical path, and for real work only after a measurement shows it is fast enough there. Do the same work once per run and share it within that run only.
- **Tests.** Each suite has one subject and every test a place in one; the entry file is the smallest unit of selection. A test starts the real command for what only a real process shows and for each public command's main path; where that start is expensive, every other case runs without a new process.
- **Changing CI.** CI is the only gate: before a push, run the narrow validation the change needs and the delivery configuration's pre-push checks, never a local copy of CI's selection. A replacing part of CI replaces the old one at once.
- **Time and scaling.** A small change finishes CI in about a minute, or as fast as its unavoidable build allows, and most runs within a few minutes; this is judged from measured runs on the CI's own runners, never from estimates. An agent that changes CI compares recent measured times with it. Signs that a repository needs more: a rising share of tests that run on every change, a job that grows with the tests and cannot be split, billed minutes far above used minutes, estimates drifting from measurements, runs falling back to everything. When CI misses the target, first measure where the time goes, apply these principles, then look in the catalogue for a shared part whose own conditions say it fits: cheap and low-risk first, not by size of gain, since not doing work gains most. If that is not enough, put a restructuring to the user as a separate question; do not start one.

## Shared Workflows

This repository calls its own shared workflows and shared steps by local path, `uses: $/.github/workflows/<file>` and `uses: $/actions/<part>`, from its callers and its validation caller, as [Calling the workflows](../maintaining.md#calling-the-workflows) says. It uses these shared parts:

| Shared part | Caller | Check | Inputs |
| --- | --- | --- | --- |
| Repository hygiene | `repository-hygiene-caller.yml` | `hygiene / hygiene` | `gitleaks-config: .gitleaks.toml`, `gitleaks-baseline: .gitleaks-baseline.json` |
| Dependabot auto-merge | `dependabot-auto-merge-caller.yml` | `auto-merge / auto-merge`, `auto-merge / merge` | none |
| Suite selection | `validate.yml` | `validation / validation` | on `select`: `suites: tests/suites.json`, `language-steps: python`, `runner-labels: linux=ubuntu-26.04 windows=windows-2025`, `setup-versions: python=3.13` |
| Python language step | `validate.yml`, shard 1 | none | none |

`docs/maintaining.md` describes what each part runs:

- Repository hygiene: [Repository hygiene](../maintaining.md#repository-hygiene), with the floor and the [publication checks](../maintaining.md#publication-checks).
- Dependabot auto-merge: [Dependabot auto-merge](../maintaining.md#dependabot-auto-merge).
- Suite selection: [Job layout](../maintaining.md#job-layout) and [Suite selection](../maintaining.md#suite-selection).
- Python language step: [Python language step](../maintaining.md#python-language-step).

The hygiene caller and the validation caller also run on the [`dependabot-merged` event](../maintaining.md#the-dependabot-merged-event).

The checks to require in a ruleset on the default branch are `hygiene / hygiene` and `validation / validation`; [Change a check name](../maintaining.md#change-a-check-name) changes them.

## Inputs

The hygiene caller may set `zizmor-persona` to `pedantic` or `auditor`, for a stricter zizmor persona than the default, and `link-exclude` to the globs, one per line, of Markdown files the repository does not maintain, such as vendored documentation, whose relative links the link check then leaves alone. A caller that sets either records the input with its value and its reason under [Repository choices](#repository-choices). No other input is the repository's own choice.

The other inputs of the hygiene caller follow from the repository: `gitleaks-config` and `gitleaks-baseline` name the files that the Gitleaks command in `docs/agents/delivery.md` passes with `--config` and `--baseline-path`, and `coverage-exclude` lists the path of each package file that is a test fixture, one per line.

## Dependabot

A new package file that Dependabot supports gets an `updates` entry in `.github/dependabot.yml`. When no entry for its ecosystem covers its directory through `directory` or `directories`, extend the ecosystem's entry or add one, with `schedule`, and `cooldown` and `groups` where present, copied from an existing entry, preferring the `github-actions` entry. A package file that is a test fixture, which nothing installs, gets no entry; its path goes into the hygiene caller's `coverage-exclude`.

## Suite selection

`validate.yml` is the validation caller, which calls the selection and verdict workflows by local path on `tests/suites.json` and runs its suites under the recording step, and `full-recording.yml` is its [full recording](../maintaining.md#full-recording); `tests/README.md` describes the suites and how they run, and `docs/maintaining.md` the shared parts themselves.

## Repository choices

None.
