# CI

## Goal

CI gives every proposed change and every change on the default branch a trustworthy verdict, fast. Trustworthy: green only when every gating check would pass if it ran, apart from the accepted risks under Reuse. Fast: from trigger to verdict, over a rolling four weeks of runs that reach a verdict, manual re-runs excluded, 99% finish within 3 minutes and half of those that select no tests within 1 minute; speed before billed minutes. No run does avoidable work: work that cannot change the verdict, work done twice, or work Reuse lets it take over. Checks not specific to one product are shared. This keeps holding as repositories grow.

## Principles

Every agent that adds or changes a check, a suite or a workflow holds to these principles; how it builds the change is its own choice.

- **Purpose.** Every check guards a real failure of the repository as it is now: red when it is present, green otherwise. A check that is obsolete, flaky, covered by another, or skipped on every run whatever the change guards nothing and is fixed or removed.
- **Shared CI.** A check not specific to one product lives in shared CI from the first repository that needs it, never in a copy; a repository adds only what its product needs. Shared CI enforces what code can check of these principles and reports each departure, such as a check with unknown inputs, while the check still runs.
- **Selection.** One decision per run, before any check starts, produces the plan: the checks, tests included, that the change can affect through what they read, what they rely on being absent, or how they run. The plan can be shown without running a check. A check runs on every change only when every change can alter it, or where selecting it costs more than it saves. An event without a code change, such as a title edit, reruns no code check and clears no failure.
- **Sound selection.** What selection cannot see selects more, never less: a check with unknown inputs runs on every change until they are known. Everything runs only when the changed files are unknown or the change alters what every result depends on; a tooling change selects only what it can alter, on its platforms and toolchains. A run that recorded all reads of a test on a platform replaces what was known there; other runs only add. Every fallback to running more is counted per reason.
- **Reuse.** A check takes over a passed result with exactly the same inputs only when the same change or the protected default branch wrote it; a proposed change and its merge are the same change. A merge whose exact tree passed runs only checks with inputs outside the tree; one tested on an older base runs what the change and the newer base can each affect, and everything the change can affect until the newer base has passed. A manual re-run reuses nothing, and a cancelled run's results are never reused. Accepted risks, the only exceptions to a trustworthy verdict: a reused flaky pass, and a rare red default branch through an unrecorded input.
- **One verdict.** One verdict covers every gating check, on proposed changes and the default branch alike; nothing else is required. It is green only when the CI definition is valid, selection succeeded and every planned check passed or took over a pass; a planned check that fails, times out or never starts makes it red, and an unplanned check counts as not run. Every run shows what it ran, skipped and took over, why, and what failed.
- **Jobs.** Prefer fewer, larger jobs, since each adds start-up and billing overhead; split by measured duration only where the goal requires it. Work several checks need runs once per run. Checks in one job do not affect each other, and one failure stops no other check. A check runs on several platforms only where its result can differ. A job's environment changes only through the repository; anything outside it is an input of the checks it affects. Code from a proposed change runs without secrets. A newer push cancels the older run of the same proposed change and gives that run no verdict; a default-branch run is never cancelled.
- **Tests.** Every test belongs to one suite with one subject. The unit of selection is the entry file, a test file the runner can start alone, and it costs no more than its result requires; a finer unit is used only where an entry file's necessary duration holds back the goal. A test uses only the setup it states, reads only what its result depends on and writes only where its setup provides; one that writes elsewhere gives no record of its reads and is fixed or removed. A test starts a real process only for what only a real process shows.
- **Changing CI.** Selection logic exists only in CI. A CI change is judged by its plan for the changes it affects, itself included, by its own normal runs, and then by the goal's measurement; no runs are added only to prove it. A change that misses the goal or undoes a saving is fixed or reverted at once. A replacement removes the old part at once; an unavoidable transition has a named person responsible, a removal condition and an end date.
- **Measurement and growth.** CI measures the goal's targets automatically at a fixed cadence and reports them. A miss, or an early sign of one (a rising share of checks that run on every change, a job that grows with the repository, billed minutes far above used ones, frequent fallbacks), is a gap whoever finds it records; it is closed by removing work first and by faster work second. A restructuring follows these principles, as a separate task whose size the repository owner decides.

## Shared Workflows

This repository calls its own shared workflows and shared steps by local path, `uses: $/.github/workflows/<file>` and `uses: $/actions/<part>`, from its callers, its pipeline and its automation workflow, as [Calling the workflows](../maintaining.md#calling-the-workflows) says. It uses these shared parts:

| Shared part | Caller | Check | Inputs |
| --- | --- | --- | --- |
| Repository hygiene | `repository-hygiene-caller.yml` | `hygiene / hygiene` | `gitleaks-config: .gitleaks.toml`, `gitleaks-baseline: .gitleaks-baseline.json` |
| Dependabot auto-merge | `dependabot-auto-merge-caller.yml` | `auto-merge / auto-merge`, `auto-merge / merge` | none |
| Suite selection | `pipeline.yml`, job `suite-selection`, through the part `suite-selection` | the suite jobs: `ci / suite-selection-jobs / shard <n>` and `ci / suite-selection-jobs / windows`; the pipeline's verdict is `ci / verdict` | the configuration file `.github/ci-config.yml` states `suites: tests/suites.json`, `language-steps: [python]` and `pinned-tools: [gitleaks]` |
| Suite jobs | `pipeline.yml`, job `suite-selection-jobs`, and `automation.yml`, job `full-recording-jobs` | the checks of the suite selection above; the full recording's checks `automation / full-recording-jobs / <shared job>` are checks that no ruleset requires | none; the suite list `tests/suites.json` states the runners, the fetch depth and the setups, and the configuration file names the pinned tool `gitleaks` |
| Python language step | `pipeline.yml`, job `suite-selection-jobs`, shard 1 through `suite-jobs.yml`; `python-tests.yml` in `python-tests-caller.yml` | none | none |
| Python tests | `python-tests-caller.yml` | `tests / tests` | `python-version: "3.14"`, `working-directory: tests/fixtures/python_tests`, `install: dev` |
| Pipeline | `ci.yml` | `ci / verdict` | none; the configuration file `.github/ci-config.yml` switches on the free job and the parts `python-verify`, `python-test`, `dotnet-test`, `node-test` and `suite-selection` |
| Pipeline, the automation workflow | `automation-caller.yml` | none that a ruleset requires | the secret `PUBLICATION_CHECKS_APP_PRIVATE_KEY`; the configuration file switches on `dependabot-auto-merge`, `additions` and `full-recording` with `tests/suites.json`, and `issue-title` with a fixture category file |

`docs/maintaining.md` describes what each part runs:

- Repository hygiene: [Repository hygiene](../maintaining.md#repository-hygiene), with the floor and the [publication checks](../maintaining.md#publication-checks).
- Dependabot auto-merge: [Dependabot auto-merge](../maintaining.md#dependabot-auto-merge).
- Suite selection: [Job layout](../maintaining.md#job-layout) and [Suite selection](../maintaining.md#suite-selection).
- Python language step: [Python language step](../maintaining.md#python-language-step).
- Python tests: [Python tests](../maintaining.md#python-tests), on the fixture project that tests the shared workflow.
- Pipeline: [Pipeline](../maintaining.md#pipeline), which runs repository hygiene a second time beside the hygiene caller while both shared workflows are at `v1`, and runs the suite selection and the suite jobs of [Job layout](../maintaining.md#job-layout); [The automation workflow](../maintaining.md#the-automation-workflow), which runs Dependabot auto-merge a second time beside the auto-merge caller for the same reason, and the full recording's suite jobs.

The hygiene caller, the Python tests caller and the pipeline caller `ci.yml` also run on the [`dependabot-merged` event](../maintaining.md#the-dependabot-merged-event).

The verdict of the pipeline, `ci / verdict`, carries the result of the suites, the hygiene check and the other parts. The checks to require in a ruleset on the default branch are `hygiene / hygiene`, `tests / tests` and `ci / verdict`; [Change a check name](../maintaining.md#change-a-check-name) changes them, and a change to the ruleset is a question to the user.

## Inputs

The hygiene caller may set `zizmor-persona` to `pedantic` or `auditor`, for a stricter zizmor persona than the default, and `link-exclude` to the globs, one per line, of Markdown files the repository does not maintain, such as vendored documentation, whose relative links the link check then leaves alone. A caller that sets either records the input with its value and its reason under [Repository choices](#repository-choices). No other input is the repository's own choice.

The other inputs of the hygiene caller follow from the repository: `gitleaks-config` names the file that the Gitleaks command in `docs/agents/delivery.md` passes with `--config` and `gitleaks-baseline` names the file that the Gitleaks command in `docs/agents/delivery.md` passes with `--baseline-path`.

## Dependabot

A new package file that Dependabot supports is covered by the `updates` entry of its ecosystem in `.github/dependabot.yml`, one entry per ecosystem, with the weekly schedule and the cooldown of seven days of the other entries. When no entry for its ecosystem exists, add one with `directory` and the group `minor-and-patch`, copied from the `github-actions` entry. When the entry of its ecosystem does not list its directory, add the directory to that entry: an ecosystem with several directories lists them under `directories` and has the one group `per-dependency` (`group-by: dependency-name`, `patterns: ["*"]`) in place of `minor-and-patch`, so that one update of a package arrives as one pull request across its directories. A project file, `.csproj`, `.fsproj` or `.vbproj`, that a `.sln` or `.slnx` directly in a directory covered by the `nuget` entry lists needs no directory of its own, as Dependabot finds it through that solution. A package file that is a test fixture, which nothing installs, gets no entry; its path goes into the hygiene caller's `coverage-exclude`. A `.sln` or `.slnx` at or under a path in `coverage-exclude` is not read: it covers none of the project files it lists, and one that cannot be read fails nothing.

## Suite selection

The suites of this repository run in the pipeline. The part `suite-selection` of `.github/ci-config.yml`, which states `suites: tests/suites.json`, `language-steps: [python]` and `pinned-tools: [gitleaks]`, selects the entry files that a change reaches in its job `suite-selection`, and its job `suite-selection-jobs` calls `suite-jobs.yml` for the Linux shards and the Windows job, each under the recording step; the suite jobs give the pinned Gitleaks to the entry files that state it. The part `full-recording` of the automation workflow, which `automation-caller.yml` calls, runs the same jobs in `full-recording-jobs`; it is the [full recording](../maintaining.md#full-recording), and the part `additions` is the [additions workflow](../maintaining.md#apply-recorded-additions). `tests/README.md` describes the suites and how they run, and `docs/maintaining.md` the shared parts themselves.

## Repository choices

None.
