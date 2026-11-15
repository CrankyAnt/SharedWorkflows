# Suite selection

The consumer kit of the suite selection with its recording step, a shared part of the [catalogue](../../../catalogue.json). The suite selection runs only the suites and entry files that a change can affect, judged from path lists that the recording step records while the suites run, and reports one verdict as the required check. A repository adopts it through two files of its own, made from the templates of this kit:

| Template | Install target | Job | Rights on the job |
| --- | --- | --- | --- |
| [validation.yml](validation.yml), the validation caller | `.github/workflows/validation.yml` | `select`, the validation workflow's job, `validation` | `actions: read`, `contents: read`, `pull-requests: read` on `select`; `contents: read` on the others |
| [validation-workflow.yml](validation-workflow.yml), the validation workflow | `.github/workflows/<validation-workflow>.yml` | `linux`, `<windows-job>` | `contents: read` |

The additions part and the full recording are not templates of this kit. They are parts of the pipeline that the repository switches on in its configuration file, as [Additions](#additions) and [The full recording](#the-full-recording) state.

With them comes the suite list `.github/suites.json`, which the repository writes itself as [The suite list](#the-suite-list) states.

## When a repository adopts it

A repository adopts the suite selection when its test suites take long enough that running only the affected ones saves more than the selection costs, its upkeep included. The signs are a CI run that misses about a minute for a small change, a rising share of tests that run on every change, and a job that grows with the tests. A repository whose whole validation finishes in about a minute gains nothing; it keeps its validation workflow as it is.

It needs:

- a validation workflow: a workflow of the repository with a job that runs a suite on `pull_request` or `push`, which the kit's validation workflow replaces;
- suites whose entry files run one at a time with one command each, such as a test module with `python3 -m unittest`;
- one Linux runner label with `strace`, `jq` and the system `python3` for every Linux suite, and one Windows runner label with administrator rights, `python` and `dotnet` for every Windows suite, as GitHub's hosted runners have them; the recording step installs nothing;
- for the additions part and the full recording, a GitHub App that may push a branch and open a pull request in the repository, as [The full recording](#the-full-recording) states.

## When a repository adopted it earlier

A repository that adopted the suite selection earlier brings its installed files onto the current templates in one pull request. It compares each installed file with the template it came from, read at `v1`, by install target:

| Template | Install target |
| --- | --- |
| [validation.yml](validation.yml) | `.github/workflows/validation.yml` |
| [validation-workflow.yml](validation-workflow.yml) | `.github/workflows/<validation-workflow>.yml` |

Every difference that a template makes is taken over, apart from the repository's own values, which carry over:

- the suite list with its path: `.github/suites.json`, or the repository's own path, which stands in `suites` on `select`, in place of `.github/suites.json` in the validation workflow, and in `suites` of the `additions` and `full-recording` parts of the configuration file. The suite list keeps the fields the repository writes, and the stored lists and the duration estimates `entry_durations` that the recording wrote. The update changes none of them;
- the validation workflow's file name, in the caller's validation job id, its `uses:` and the `needs` of `validation`;
- the validation workflow's setups: its setup steps in the shard job and the Windows job, as [Setups](#setups) states;
- the checkout options of each job, in the validation workflow and in the full recording, that the repository set beside the template's, such as `fetch-depth: 0`, as [The validation workflow](#the-validation-workflow) states; the full recording of an earlier kit is the exception, as the step on it below states;
- the variables in each job's `env` that the repository set beside the template's, as [Setups](#setups) states;
- the inputs the repository set on each caller and template: the default branch; on `select` the values of `suites`, `language-steps`, `runner-labels` and `setup-versions`; in the validation workflow `<name>`, the runner labels, `<windows-job>` and `<python-version>`, with the jobs and steps the repository dropped.

Each `@<pin>` of a template is filled again from its pin source, as [The validation workflow](#the-validation-workflow) states.

An earlier adoption also takes these steps where they apply:

- A caller copied from an earlier template removes `v1-moved` from the `types` of its `repository_dispatch` trigger: nothing sends that event any more, as [When `v1` moves](#when-v1-moves) states.
- A caller with more than one validation job keeps one: the suites of the other validation workflows move into the one validation workflow, as [The validation workflow](#the-validation-workflow) states. A validation workflow from an earlier kit, with a gate per suite, becomes the template's sharded form, whose checks are `<file name> / shard <number>` and `<file name> / <windows-job>`; none of them is a required check, and a change to the ruleset is a question to the user.
- A repository without the additions part adds the fixed caller `automation.yml` and the `additions` part in a pull request of its own first, as [Additions](#additions) states.
- A `full-recording.yml` or `apply-additions.yml` installed from an earlier kit has no template in this kit any more. It keeps working, since the shared steps that it calls stay at `v1`. The repository either registers the file under `local-workflows` of its configuration file, as the [pipeline kit](../../pipeline/consumer-kit/README.md#the-configuration-file) states, or switches the part on under `parts` and removes the file in the same pull request. A switched-on part runs the shared workflow instead, so the installed file is no longer needed.

The result works when these pass:

- on the pull request, `validation / validation`. A changed step of the validation workflow runs every suite in `<file name> / shard <number>` and `<file name> / <windows-job>`; when `check` reports additions, the additions part commits them onto the pull request's branch and the run on the applied lists passes. `hygiene / hygiene` passes as well, with actionlint and zizmor over the changed workflows;
- after the merge, `validation / validation` on the push to the default branch;
- the full recording, started by hand on the default branch with `gh workflow run automation.yml -f part=full-recording -f verify-token=true --ref <default-branch>`, whose run passes and whose summary reports the App's token as minted.

## The validation caller

Copy [validation.yml](validation.yml) to `.github/workflows/validation.yml` and replace:

- `<default-branch>` with the repository's default branch;
- `<runner-labels>` with the runner label of the validation workflow's jobs on each system, as `<os>=<label>` separated by spaces, such as `linux=ubuntu-24.04 windows=windows-2025`: the label of every shard job for `linux` and that of the Windows job for `windows`;
- `<setup-versions>` with the version of each setup that pins it outside the files the recording reads, as `<setup>=<version>` separated by spaces, such as `python=3.13` for the `python-version` of `actions/setup-python`; drop `setup-versions` when no setup pins one;
- `<validation-workflow>` with the file name of the validation workflow without `.yml`, in its job id, its `uses:` and the `needs` of `validation`. The caller has this one validation job, and `validation` needs `select` and it.

The validation job passes the outputs `shards`, `windows`, `language-steps` and `shared-digest` of `select`. Keep `language-steps: python` on `select`, and the input `language-steps` of the validation job, in a repository with Python files; drop both otherwise. The validation job's `uses:` keeps `$/` as the template writes it: `$/` is GitHub's reference to the running workflow's own repository and revision.

The caller runs on pull requests, on pushes to the default branch and on the `repository_dispatch` event `dependabot-merged`, which Dependabot auto-merge sends after each merge, without `paths:` or `paths-ignore:`. A pull-request run is cancelled when the workflow starts for a newer head of the same pull request; push and dispatch runs are never cancelled. On a `dependabot-merged` run, `select` fails, and with it the run, when `client_payload.sha` is malformed, absent from the checkout, or a commit off the default branch. The validation workflow's jobs wait for `select`, so they need no check of their own. A workflow-level `concurrency` of a former validation workflow moves to the caller.

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
- Each checkout gets `ref: ${{ github.event_name == 'repository_dispatch' && github.event.client_payload.sha || '' }}` and `persist-credentials: false`, and keeps the repository's own options beside these two. The template's checkout fetches only the checked-out commit; when entry files read older commits of the repository's history, for example with `git cat-file blob <sha>`, the shard job's checkout keeps `fetch-depth: 0`.
- On a pull request, a shard job uploads the entry files that passed as `suite-results-shard-<number>` and the Windows job as `suite-results-windows`. The verdict stores the passed results of a pull request from these artifacts, and a later head of the pull request reuses them. When the recording check passed, the job uploads every entry file that passed. When it reported additions, `Leave the entry files with additions out of the passed list` runs `recording.py keep` with the check's output `additions`, and the job uploads the entry files that passed minus those that the additions name, and still fails. After a recording error the job uploads no passed list.

No step of the validation workflow is gated on a suite, so a change to one of its steps runs every suite, apart from a setup step, which runs the entry files that state its setup, and a step after the entry files that only stores or reports results, as [What selects a suite](#what-selects-a-suite) states.

Each action reference of the template carries `<pin>` after `@`, such as `uses: actions/checkout@<pin>`. Replace each `@<pin>` with the commit and version comment of the same action in the pin source, the workflow of `CrankyAnt/SharedWorkflows` at `v1` that runs the same steps:

| Template | Pin source |
| --- | --- |
| [validation-workflow.yml](validation-workflow.yml) | `.github/workflows/validate.yml` |

The installed line then reads, for example, `uses: actions/checkout@<40-hex commit> # v<version>`. A call of a shared workflow or a shared step keeps `@v1`.

A validation workflow of the repository's own carries an action from outside the repository, so the repository needs a Dependabot configuration with a `github-actions` entry that ignores `CrankyAnt/SharedWorkflows`.

## Setups

A setup is what a suite needs installed before its entry files run, such as an interpreter or a tool. The repository names its setups; the suite list states them per suite in `setups` and per entry file in `entry_setups`, and each job of the validation workflow runs each setup that its entry files state. Each setup installs into a location of its own, and an entry file sees only the setups it states:

- A setup that is a `run:` step becomes `python3 "$RECORDING/recording.py" exec --setup <setup> -- <its command>` after `start`, installing into `$RUNNER_TEMP/setups/<setup>` rather than onto the job's `PATH` or into the system.
- A setup that is a `uses:` action stands after the recording step and before `start`, outside the recording, with `update-environment: false` for `actions/setup-python`. Its repository reads are extra reads of the suites and entry files that state it, as [The suite list](#the-suite-list) states.
- A setup that gives the entry files something is followed by `python3 "$RECORDING/recording.py" provide --setup <setup>` with it: `--path` for each tool directory, `--variable <name>=<value>` for each variable and `--python <interpreter>` for each Python interpreter it installs. A setup that gives nothing has no `provide`.
- A shard whose entry files state the setup `python` sets it up with the template's steps `Set up Python outside the recording` and `Provide the set-up Python with the lookup report to the entry files that state it`, with `<python-version>` replaced by the version the repository tests on, and needs no step `Run the setup <setup> under the recording` for it. `--python` installs a lookup report into that interpreter, so the recording sees the module lookups of its imports. The full recording sets up `python` the same way, with Python 3.13, as [The full recording](#the-full-recording) states.

A setup step writes nothing to `$GITHUB_PATH` or `$GITHUB_ENV`. An entry file runs in the job's environment before its setups, with only what its stated setups provide. A variable of the step that runs the entry files does not reach them. A variable that an entry file needs and no setup provides reaches it by its source:

- a value from the contexts that a job's `env` allows, `github`, `needs`, `strategy`, `matrix`, `vars`, `secrets` and `inputs`, goes in the job's `env`;
- a value from the `runner` context, which a job's `env` does not allow, reaches the entry file under its own name through its default environment variable, such as `RUNNER_TEMP` for `runner.temp`, which the job's environment already holds;
- a value from the `runner` context under another name reaches it only through a setup's `provide --variable`.

`exec` gives each entry file a temporary directory of its own in `TMPDIR`, and on Windows in `TEMP` and `TMP` as well; no other value replaces them, neither the job's `env` nor a setup's `provide --variable`.

`exec --entry` fails when the job did not install a setup that the entry file states. What a test writes outside what its setups provide, such as `$HOME` or the shared checkout, is no input of selection; a result that depends on such a write is a flaky result. A job that records restores no cache that holds build or test results and turns off build servers and node reuse.

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

The additions part applies the additions of a validation run to the suite list. The pipeline runs it in the fixed caller `automation.yml` of the [pipeline kit](../../pipeline/consumer-kit/README.md#the-fixed-callers), next to the [full recording](#the-full-recording). After a failed pull-request run it commits the change onto the pull request's branch; started by hand with a run id, it proposes the change as a pull request onto that branch. Its jobs report the checks `automation / additions` and `automation / additions-comment`.

Switch the part on in `.github/ci-config.yml`, in every repository with the validation caller:

```yaml
parts:
  additions:
    suites: .github/suites.json
  full-recording:
    suites: .github/suites.json
    validation-workflow: .github/workflows/validation.yml
```

`suites` is the path of the suite list, and `validation-workflow` the path of the validation caller, which the closing job of the full recording passes to `select`. The configuration file and the fixed caller reach the default branch first, since GitHub starts a workflow by hand or on `workflow_run` only when its file is on the default branch. That takes a pull request of its own before the pull request that adopts the suite selection, whose suites report additions on their first run.

When the check of a pull request's run reports additions, `validation / validation` fails and the job summary of each failed suite job names both routes below.

### The automatic route

The job `additions` runs on `workflow_run` when a run of the workflow `CI` completes, the trigger that the fixed caller names. A run of a workflow with another name, such as the validation caller of this kit, does not start it: until the suites run in `CI`, their additions follow [the manual route](#the-manual-route). It acts only when all of these hold:

- the run's event is `pull_request`, the run failed, and its head branch lies in the repository itself; a pull request from a fork gets no automatic route;
- the run uploaded at least one unexpired `recording-additions-*` artifact;
- an open pull request has the run's head branch as its head and the run's head commit as its head SHA.

It checks out the run's head commit, downloads every `recording-additions-*` artifact of the run and applies every `additions.json` of the download in one `recording.py apply` of the recording step at `v1`, before the App's token exists and with no token in that step's environment. An `additions.json` that is no valid additions document fails the job there, and a change to any file other than the suite list fails the job before the token. When the suite list changed, the job commits it as the GitHub App onto the pull request's branch with the subject `chore: apply the recorded additions of run <run id>` and `[dependabot skip]` in the body, scans the commit with the pinned Gitleaks and pushes it with a lease on the run's head commit. It opens no pull request. `[dependabot skip]` keeps Dependabot rebasing its pull request over the commit.

The App's push starts the validation caller on the new head. There `select` runs only the entry files with additions and the units whose stored lists name the suite list, runs every recorded entry file of a pooled suite with additions again, and reuses the passed result of every other unit that passed in the failed run. The failed run stays red; the required check `validation / validation` on the new head decides the merge.

Each push by a person or agent gets at most one automatic round: when the failed run's head commit carries the subject of applied additions, the workflow applies nothing. The subject decides, not the author, since the same App also makes other commits.

The job `additions-comment` posts one comment on the pull request with the reason and the manual route, in three cases: at the bound, when applying leaves the suite list unchanged, and when the token, the push or another step after the apply fails. It posts no comment otherwise. The comment names the manual route with the file of the fixed caller, `automation.yml`: `gh workflow run automation.yml -f part=additions -f run-id=<run id>`. A head that moved since the run, or a pull request that closed, ends the workflow without a commit and without a comment. Runs for one head branch share one concurrency group without cancel, so a later run waits for the earlier one and then finds the head moved. GitHub keeps one waiting run per group and cancels an older waiting one when another arrives; a cancelled run commits and comments nothing.

The route covers every pull request whose head branch lies in the repository, Dependabot's included, without a label. When Dependabot rebuilds its pull request over the App's commit, the next failed run applies the additions again. [Dependabot auto-merge](../../dependabot-auto-merge/consumer-kit/README.md#switching-it-on) accepts the App's commit when its part names the suite list's path in `suites`. A suite list that the base branch changed in the meantime can conflict with the applied additions; the pull request resolves that conflict like any other.

### The manual route

Started by hand, the job proposes the additions of a run as a pull request. It serves a run that the comment names and a run that the automatic route does not cover:

1. Take the run id from the address of the validation run, `.../actions/runs/<run id>`.
2. Start the workflow `Automation` on the default branch with the part and that run id: `gh workflow run automation.yml -f part=additions -f run-id=<run id>`.
3. Review its pull request `chore: apply the recorded additions of run <run id>` from `chore/apply-recorded-additions-<run id>`, whose base is the reporting pull request's branch and whose only change is the suite list, and merge it into that branch once its own checks pass.
4. The merge updates the reporting pull request, whose validation then runs again on the applied lists.

The job reads the run and its pull request, checks out that pull request's head branch, downloads every `recording-additions-*` artifact of the run and applies every `additions.json` of the download in one `recording.py apply` of the recording step at `v1`, at any depth of the download folder: `actions/download-artifact` extracts a single artifact directly into it and each of several into a folder named after the artifact. When the suite list does not change, it opens no pull request and its summary says so. It fails with the reason and opens nothing when the run has no `recording-additions-*` artifact, ran on an event other than `pull_request`, or comes from a pull request whose head branch lies in a fork, since the App proposes only onto a branch of the repository itself. A later start with the same run id updates the open pull request of that run.

### Tokens

The workflow token of `additions` only reads the run, its artifacts, the open pull requests and the checkout; `additions-comment` writes the comment with a workflow token that may write pull requests and checks out nothing. The commit and the pull request are made by the GitHub App of [The full recording](#the-full-recording). The fixed caller passes the secret `PUBLICATION_CHECKS_APP_PRIVATE_KEY` by name, and the repository variable `PUBLICATION_CHECKS_APP_CLIENT_ID` is still read; without them the workflow fails with the App setting it needs and, on the automatic route, comments with the manual route.

## The full recording

The full recording is the safety net of the stored lists and the only route that removes a stored read. The pipeline runs it as the part `full-recording`, in the fixed caller `automation.yml` of the [pipeline kit](../../pipeline/consumer-kit/README.md#the-fixed-callers). Switch it on in `.github/ci-config.yml` as [Additions](#additions) shows, in every repository with the validation caller. It runs on the default branch on a weekly schedule on Mondays, where it records only in even ISO weeks, and whenever it is started by hand with `gh workflow run automation.yml -f part=full-recording [-f verify-token=true] --ref <branch>`.

The job `full-recording` assigns every entry file to shards with `select --full` on `ubuntu-26.04`. Each shard runs its setups and entry files under the recording in the full-recording mode, in the job `full-recording-linux` on `ubuntu-26.04`; the job `full-recording-windows` on `windows-2025` runs every Windows entry file in one job. The job `full-recording-close` on `ubuntu-slim` joins the results and replaces the stored lists of each entry file that exited with `0` and whose test report reports no skipped test, keeps the others with the reason, and writes the duration estimates: it corrects each measurement for the speed of its shard job, which `start --job` names, and changes a stored estimate only when the corrected value differs from it by more than 25%. On the default branch it applies the results to the suite list of the default branch's tip as `close` fetches it, and proposes the changed suite list as the pull request `chore: update the recorded path lists` from `chore/update-recorded-lists`, a commit onto that tip, with the report as its body. It then asks GitHub to merge that pull request by squash once its required checks have passed, with `gh pr merge --auto --squash` bound to the pushed commit, so that a head which moved since the push does not merge on that request. The repository setting [Allow auto-merge](#repository-settings) must allow it. When the default branch moved during the run, `close` selects the change since the recorded commit as the validation caller would on a push, and each entry file that the change selects keeps its stored reads beside the recorded ones, with the reason in the report; when that selection fails, every entry file keeps them. A result for a suite or an entry file that the tip no longer holds is left out and named in the report. A run on another branch applies to the commit it started from, uploads the joined result as the artifact `full-recording-joined` and opens no pull request.

The setups are fixed in the shared workflow. The shards and the Windows job install the setups by the names `python`, `node`, `commitlint`, `actionlint`, `zizmor`, `gitleaks`, `reuse`, `ruff`, `mypy` and `dotnet`, as Shared Workflows' own suites state them, with Python 3.13 and the runner labels above. A repository whose suite list states another setup, or one of these names with another meaning, cannot run the full recording through the part until setups are data of the suite list. Such a repository keeps a full recording of its own as a registered local workflow, in the `local-workflows` register of its configuration file, as the [pipeline kit](../../pipeline/consumer-kit/README.md#the-configuration-file) states.

A run started by hand on the default branch with the input `verify-token` set to `true` mints the App's token even when the suite list did not change, and its run summary reports whether that succeeded; this verifies a new private key. Without a change it proposes no pull request.

The closing job makes the pull request as a GitHub App installed on the repository with Contents and Pull requests read and write, with the App's client id in the repository variable `PUBLICATION_CHECKS_APP_CLIENT_ID` and its private key in the repository secret `PUBLICATION_CHECKS_APP_PRIVATE_KEY`. Without them it uploads the joined result and fails with the App setting it needs. Both parts scan the commit before the push with the pinned Gitleaks, which reads `.gitleaks.toml` and `.gitleaks-baseline.json` by those fixed names when the repository holds them.

## Repository settings

The user configures these settings; the workflows change none of them:

- A ruleset on the default branch requires `validation / validation` with GitHub Actions as its source. The ruleset editor lists a check only after it succeeded in the repository within the past 7 days, so the caller reaches the default branch and reports on a pull request before the ruleset names it.
- For the additions part and the full recording, the App installation, the repository variable and the repository secret that [The full recording](#the-full-recording) names.
- Allow auto-merge, in the repository's settings for pull requests, so that the pull request `chore: update the recorded path lists` merges itself once its required checks have passed, as [The full recording](#the-full-recording) states. Without it the closing job fails with the App setting it needs.

## When `v1` moves

The recording step comes from `CrankyAnt/SharedWorkflows` at `v1`, not from the repository. `select` takes a digest of it and compares it with the digest of the last passed run of the caller on the base branch; for a pull request whose base is not the default branch, that run is the passed pull-request run that produced the base's tip, or else the last passed run on the default branch where the base meets it. When the two differ, or that run cannot be found, every suite runs. The first run after `v1` moves therefore runs every suite once.

No move of `v1` sends the repository an event, and Shared Workflows asks GitHub nothing about the repositories that call it. After a move that changes the shared digest, each pull-request run runs every suite until a run on the default branch, a push or a Dependabot merge, has passed under the new digest. Every pull-request run that starts after that run takes it as the last passed run on the base branch and runs its normal selection. The merge of the first pull request after the move is such a push: its tree equals the tree that passed on the pull request under the new digest, so it runs no suite again and passes. The automatic move of a pins-only commit leaves the shared digest unchanged.

When `v1` moves during a run, a suite job fails on its digest; start the whole run again, since a re-run of the failed jobs keeps the earlier `select`.

## A skipped suite

When a run skipped a suite that had to run, read the summary of its `select` job: the changed files and each suite with its decision and reason. A read that no recording sees, such as a branch that no run takes, goes into the suite's extra reads, or into `always` when every suite reads it. A read that the recording should have seen is a fault of the shared part: report it to the user with the summary and the recording of the run.
