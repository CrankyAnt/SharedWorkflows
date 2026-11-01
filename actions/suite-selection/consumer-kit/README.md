# Suite selection

The consumer kit of the suite selection with its recording step, a shared part of the [catalogue](../../../catalogue.json). The suite selection runs only the suites and entry files that a change can affect, judged from path lists that the recording step records while the suites run, and reports one verdict as the required check. A repository adopts it through four files of its own, made from the templates of this kit:

| Template | Install target | Job | Rights on the job |
| --- | --- | --- | --- |
| [validation.yml](validation.yml), the validation caller | `.github/workflows/validation.yml` | `select`, the validation workflow's job, `validation` | `actions: read`, `contents: read`, `pull-requests: read` on `select`; `contents: read` on the others |
| [validation-workflow.yml](validation-workflow.yml), the validation workflow | `.github/workflows/<validation-workflow>.yml` | `linux`, `<windows-job>` | `contents: read` |
| [full-recording.yml](full-recording.yml), the full recording | `.github/workflows/full-recording.yml` | `plan`, `linux`, `windows`, `close` | `contents: read` |
| [apply-additions.yml](apply-additions.yml), the additions workflow | `.github/workflows/apply-additions.yml` | `apply` | `actions: read`, `contents: read` |

With them comes the suite list `.github/suites.json`, which the repository writes itself as [The suite list](#the-suite-list) states.

## When a repository adopts it

A repository adopts the suite selection when its test suites take long enough that running only the affected ones saves more than the selection costs, its upkeep included. The signs are a CI run that misses about a minute for a small change, a rising share of tests that run on every change, and a job that grows with the tests. A repository whose whole validation finishes in about a minute gains nothing; it keeps its validation workflow as it is.

It needs:

- a validation workflow: a workflow of the repository with a job that runs a suite on `pull_request` or `push`, which the kit's validation workflow replaces;
- suites whose entry files run one at a time with one command each, such as a test module with `python3 -m unittest`;
- one Linux runner label with `strace`, `jq` and the system `python3` for every Linux suite, and one Windows runner label with administrator rights, `python` and `dotnet` for every Windows suite, as GitHub's hosted runners have them; the recording step installs nothing;
- for the full recording, a GitHub App that may push a branch and open a pull request in the repository, as [The full recording](#the-full-recording) states.

## The validation caller

Copy [validation.yml](validation.yml) to `.github/workflows/validation.yml` and replace:

- `<default-branch>` with the repository's default branch;
- `<runner-labels>` with the runner label of the validation workflow's jobs on each system, as `<os>=<label>` separated by spaces, such as `linux=ubuntu-24.04 windows=windows-2025`: the label of every shard job for `linux` and that of the Windows job for `windows`;
- `<setup-versions>` with the version of each setup that pins it outside the files the recording reads, as `<setup>=<version>` separated by spaces, such as `python=3.13` for the `python-version` of `actions/setup-python`; drop `setup-versions` when no setup pins one;
- `<validation-workflow>` with the file name of the validation workflow without `.yml`, in its job id, its `uses:` and the `needs` of `validation`. The caller has this one validation job, and `validation` needs `select` and it.

The validation job passes the outputs `shards`, `windows`, `language-steps` and `shared-digest` of `select`. Keep `language-steps: python` on `select`, and the input `language-steps` of the validation job, in a repository with Python files; drop both otherwise. The validation job's `uses:` keeps `$/` as the template writes it: `$/` is GitHub's reference to the running workflow's own repository and revision.

The caller runs on pull requests, on pushes to the default branch and on the `repository_dispatch` events `dependabot-merged`, which Dependabot auto-merge sends after each merge, and `v1-moved`, which a manual move of `v1` sends ([When `v1` moves](#when-v1-moves)), without `paths:` or `paths-ignore:`. A pull-request run is cancelled when the workflow starts for a newer head of the same pull request; push and dispatch runs are never cancelled. On a `dependabot-merged` run, `select` fails, and with it the run, when `client_payload.sha` is malformed, absent from the checkout, or a commit off the default branch; a `v1-moved` run carries no `client_payload` and runs on the default branch's tip. The validation workflow's jobs wait for `select`, so they need no check of their own. A workflow-level `concurrency` of a former validation workflow moves to the caller.

The job `select` calls `CrankyAnt/SharedWorkflows/.github/workflows/suite-selection.yml@v1`:

| Input | Value |
| --- | --- |
| `suites` | the suite list's path, default `.github/suites.json` |
| `language-steps` | `python` for a repository with Python |
| `runner-labels` | the runner label of each system's suite jobs |
| `setup-versions` | the version that a setup pins outside the recorded files |

| Output | Value |
| --- | --- |
| `suites` | one JSON object with `run` or `skip` for every suite |
| `entries` | one JSON object with the selected entry files of each suite selected per entry file, empty when there is none |
| `shards` | one JSON list with one object per Linux shard: its number `shard`, its estimate, its `setups` and its `entries`, each with its `suite`, `entry` and `command`; empty or `[]` when no Linux entry file runs |
| `windows` | one JSON object with the estimate, the `setups` and the `entries` of the Windows job, empty when no Windows entry file is selected |
| `language-steps` | one JSON object with `run` or `skip` for each language step |
| `shared-digest` | the digest of the shared recording step that `select` took |
| `identical` | `true` on a push or dispatch run whose tree equals a tree that passed on its pull request under the same shared digest, empty otherwise |

The job `validation` calls `CrankyAnt/SharedWorkflows/.github/workflows/suite-verdict.yml@v1` with `${{ toJSON(needs) }}` and runs under `if: always() && !(needs.select.result == 'success' && needs.select.outputs.identical == 'true')`. It fails when `select` did not succeed or a called job ended `failure` or `cancelled`; a skipped job is a suite that the selection did not run. Its check `validation / validation` is the one check that the suite selection adds to the checks the default-branch ruleset requires. The validation workflow's checks are `<file name> / shard <number>` for each shard job and `<file name> / <windows-job>` for the Windows job, such as `tests / shard 1` and `tests / windows` for `tests.yml`, and none of them is a required check. A required check of a validation workflow from before the adoption changes its name once the workflow runs through the caller; a change to the ruleset is a question to the user.

## The validation workflow

The repository has one validation workflow: copy [validation-workflow.yml](validation-workflow.yml) to `.github/workflows/<validation-workflow>.yml`. It runs every selected Linux entry file in shards, all on one runner label, and every selected Windows entry file in one job. The suites of former validation workflows move into it: their entry files and commands into the suite list, their setups into its setup steps. The template's placeholders:

| Placeholder | Value |
| --- | --- |
| `<name>` | the workflow's `name:` |
| `<linux-runner>` | the runner label that `runner-labels` gives for `linux` |
| `<windows-job>` | the id of the Windows job, such as `windows` |
| `<windows-runner>` | the runner label that `runner-labels` gives for `windows` |
| `<python-version>` | the Python version the repository tests on |
| `<setup>`, `<setup-command>`, `<setup-provides>` and their `windows-` forms | a setup's name, its command and its `provide` options, as [Setups](#setups) states |
| `<pin>` | an action pin, as the end of this section states |

A repository without Windows suites drops `<windows-job>`, one without Linux suites and without Python drops `linux`, and one without Python drops the Python steps, the `language-steps` input and the Python language step. A repository with Python keeps `linux` for the Python language step: when only that step runs, `shards` holds one shard without entry files.

- `workflow_call` is its only trigger, with the inputs `shards` (`type: string`, `required: false`), `windows` (`type: string`, `required: false`) and `shared-digest` (`type: string`, `required: true`), and in a repository with Python `language-steps` (`type: string`, `required: false`). `permissions: {}` stands at the top and `contents: read` on each job.
- The job `linux` runs under `if: inputs.shards != '' && inputs.shards != '[]'`, one shard job per item of `shards`, under `strategy.matrix.shard: ${{ fromJSON(inputs.shards) }}` with `fail-fast: false`, named `shard ${{ matrix.shard.shard }}`, with the shard as `SHARD: ${{ toJSON(matrix.shard) }}` in its `env`. `select` sizes the shards so that each shard's estimated time on its two workers stays under 120 s; an entry file over that bound gets a shard of its own.
- A shard job runs, in order: the checkout; the recording step, `CrankyAnt/SharedWorkflows/actions/recording@v1`, with `id: recording` and `digest: ${{ inputs.shared-digest }}`; the `uses:` setups; the step `Start the recording` with `id: start`; each `run:` setup; the step `Run the shard's entry files under the recording, two at a time`; the step `Check the recording against the suite list` with `id: check` and the artifact name `recording-additions-shard-${{ matrix.shard.shard }}`; in a repository with Python the step `Run the Python language step outside the recording`; and the steps `List the entry files that passed`, `Leave the entry files with additions out of the passed list` and `Upload the entry files that passed`.
- Each setup step of a shard job runs under `if: contains(matrix.shard.setups, '<setup>')`, so a shard sets up only what its entry files state. No step carries a gate on a suite or an entry file.
- The step that runs the entry files runs them two at a time, each as `exec --entry "$entry" --suites .github/suites.json --suite "$suite" -- <command> "$entry"` with the suite and the command that the shard gives for it. It writes each entry file's output to a log of its own and shows the logs, one log group per entry file, after all of them have run. When an entry file fails, the shard job fails after every entry file of the shard has run.
- Shard 1 runs the Python language step after `check`, under `if: ${{ !cancelled() && matrix.shard.shard == 1 && fromJSON(inputs.language-steps).python == 'run' }}`; `select` gives one shard without entry files when only the language step runs. The template passes no `format-check`, so `ruff format --check` stays off unless the repository sets `format-check: "true"`.
- The Windows job `<windows-job>` runs under `if: inputs.windows != ''`, with `WINDOWS: ${{ inputs.windows }}` and the `setups` of that input as `SETUPS` in its `env`. Its setup steps run under `if: contains(fromJSON(env.SETUPS), '<setup>')`, and it runs every Windows entry file of `windows` one after another, each in a log group of its own. The recording commands run as `python "$env:RECORDING\recording.py" ...` in `shell: pwsh` steps; the step that runs the entry files writes the entry files that passed to `$RUNNER_TEMP\suite-results\passed.json`, the check's artifact name is `recording-additions-windows`, and the job ends with `Leave the entry files with additions out of the passed list` and `Upload the entry files that passed`.
- Each checkout gets `ref: ${{ github.event_name == 'repository_dispatch' && github.event.client_payload.sha || '' }}` and `persist-credentials: false`.
- On a pull request, a shard job uploads the entry files that passed as `suite-results-shard-<number>` and the Windows job as `suite-results-windows`. The verdict stores the passed results of a pull request from these artifacts, and a later head of the pull request reuses them. When the recording check passed, the job uploads every entry file that passed. When it reported additions, `Leave the entry files with additions out of the passed list` runs `recording.py keep` with the check's output `additions`, and the job uploads the entry files that passed minus those that the additions name, and still fails. After a recording error the job uploads no passed list.

No step of the validation workflow is gated on a suite, so a change to one of its steps runs every suite, apart from a setup step, which runs the entry files that state its setup, and a step after the entry files that only stores or reports results, as [What selects a suite](#what-selects-a-suite) states.

Each action reference of the templates carries `<pin>` after `@`, such as `uses: actions/checkout@<pin>`. Replace each `@<pin>` with the commit and version comment of the same action in the template's pin source, the workflow of `CrankyAnt/SharedWorkflows` at `v1` that runs the same steps:

| Template | Pin source |
| --- | --- |
| [validation-workflow.yml](validation-workflow.yml) | `.github/workflows/validate.yml` |
| [full-recording.yml](full-recording.yml) | `.github/workflows/full-recording.yml` |
| [apply-additions.yml](apply-additions.yml) | `.github/workflows/full-recording.yml` |

The installed line then reads, for example, `uses: actions/checkout@<40-hex commit> # v<version>`. A call of a shared workflow or a shared step keeps `@v1`.

A validation workflow of the repository's own carries an action from outside the repository, so the repository needs a Dependabot configuration with a `github-actions` entry that ignores `CrankyAnt/SharedWorkflows`.

## Setups

A setup is what a suite needs installed before its entry files run, such as an interpreter or a tool. The repository names its setups; the suite list states them per suite in `setups` and per entry file in `entry_setups`, and each job of the validation workflow runs each setup that its entry files state. Each setup installs into a location of its own, and an entry file sees only the setups it states:

- A setup that is a `run:` step becomes `python3 "$RECORDING/recording.py" exec --setup <setup> -- <its command>` after `start`, installing into `$RUNNER_TEMP/setups/<setup>` rather than onto the job's `PATH` or into the system.
- A setup that is a `uses:` action stands after the recording step and before `start`, outside the recording, with `update-environment: false` for `actions/setup-python`. Its repository reads are extra reads of the suites and entry files that state it, as [The suite list](#the-suite-list) states.
- A setup that gives the entry files something is followed by `python3 "$RECORDING/recording.py" provide --setup <setup>` with it: `--path` for each tool directory, `--variable <name>=<value>` for each variable and `--python <interpreter>` for each Python interpreter it installs. A setup that gives nothing has no `provide`.
- A shard whose entry files state the setup `python` sets it up with the template's steps `Set up Python outside the recording` and `Provide the set-up Python with the lookup report to the entry files that state it`, in the full recording as well, and needs no step `Run the setup <setup> under the recording` for it, with `<python-version>` replaced by the version the repository tests on. `--python` installs a lookup report into that interpreter, so the recording sees the module lookups of its imports.

A setup step writes nothing to `$GITHUB_PATH` or `$GITHUB_ENV`. An entry file runs in the job's environment before its setups, with only what its stated setups provide, so a variable that an entry file needs and no setup provides belongs in the job's `env`. `exec --entry` fails when the job did not install a setup that the entry file states. What a test writes outside what its setups provide, such as `$HOME` or the shared checkout, is no input of selection; a result that depends on such a write is a flaky result. A job that records restores no cache that holds build or test results and turns off build servers and node reuse.

## The suite list

`.github/suites.json` is JSON with the keys `always` and `suites`, and `module_lookups` once a recording stores a module lookup:

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
      "name": "mod",
      "entries": ["checks/run.lua"],
      "command": ["lua"],
      "setups": ["lua"],
      "paths": ["checks/run.lua", "src/**/*.lua"]
    }
  ]
}
```

The repository writes these fields:

- `always`: patterns of paths whose change can affect every suite. The caller, the validation workflow, the full recording and the suite list are none; without such a path `always` is empty.
- `name`: lowercase letters, digits and hyphens, unique in the suite list; it is the key in the selection's outputs and in the gates.
- `entries`: the suite's entry files, as repository paths or as glob patterns over the tracked files. An entry file is a file in any language, such as a test module, a script or a test project file.
- `command`: the program and its arguments that run one entry file, a non-empty list; the job appends the entry file's path.
- `setups`: the names of the setups that the suite needs, as [Setups](#setups) states.
- `os`: `linux`, the default, or `windows`.
- `entry_selection`: `true` for a suite selected per entry file; each entry file then runs when a changed file matches its own lists. A suite without the key is selected as a whole.
- `entry_setups`: with `entry_selection`, more setups per entry file, an object keyed by an entry file.
- The extra reads, only for a read that no recording sees, such as the repository reads of a `uses:` setup, each pattern no wider than that read. They add to the stored lists and never remove from them: `extra_paths` (file content), `entry_extra_paths` (file content of some entry files, an object with a list per entry file), `extra_directory_memberships` (glob patterns whose added, removed or renamed paths the suite notices) and `extra_existence_reads` (objects with a `path` and a `kind` of `file`, `directory` or `any`, for a path whose appearance or disappearance the suite notices).

The recording writes the stored lists and nobody edits them by hand: `paths`, `directory_memberships`, `existence_reads` and `lookup_directories`, per entry file `entry_paths`, `entry_directory_memberships`, `entry_existence_reads` and `entry_lookup_directories`, the module lookups under `module_lookups` and the duration estimates `entry_durations`. A suite without `paths` runs on every change until its lists are recorded.

Patterns use GitHub's path-filter glob syntax: `**` crosses directory separators, `*` and `?` do not. Any other key, an entry file or pattern that matches no tracked file, or a malformed extra read makes the suite list invalid and fails `select`; it never runs more instead.

Propose the first suite list from the existing validation workflows: one suite per job or step that runs a suite, named after that job or step, with its entry files, the command that runs one of them, its setups, `os: windows` on a Windows runner, and `entry_selection` where the repository selects per entry file. A `paths:` filter of an existing workflow shows where to look for extra reads and is not copied. The suites start without stored lists and run on every change until the `check` of the adopting pull request names their additions, which that pull request applies as [Additions](#additions) states.

## What selects a suite

`select` compares the changed files of the event with each suite's stored lists and extra reads. A suite runs when a changed file matches its paths, changes a directory membership or an existence read it makes, or adds a module name that it looks up; a suite selected per entry file runs the entry files whose own lists match. Every suite runs when the changed files cannot be determined, a changed file matches an `always` pattern, or a change touches a tracked `.gitignore` file or adds a path that the ignore rules match. A changed file that no suite reads selects none.

A change to what runs a suite selects it as well: a changed `command`, `setups`, `os` or `entry_selection` runs every entry file of the suite, and a changed `entries` or `entry_setups` the entry files it adds or changes; a removal from the stored lists or the extra reads runs what it was removed from. A changed part of a suite job that applies before or while its entry files run, such as its condition, `runs-on`, the checkout or the step that runs the entry files, runs every unit of the job; the jobs of the kit's validation workflow gate on no suite, so for them that is every suite. A changed setup step runs the entry files that state its setup. A validation workflow from an earlier kit, with a gate per suite, keeps working: a changed step of it runs the units of its gate. A changed `on` or `concurrency`, a name or comment, the jobs `select` and `validation`, and a step after the entry files that only stores or reports results select nothing.

The `select` job's summary lists every suite with its decision and reason. On a pull request, a later head reuses a passed result whose reads, command, setup versions, runner label, shared digest and tracked ignore rules did not change; a re-run reuses nothing.

## The recording and its check

The recording step records what each setup and entry file reads, on Linux under `strace` and on Windows through an ETW session, and its `check` compares the reads with the stored lists of the suites and entry files that ran. A read of a path that the ignore rules match and that is not tracked is no read, so build output and caches select nothing. A setup's reads belong to the lists of every suite and entry file in its job that states it.

Each read outside the stored lists is an addition: `check` lists the additions in the job summary, uploads them as the artifact named by `artifact-name`, and fails the job, so `validation / validation` fails. A suite without a stored list always reports additions.

## Additions

The additions workflow applies the additions of a validation run to the suite list and proposes the change as a pull request onto the branch of the pull request that reported them. Copy [apply-additions.yml](apply-additions.yml) to `.github/workflows/apply-additions.yml` in every repository with the validation caller, and replace:

- `<gitleaks-options>` as in [The full recording](#the-full-recording);
- each `@<pin>` as [The validation workflow](#the-validation-workflow) states.

GitHub starts a workflow by hand only when its file is on the default branch, so the additions workflow reaches the default branch in a pull request of its own before the pull request that adopts the suite selection, whose suites report additions on their first run.

When the check of a pull request's run reports additions, `validation / validation` fails and the job summary of each failed suite job names this route:

1. Take the run id from the address of the validation run, `.../actions/runs/<run id>`.
2. Start the workflow `Apply recorded additions` on the default branch with that run id: `gh workflow run apply-additions.yml -f run-id=<run id>`.
3. Review its pull request `chore: apply the recorded additions of run <run id>` from `chore/apply-recorded-additions-<run id>`, whose base is the reporting pull request's branch and whose only change is the suite list, and merge it into that branch once its own checks pass.
4. The merge updates the reporting pull request, whose validation then runs again on the applied lists.

The workflow reads the run and its pull request, checks out that pull request's head branch, downloads every `recording-additions-*` artifact of the run, each into its own directory, and applies all of them in one `recording.py apply` of the recording step at `v1`. When the suite list does not change, it opens no pull request and its summary says so. It fails with the reason and opens nothing when the run has no `recording-additions-*` artifact, ran on an event other than `pull_request`, or comes from a pull request whose head branch lies in a fork, since the App proposes only onto a branch of the repository itself. A later start with the same run id updates the open pull request of that run.

Its workflow token only reads the run, its artifacts and the checkout. The pull request is made by the GitHub App of [The full recording](#the-full-recording), with the same repository variable and secret; without them the workflow fails with the App setting it needs.

## The full recording

The full recording is the safety net of the stored lists and the only route that removes a stored read. Copy [full-recording.yml](full-recording.yml) to `.github/workflows/full-recording.yml` in every repository with the validation caller, and replace:

- `<linux-runner>` with the runner label of the repository's Linux suite jobs and `<windows-runner>` with that of its Windows suite jobs, or with `windows-latest` without one;
- in `linux` and in `windows`, the step `Run the setup <setup> under the recording` with one step per setup of the suite list, each with `if: contains(matrix.shard.setups, '<setup>')` in `linux` and `if: contains(fromJSON(env.SETUPS), '<setup>')` in `windows`, running its command as in the validation workflow; a setup that is a `uses:` action stands before `Start the full recording`, under the same `if:`, the setup `python` keeps the template's own steps, and without another setup the step goes;
- `<python-version>` with the version of the validation workflow;
- `<gitleaks-options>` with the `--config` and `--baseline-path` options of the repository's own Gitleaks command, the files that its hygiene caller passes as `gitleaks-config` and `gitleaks-baseline`, or remove it when that command passes neither;
- `<validation-caller>` with the path of the [validation caller](#the-validation-caller), `.github/workflows/validation.yml`;
- each `@<pin>` as [The validation workflow](#the-validation-workflow) states.

It runs every two weeks on the default branch, on a Monday schedule in even ISO weeks, and whenever it is started by hand. `plan` assigns every entry file to shards; each shard runs its setups and entry files under the recording in the full-recording mode, and `windows` runs every Windows entry file in one job. `close` joins the results and replaces the stored lists of each entry file that exited with `0` and whose test report reports no skipped test, keeps the others with the reason, and writes the duration estimates: it corrects each measurement for the speed of its shard job, which `start --job` names, and changes a stored estimate only when the corrected value differs from it by more than 25%. On the default branch it applies the results to the suite list of the default branch's tip as `close` fetches it, and proposes the changed suite list as the pull request `chore: update the recorded path lists` from `chore/update-recorded-lists`, a commit onto that tip, with the report as its body; merge it when its CI is green. When the default branch moved during the run, `close` selects the change since the recorded commit as the validation caller would on a push, and each entry file that the change selects keeps its stored reads beside the recorded ones, with the reason in the report; when that selection fails, every entry file keeps them. A result for a suite or an entry file that the tip no longer holds is left out and named in the report. A run on another branch applies to the commit it started from, uploads the joined result as the artifact `full-recording-joined` and opens no pull request.

A run started by hand on the default branch with the input `verify_token` set to `true` mints the App's token even when the suite list did not change, and its run summary reports whether that succeeded; this verifies a new private key. Without a change it proposes no pull request.

`close` makes the pull request as a GitHub App installed on the repository with Contents and Pull requests read and write, with the App's client id in the repository variable `PUBLICATION_CHECKS_APP_CLIENT_ID` and its private key in the repository secret `PUBLICATION_CHECKS_APP_PRIVATE_KEY`. Without them it uploads the joined result and fails with the App setting it needs.

## Repository settings

The user configures these settings; the workflows change none of them:

- A ruleset on the default branch requires `validation / validation` with GitHub Actions as its source. The ruleset editor lists a check only after it succeeded in the repository within the past 7 days, so the caller reaches the default branch and reports on a pull request before the ruleset names it.
- For the full recording and the additions workflow, the App installation, the repository variable and the repository secret that [The full recording](#the-full-recording) names.

## When `v1` moves

The recording step comes from `CrankyAnt/SharedWorkflows` at `v1`, not from the repository. `select` takes a digest of it and compares it with the digest of the last passed run of the caller on the base branch; when the two differ, or that run cannot be found, every suite runs. The first run after `v1` moves therefore runs every suite once.

After each manual move of `v1`, a promotion, a rollback or the first creation alike, `promote_v1.py promote`, which moves `v1` by hand in Shared Workflows, sends the `repository_dispatch` event `v1-moved`, without a `client_payload`, to every repository of the owner that is not archived and whose default-branch workflow file calls `CrankyAnt/SharedWorkflows/.github/workflows/suite-selection.yml@v1` and lists `v1-moved` among its `repository_dispatch` types. The caller template lists it; a caller copied from an earlier template adds `v1-moved` to the `types` of its `repository_dispatch` trigger. The `v1-moved` run checks out the default branch's tip and selects as a push of that commit: every suite runs when the shared digest moved, and no suite runs when the tree equals a tree that passed on its pull request under an unchanged digest. Once that run has passed, every later pull-request run takes it as the last passed run on the base branch and runs its normal selection; a pull-request run that starts before it passed still runs every suite. A caller that does not list `v1-moved` gets no event, and its pull-request runs run every suite until a push or a Dependabot merge passes under the new digest. The automatic move of a pins-only commit sends no event, since it leaves the shared digest unchanged.

When `v1` moves during a run, a suite job fails on its digest; start the whole run again, since a re-run of the failed jobs keeps the earlier `select`.

## A skipped suite

When a run skipped a suite that had to run, read the summary of its `select` job: the changed files and each suite with its decision and reason. A read that no recording sees, such as a branch that no run takes, goes into the suite's extra reads, or into `always` when every suite reads it. A read that the recording should have seen is a fault of the shared part: report it to the user with the summary and the recording of the run.
