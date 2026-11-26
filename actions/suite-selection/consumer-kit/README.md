# Suite selection

The consumer kit of the suite selection with its recording step, a shared part of the [catalogue](../../../catalogue.json). The suite selection runs only the suites and entry files that a change can affect, judged from path lists that the recording step records while the suites run, and reports one verdict as the required check.

The kit holds no template. A repository adopts the suite selection by switching the part `suite-selection` on in its configuration file, next to the parts `additions` and `full-recording`, and by writing its suite list. The suite jobs run through the fixed caller `ci.yml` of the [pipeline kit](../../pipeline/consumer-kit/README.md#the-fixed-callers), and the shared workflows and steps that they call stay at `v1`.

With the part comes the suite list `.github/suites.json`, which the repository writes itself as [The suite list](#the-suite-list) states.

## When a repository adopts it

A repository adopts the suite selection when its test suites take long enough that running only the affected ones saves more than the selection costs, its upkeep included. The signs are a CI run that misses about a minute for a small change, a rising share of tests that run on every change, and a job that grows with the tests. A repository whose whole validation finishes in about a minute gains nothing; it keeps its validation as it is.

It needs:

- a validation workflow, if the repository has one: its suites move into the suite list and its setups into `setups`, and the suite jobs replace the workflow, as [Switching it on](#switching-it-on) states;
- suites whose entry files run one at a time with one command each, such as a test module with `python3 -m unittest`;
- runner labels with `strace`, `jq` and the system `python3` for every Linux suite, and with administrator rights, `python` and `dotnet` for every Windows suite, as GitHub's hosted runners have them, `ubuntu-26.04` and `windows-2025` by default; the recording step installs nothing, so a tool that a suite needs beyond these comes from its [setups](#setups) or from the [pinned tools](#pinned-tools);
- for the additions part and the full recording, a GitHub App that may push a branch and open a pull request in the repository, as [The full recording](#the-full-recording) states.

## When a repository adopted it earlier

A repository that adopted the suite selection with an earlier kit had an installed validation caller `.github/workflows/validation.yml` and a validation workflow of its own. The earlier shared workflows `suite-selection.yml` and `suite-verdict.yml` are retired; switch to the current form in one pull request:
  1. it moves each setup step of its validation workflow into a script, and states the script under `setups` of its suite list, with `tool` and its `version` or `version-file` for a setup that installs Python, Node.js or .NET, and with the `SETUP_PROVIDES` lines of the script for what the setup gives the entry files; a setup that installs Node.js or Gitleaks at the versions of the shared pins file becomes a [pinned tool](#pinned-tools) instead: its definition leaves the suite list, and the configuration file names it under `pinned-tools`;
  2. it states `jobs` in its suite list where the runner labels or the checkout depth differ from the defaults;
  3. it switches `suite-selection` on in its configuration file, as [Switching it on](#switching-it-on) shows;
  4. it removes `validation.yml` and its validation workflow.

  The required check then changes from `validation / validation` to `ci / verdict`. This changes the user's ruleset, so it is a question to the user before the pull request merges.

An earlier adoption also takes these steps where they apply:

- A caller copied from an earlier template removes `v1-moved` from the `types` of its `repository_dispatch` trigger: nothing sends that event any more, as [When `v1` moves](#when-v1-moves) states.
- A repository without the additions part adds the fixed caller `automation.yml` and the `additions` part in a pull request of its own first, as [Additions](#additions) states.
- A `full-recording.yml` or `apply-additions.yml` installed from an earlier kit has no template in this kit any more. It keeps working, since the shared steps that it calls stay at `v1`. The repository either registers the file under `local-workflows` of its configuration file, as the [pipeline kit](../../pipeline/consumer-kit/README.md#the-configuration-file) states, or switches the part on under `parts` and removes the file in the same pull request. A switched-on part runs the shared workflow instead, so the installed file is no longer needed. While the repository holds no fixed caller `automation.yml`, the job summary of a failed recording check names the manual route of the installed `Apply recorded additions` workflow, a start by hand with the run id; once it holds that caller, the summary names the route of [Additions](#additions).

The result works when these pass:

- on the pull request, the required check `ci / verdict` passes, including actionlint and zizmor over the changed workflows. Where additions are reported, the additions part commits them onto the pull request's branch and the run on the applied lists passes;
- after the merge, the same check passes on the push to the default branch;
- the full recording, started by hand on the default branch with `gh workflow run automation.yml -f part=full-recording -f verify-token=true --ref <default-branch>`, whose run passes and whose summary reports the App's token as minted.

## Switching it on

Switch the part on in `.github/ci-config.yml`, beside the parts `additions` and `full-recording`:

```yaml
version: 1
parts:
  suite-selection:
    suites: .github/suites.json
    language-steps: [python]
    pinned-tools: [gitleaks]
  additions:
    suites: .github/suites.json
  full-recording:
    suites: .github/suites.json
    validation-workflow: .github/workflows/ci.yml
```

- `suites` is the path of the suite list, which the repository writes as [The suite list](#the-suite-list) states.
- `language-steps: [python]` runs the Python language step in shard 1 (the least-loaded measured Linux shard) of the suite jobs. Drop it in a repository without Python files.
- `pinned-tools`, optional, names the [pinned tools](#pinned-tools) of the suite jobs, `node` or `gitleaks`, each once. The example names Gitleaks; its suite list then defines no setup of that name.
- `validation-workflow` is the fixed caller `ci.yml`, the workflow that runs the suites on a pull request. The full recording names it.

The configuration file and the fixed callers reach the default branch before the pull request that adopts the suite selection, as [Additions](#additions) states. The suite jobs run in `CI`, so a repository gets its suite jobs with the part on, and no other workflow of its own runs them.

## The suite list

`.github/suites.json` is JSON with the keys `always`, `jobs`, `setups` and `suites`, and `module_lookups` once a recording stores a module lookup. A suite list without `jobs` and `setups` is the form of the earlier kit, whose setups were steps of a validation workflow; its setups move into `setups` as [When a repository adopted it earlier](#when-a-repository-adopted-it-earlier) states.

```json
{
  "always": [],
  "jobs": {
    "linux": {"runner": "ubuntu-24.04", "fetch-depth": 0}
  },
  "setups": {
    "python": {"script": "ci/setups/python.py", "recorded": false, "tool": "python", "version": "3.13"},
    "node": {"script": "ci/setups/node.py", "recorded": false, "tool": "node", "version-file": "ci/pins.json#/node/version"},
    "lua": {"script": "ci/setups/lua.sh"},
    "database": {"script": "ci/setups/database.sh", "recorded": false}
  },
  "suites": [
    {
      "name": "unit",
      "entries": ["checks/test_*.py"],
      "command": ["python", "-m", "unittest", "-v"],
      "setups": ["python"],
      "extra_paths": ["fixtures/**"]
    },
    {
      "name": "web",
      "entries": ["checks/web/*.test.js"],
      "command": ["node", "--test"],
      "setups": ["node"],
      "paths": ["checks/web/*.test.js", "src/web/**"]
    },
    {
      "name": "mod",
      "entries": ["checks/run.lua"],
      "command": ["lua"],
      "setups": ["lua", "database"],
      "paths": ["checks/run.lua", "src/**/*.lua"]
    }
  ]
}
```

The example is invented. The repository writes these fields:

- `jobs`: per operating system, `linux` or `windows`, the `runner` label of its suite job (default `ubuntu-26.04` for `linux`, `windows-2025` for `windows`) and the `fetch-depth` of its checkout (default `1`). Use `0` for the whole history, for an entry file that reads older commits of the repository, such as with `git cat-file blob <sha>`. A `runner` is a label of a GitHub-hosted runner of the `ubuntu-`, `windows-` or `macos-` family, such as `ubuntu-24.04` or `windows-2022`. Any other label, key or operating system makes the suite list invalid.
- `setups`: an object with one setup per name, in the order the setups run. A setup has these keys:
  - `script`: exactly one script, a tracked repository path that ends in `.py`, `.sh` or `.ps1`. It runs from the repository root, on the job's own Python, in Bash or in PowerShell.
  - `recorded`: `true` by default. `false` runs the script before the recording starts, outside it, for a setup that cannot run traced, such as one that uses `sudo` or starts a service.
  - `tool`: optional, one of `python`, `node` or `dotnet`, with exactly one of `version`, a text, or `version-file`, a tracked JSON file and a key path in the form `<JSON file>#/<key>/<key>`. The suite job installs that tool with its pinned action before the recording starts, and the script gets its location. A suite list holds one version per tool; two setups that give the same tool different versions make it invalid, and so does a `tool: node` setup while `pinned-tools` names `node`.
  - With `setups` present, every setup that a suite states must be defined, unless the configuration file names it as a pinned tool.
- `name`: lowercase letters, digits and hyphens, unique in the suite list; it is the key in the selection's outputs and in the gates.
- `entries`: the suite's entry files, as repository paths or as glob patterns over the tracked files. An entry file is a file in any language, such as a test module, a script or a test project file.
- `command`: the program and its arguments that run one entry file, a non-empty list; the job appends the entry file's path.
- `setups`: the names of the setups that the suite needs, as [Setups](#setups) and [Pinned tools](#pinned-tools) state.
- `os`: `linux`, the default, or `windows`.
- `entry_selection`: `true` for a suite selected per entry file; each entry file then runs when a changed file matches its own lists. A suite without the key is selected as a whole.
- `entry_setups`: with `entry_selection`, more setups per entry file, an object keyed by entry file.
- `always`: patterns of paths whose change can affect every suite. The CI caller, the full recording and the suite list are none; without such a path `always` is empty.
- The extra reads, only for a read that no recording sees, such as the repository reads of a setup, each pattern no wider than that read. They add to the stored lists and never remove from them: `extra_paths` (file content), `entry_extra_paths` (file content of some entry files, an object with a list per entry file), `extra_directory_memberships` (glob patterns whose added, removed or renamed paths the suite notices) and `extra_existence_reads` (objects with a `path` and a `kind` of `file`, `directory` or `any`, for a path whose appearance or disappearance the suite notices).

The recording writes the stored lists and nobody edits them by hand: `paths`, `directory_memberships`, `existence_reads`, `lookup_directories` and `module_lookups`, per entry file `entry_paths`, `entry_directory_memberships`, `entry_existence_reads`, `entry_lookup_directories` and `entry_module_lookups`, the top-level union of module lookups under `module_lookups` and the duration estimates `entry_durations`. A suite without `paths` runs on every change until its lists are recorded.

A key of a per-entry field is an entry file named by path in `entries`, or a tracked file that a glob pattern of `entries` matches. This holds for each of them: `entry_paths`, `entry_extra_paths`, `entry_directory_memberships`, `entry_existence_reads`, `entry_lookup_directories`, `entry_module_lookups`, `entry_setups` and `entry_durations`. The recording keeps the patterns of `entries` as they are and keys the stored lists by entry file. Three rules follow, for entry files named by path and by pattern alike:

- In a suite selected per entry file, an entry file that has no stored list yet, such as a new test file that a pattern matches or a new or renamed file that `entries` names by path, runs on every change, with a reason that names the missing list, until its additions are applied as [Additions](#additions) states. The suite list stays valid, and the other entry files are selected on their own lists.
- In a suite selected as a whole, a change that adds an entry file runs the suite, with every entry file: a new test file that the pattern matches, and a file that the change names by path in `entries`. A passed result of such a suite is reused only when each of its entry files is unchanged since the run that passed, whether its stored lists match the entry file or not.
- A key that names no entry file any more, as the key of a deleted or renamed test file does, leaves the suite list valid in the fields that the recording writes, in every suite. The selection ignores it, and the next applied additions or the next full recording remove it. In `entry_extra_paths` and `entry_setups`, which the repository writes, a key that is neither an entry path nor matched by a pattern makes the suite list invalid.

Patterns use GitHub's path-filter glob syntax: `**` crosses directory separators, `*` and `?` do not. Any other key, an entry file or pattern that matches no tracked file, or a malformed extra read makes the suite list invalid and fails `select`; it never runs more instead.

Propose the first suite list from the existing validation workflows: one suite per job or step that runs a suite, named after that job or step, with its entry files, the command that runs one of them, its setups, `os: windows` on a Windows runner, and `entries` and `entry_selection` as [Naming entry files and the unit of selection](#naming-entry-files-and-the-unit-of-selection) states. A `paths:` filter of an existing workflow shows where to look for extra reads and is not copied. The suites start without stored lists and run on every change until the check of the adopting pull request names their additions, which that pull request applies as [Additions](#additions) states.

### Naming entry files and the unit of selection

A suite makes two choices in the suite list: `entries` names its entry files by path or by glob pattern, and the suite is selected as a whole or, with `entry_selection`, per entry file. Each criterion below names the fact to measure and the choice that follows.

**Naming entry files.** The fact is the listing of the tracked files, `git ls-files`: the files of the suite's directory, and those among them that a pattern matches.

- By glob, when one pattern matches every entry file of the suite and no other tracked file. The pattern names a directory and a file-name rule with an extension, such as `checks/test_*.py`. It uses `**` only for a directory tree that holds nothing but that suite's tests and their reserved helper places.
- By path, when the suite's entry files share no such rule, when a helper, fixture or data file would match it, or when the suite has one entry file, such as a test project file or a script.

A new test file does this under each choice. Under a glob, a file that the pattern matches runs from the push that adds it. In a suite selected per entry file it runs alone, on every change until its additions are applied; in a suite selected as a whole it runs with every entry file of the suite. Under a list of paths, a file outside the list is no entry file, and it runs only once the list names it.

Both criteria follow how test runners find tests and how a build system treats a glob. A runner defines a test file by one naming rule and reserves a place for what is no test. [pytest](https://docs.pytest.org/en/stable/explanation/goodpractices.html) collects two file-name patterns and requires unique base names in its default import mode. [unittest](https://docs.python.org/3/library/unittest.html#test-discovery) discovers only importable names in directories that are packages. [Go](https://pkg.go.dev/cmd/go#hdr-Test_packages) compiles the files with its test suffix and ignores directories named `testdata`. [Jest](https://jestjs.io/docs/configuration) warns that a folder whose name matches the pattern runs all its files as tests. [Surefire](https://maven.apache.org/surefire/maven-surefire-plugin/examples/inclusion-exclusion.html) includes four name patterns by default. [Cargo](https://doc.rust-lang.org/cargo/reference/cargo-targets.html) discovers the files of its test directory and keeps a manual list for special cases. Bazel's [style guide](https://bazel.build/build/style-guide) rejects a glob that matches nothing, advises against recursive globs for source files and accepts non-recursive ones, and its [`glob` documentation](https://bazel.build/reference/be/functions#glob) prefers a specific extension over a bare `*`. The name rule decides, not the number of entry files.

**The unit of selection.** The facts are the number of entry files of the suite, the sum of its duration estimates `entry_durations`, in seconds, and the stored lists of its entry files.

- Per entry file, when the suite has two or more entry files and the sum of its `entry_durations` is one minute or more.
- As a whole, when the suite has one entry file, when the sum is under one minute, or when its entry files' stored lists are equal.

One minute is the starting value, from one published evaluation of Java projects: selection roughly halved the testing time of suites that take over a minute, and every slowdown was a short suite ([Gligoric 2015](https://users.ece.utexas.edu/~gligoric/papers/Gligoric15PhD.pdf), section 3.3). From there measured runs of the repository decide, as the goal and the Measurement and growth principle of the [CI guide](../../pipeline/consumer-kit/docs/agents/ci.md) say. The full recording and applied additions write `entry_durations`; for a suite without them, as in a first suite list, the sum is the measured time of the job or step that runs the suite now.

The entry file is the unit because selection systems select at the test artifact that runs on its own, from file-level inputs: the test target at [Meta](https://arxiv.org/abs/1810.05286), the test class in [Develocity](https://docs.develocity.ai/predictive-test-selection/), the test with its covered files at [Datadog](https://docs.datadoghq.com/tests/test_impact_analysis/). Selection per class outperformed selection per method ([Legunsen et al. 2016](https://mir.cs.illinois.edu/~marinov/publications/LegunsenETAL16StaticRTS.pdf)). Each unit that starts on its own has a cost: Develocity's [test distribution](https://docs.develocity.ai/test-distribution/) advises it only for tasks that take more than a few seconds and names the overhead of one partition per test class.

**Moving between the choices.** A suite moves when the sign of a move holds. Each move is one edit to the suite list:

- **Path to glob.** Sign: `git ls-files` shows that one pattern of a directory and a file-name rule with an extension matches every entry file of the suite and no other tracked file. Edit: replace the paths in `entries` by the pattern; the stored lists and the other per-entry fields keep their keys, one per entry file. Next run: the edit selects no entry file of the suite, so the run runs none for it and records nothing. A tracked file that the pattern matches and no path named runs as an added entry file does.
- **Glob to path.** Sign: `git ls-files` shows a tracked file that the pattern matches and that is no entry file, such as a helper, a fixture or a data file, or entry files that no longer share one rule. Edit: replace the pattern in `entries` by the path of each entry file, and remove from `entry_extra_paths` and `entry_setups` each key that names none of them; a key left behind there makes the suite list invalid. A key of a field that the recording writes stays until the next applied additions or full recording remove it, and a new test file whose additions are not applied yet may be named as well. Next run: the edit selects no entry file of the suite that has a stored list, so the run records nothing new for those. In a suite selected per entry file, a named entry file without a stored list runs on every change until its additions are applied, and the next applied additions or full recording drop from the suite's lists what only the removed file read.
- **Whole to per entry file.** Sign: the suite has two or more entry files and the sum of its `entry_durations` is one minute or more. Edit: add `entry_selection: true` and remove the suite's stored lists `paths`, `directory_memberships`, `existence_reads`, `lookup_directories` and `module_lookups`; a `paths` left behind makes the suite list invalid. `entry_durations` and the extra reads stay. Next run: the suite has no stored list, so the run runs every entry file and its check reports the reads of each as additions. Applying them records a list per entry file and the suite's lists as their union. From then on a change runs the entry files whose own lists it reaches.
- **Per entry file to whole.** Sign: the suite has one entry file, the sum of its `entry_durations` is under one minute, or the stored lists of its entry files are equal. Edit: remove `entry_selection` and the per-entry lists `entry_paths`, `entry_directory_memberships`, `entry_existence_reads`, `entry_lookup_directories` and `entry_module_lookups`, and state the setups of `entry_setups` under `setups` and the patterns of `entry_extra_paths` under `extra_paths` in place of those two keys. A per-entry list, an `entry_setups` or an `entry_extra_paths` left behind makes the suite list invalid. The suite's own lists and `entry_durations` stay. Next run: the changed unit of selection runs every entry file of the suite, and the run records nothing new, since the suite's lists already hold the union of its entry files' lists. From then on a change that reaches the suite's lists runs every entry file.

## Setups

A setup is what a suite needs installed before its entry files run, such as an interpreter or a tool. The suite list names each setup once under `setups` and states, per suite, the setups that its entry files need under `setups`, and per entry file under `entry_setups`. Each suite job runs the setups that its own entry files state, and no others. Each setup installs into a location of its own, and an entry file sees only the setups it states.

A setup's script runs in one of two phases, in the order of the suite list:

- A setup with `recorded: false` runs before the recording starts, outside it.
- Every other setup runs after the recording starts, under it.

A setup with `tool` gets its tool installed by the pinned action before both phases, and its script gets the tool's location. Each script receives these environment variables:

- `SETUP_NAME`: the setup's name;
- `SETUP_DIRECTORY`: a directory of its own, `$RUNNER_TEMP/setups/<name>`, where it installs;
- `SETUP_PROVIDES`: a file, to which the script appends the lines that state what the setup gives the entry files that state it:
  - `path=<directory>`: a directory that goes first on the `PATH` of the entry files, in the order of the stated setups;
  - `variable=<name>=<value>`: a variable of the entry files;
  - `python=<interpreter>`: a Python interpreter into which the lookup report of the recording is installed, so the recording sees the module lookups of its imports;
  - `write=<directory>`: an absolute writable directory that the setup grants to the entry files;
- `SETUP_TOOL`, for a tool setup only: the interpreter for `python`, the directory of its programs for `node`, its root for `dotnet`.

A setup that gives nothing writes no line. A setup step writes nothing to `$GITHUB_PATH` or `$GITHUB_ENV`. An entry file runs in the job's environment before its setups, with only what its stated setups provide and the job's default environment variables. The suite jobs have no `env` of the repository's own, so a value that an entry file needs and no setup provides reaches it in one of two ways:

- a value from the `runner` context reaches the entry file under its own name through its default environment variable, such as `RUNNER_TEMP` for `runner.temp`, which the job's environment already holds;
- any other value reaches it only through a `variable=` line of a setup that provides it.

`exec` gives each entry file a temporary directory of its own in `TMPDIR`, and on Windows in `TEMP` and `TMP` as well; no other value replaces them, neither a job default nor a `variable=` line.

`exec --entry` fails when the job did not install a setup that the entry file states. What a test writes outside its own temporary, bytecode-cache and lookup directories and what its setups grant through `write=`, such as `$HOME` or the shared checkout, is no input of selection; a result that depends on such a write is a flaky result, and `check` reports such a write as a departure while the test still runs. A job that records restores no cache that holds build or test results and turns off build servers and node reuse.

### Pinned tools

A pinned tool is a setup that the suite jobs give at the version of the shared pins file, so the repository states no version for it. The configuration file names the pinned tools under `pinned-tools` of the part `suite-selection`: `node`, for Node.js, and `gitleaks`, for Gitleaks. The suite list defines no setup of such a name. A suite states the pinned tool under `setups`, or an entry file under `entry_setups`, as it states any other setup, and the suite job gives it only to the entry files that state it, on their `PATH`, in the order of the stated setups.

- `node` is installed by the pinned action at the Node.js version of the pins file, as a setup with `tool: node` is, and the entry files get the directory of its programs.
- `gitleaks` is installed from the pins file before the recording starts, outside it, for the platform of the job's system, and the entry files get its directory. A sha256 that differs from the pin, or a platform that the pins file has no asset for, fails the job before any entry file runs.

The rules:

- A suite that states `node` or `gitleaks` while `pinned-tools` does not name it makes `select` fail with the undefined setup, as it does for any other setup. A suite list that defines a setup of a name that `pinned-tools` names fails `select` as well, and so does a `tool: node` setup beside a pinned `node`.
- A name outside `node` and `gitleaks` in `pinned-tools`, or the same name twice, fails the check of the configuration file.
- The commands that only read the suite list accept the two names without a definition. `select` decides on them.
- A passed result is reused only with the same pin: the version, and for Gitleaks its assets with their sha256. A version change in the shared pins file moves that tool's digest and reruns only the units that state that tool.
- A setup whose definition the suite list removes, so that its name becomes a pinned tool, runs the entry files that state it, as a changed setup definition does.

## The suite jobs

The suite jobs are the shared workflow `suite-jobs.yml` at `v1`, which the pipeline's part `suite-selection` calls: one job per Linux shard, named `shard <n>`, and one Windows job. They run on the runner labels and with the fetch depth that the suite list's `jobs` states. Each job runs the same steps in this order, on Linux and on Windows:

1. the checkout;
2. the recording step;
3. the tools that the job's setups name, each installed by its pinned action, Node.js among them when `node` is a pinned tool;
4. the pinned Gitleaks, when `gitleaks` is a pinned tool of the job, installed outside the recording;
5. the setups with `recorded: false`, in the order of the suite list, outside the recording;
6. the start of the recording;
7. the other setups, in the order of the suite list, under the recording, with what they provide;
8. the entry files that the job runs, two at a time on Linux and one after another on Windows, each under the recording and in a log group of its own;
9. the check of the recording against the suite list;
10. on Linux shard 1, the Python language step (shard 1 is the least-loaded measured shard), when `language-steps` names Python;
11. on a pull request, the list of the entry files that passed, and its upload for a later head.

A shard runs only the setups that its entry files state, and the pinned tools among them. When an entry file fails, its job fails after every entry file of the job has run.

## Jobs and checks

| Check | Runs |
| --- | --- |
| `ci / suite-selection` | `select`: the changed files, each suite's decision and the shards |
| `ci / suite-selection-jobs / shard <n>` | one Linux shard of the suite jobs |
| `ci / suite-selection-jobs / windows` | the Windows job of the suite jobs, when a Windows entry file is selected |
| `ci / suite-selection-results` | on a pull request, keeps the passed results for a later head |
| `ci / verdict` | the one required check: it fails when a job it needs failed or was cancelled; a skipped job is a suite that the selection did not run |

The required check is `ci / verdict`; none of the other checks is required on its own.

## What selects a suite

`select` compares the changed files of the event with each suite's stored lists and extra reads. A suite runs when a changed file matches its paths, changes a directory membership or an existence read it makes, or adds a module name that it looks up; a suite selected per entry file runs the entry files whose own lists match. Every suite runs when the changed files cannot be determined, a changed file matches an `always` pattern, or a change touches a tracked `.gitignore` file or adds a path that the ignore rules match. A changed file that no suite reads selects none.

A change to what runs a suite selects it as well:

- a changed `command`, `setups`, `os` or `entry_selection` of a suite runs every entry file of the suite, and a changed `entries` or `entry_setups` the entry files it adds or changes; a tracked file that a pattern of `entries` starts to match runs as an added entry file does, in a suite selected as a whole with every entry file of the suite; a removal from the stored lists or the extra reads runs what it was removed from;
- a changed setup definition in `setups`, such as its `script`, `tool` or `version`, runs the entry files that state the setup; so does a changed script file or version file of a setup, and so does a definition that the suite list removes to make its name a [pinned tool](#pinned-tools);
- a changed `jobs` entry runs the entry files of its system;
- a change of the shared suite jobs, the shared recording step, a pinned tool or a language step reaches the repository through its platform digest, tool digest or language-step digest: `select` takes them and reruns only the checks whose digest moved, as [When `v1` moves](#when-v1-moves) states.

A changed `on` or `concurrency`, a name or comment, the job that runs `select`, and a step after the entry files that only stores or reports results select nothing.

The `select` job's summary lists every suite with its decision and reason. On a pull request, a later head reuses a passed result whose reads, command, setup versions, pins of its pinned tools, runner label, platform, tool and language-step digests and tracked ignore rules did not change; for a suite selected as a whole, its entry files are unchanged as well; a re-run reuses nothing.

## The recording and its check

The recording step records what each setup and entry file reads, on Linux under `strace` and on Windows through an ETW session, and its `check` compares the reads with the stored lists of the suites and entry files that ran. A read of a path that the ignore rules match and that is not tracked is no read, so build output and caches select nothing. A setup's reads belong to the lists of every suite and entry file in its job that states it.

Each read outside the stored lists is an addition: `check` lists the additions in the job summary, uploads `recording-additions-*` as an artifact, and fails the job, so `ci / verdict` fails. A suite without a stored list always reports additions. When a shard has no read additions but recorded exit-0 entry times or clean list replacements, `check` uploads `recording-entry-times-*` and passes.

## Additions

The additions part applies the additions of a suite run to the suite list. The pipeline runs it in the fixed caller `automation.yml` of the [pipeline kit](../../pipeline/consumer-kit/README.md#the-fixed-callers), next to the [full recording](#the-full-recording). After a failed pull-request run it commits the change onto the pull request's branch; started by hand with a run id, it proposes the change as a pull request onto that branch. Its jobs report the checks `automation / additions` and `automation / additions-comment`.

Switch the part on as [Switching it on](#switching-it-on) shows. When the check of a pull request's run reports additions, `ci / verdict` fails and the job summary of each failed suite job names both routes below.

### The automatic route

The job `additions` runs on `workflow_run` when a run of the workflow `CI` completes, the trigger that the fixed caller names. It acts only when all of these hold:

- the run's event is `pull_request`, the run failed, and its head branch lies in the repository itself; a pull request from a fork gets no automatic route;
- the run uploaded at least one unexpired `recording-additions-*` artifact;
- an open pull request has the run's head branch as its head and the run's head commit as its head SHA.

It checks out the run's head commit, downloads every `recording-additions-*` and `recording-entry-times-*` artifact of the run and applies every `additions.json` of the download in one `recording.py apply` of the recording step at `v1`, before the App's token exists and with no token in that step's environment (writing raw entry times for exit-0 entry files through the 25% threshold, without speed correction). Runs that uploaded only `recording-entry-times-*` artifacts trigger no commit. An `additions.json` that is no valid additions document fails the job there, and a change to any file other than the suite list fails the job before the token. When the suite list changed, the job commits it as the GitHub App onto the pull request's branch with the subject `chore: apply the recorded additions of run <run id>` and `[dependabot skip]` in the body, scans the commit with the pinned Gitleaks and pushes it with a lease on the run's head commit. It opens no pull request. `[dependabot skip]` keeps Dependabot rebasing its pull request over the commit.

The App's push starts `CI` on the new head. There `select` reuses the passed result of every unit that passed in the failed run, an entry file with additions and a pooled suite with additions included, as long as the new head changes nothing that the unit read under the applied lists. A unit whose stored lists name the suite list runs again, since the suite list changed, and so does a unit whose stored lists on the new head differ from those that the failed run's additions give. The failed run stays red; the required check `ci / verdict` on the new head decides the merge.

Each push by a person or agent gets at most one automatic round: when the failed run's head commit carries the subject of applied additions, the workflow applies nothing. The subject decides, not the author, since the same App also makes other commits.

The job `additions-comment` posts one comment on the pull request with the reason and the manual route, in three cases: at the bound, when applying leaves the suite list unchanged, and when applying, the token, the push or another step fails. It posts no comment otherwise. The comment names the manual route with the file of the fixed caller, `automation.yml`: `gh workflow run automation.yml -f part=additions -f run-id=<run id>`. A head that moved since the run, or a pull request that closed, stops both routes without writing and ends the automatic route without a comment. Runs for one head branch share one concurrency group without cancel, so a later run waits for the earlier one and then finds the head moved. GitHub keeps one waiting run per group and cancels an older waiting one when another arrives; a cancelled run commits and comments nothing.

The route covers every pull request whose head branch lies in the repository, Dependabot's included, without a label. When Dependabot rebuilds its pull request over the App's commit, the next failed run applies the additions again. [Dependabot auto-merge](../../dependabot-auto-merge/consumer-kit/README.md#switching-it-on) accepts the App's commit when its part names the suite list's path in `suites`. A suite list that the base branch changed in the meantime can conflict with the applied additions; the pull request resolves that conflict like any other.

### The manual route

Started by hand, the job proposes the additions of a run as a pull request. It serves a run that the comment names and a run that the automatic route does not cover:

1. Take the run id from the address of the `CI` run, `.../actions/runs/<run id>`.
2. Start the workflow `Automation` on the default branch with the part and that run id: `gh workflow run automation.yml -f part=additions -f run-id=<run id>`.
3. Review its pull request `chore: apply the recorded additions of run <run id>` from `chore/apply-recorded-additions-<run id>`, whose base is the reporting pull request's branch and whose only change is the suite list, and merge it into that branch once its own checks pass.
4. The merge updates the reporting pull request, whose `CI` then runs again on the applied lists.

The job reads the run and its pull request, checks out that pull request's head branch, downloads every `recording-additions-*` and `recording-entry-times-*` artifact of the run and applies every `additions.json` of the download in one `recording.py apply` of the recording step at `v1`, at any depth of the download folder: `actions/download-artifact` extracts a single artifact directly into it and each of several into a folder named after the artifact. When the suite list does not change, it opens no pull request and its summary says so. It fails with the reason and opens nothing when the run has no `recording-additions-*` artifact, ran on an event other than `pull_request`, or comes from a pull request whose head branch lies in a fork, since the App proposes only onto a branch of the repository itself. A later start with the same run id updates the open pull request of that run.

### Tokens

The workflow token of `additions` only reads the run, its artifacts, the open pull requests and the checkout; `additions-comment` writes the comment with a workflow token that may write pull requests and checks out nothing. The commit and the pull request are made by the GitHub App of [The full recording](#the-full-recording). The fixed caller passes the secret `PUBLICATION_CHECKS_APP_PRIVATE_KEY` by name, and the repository variable `PUBLICATION_CHECKS_APP_CLIENT_ID` is still read; without them the workflow fails with the App setting it needs and, on the automatic route, comments with the manual route.

## The full recording

The full recording is the periodic safety net of the stored lists and renews duration estimates; complete pull-request runs also replace stored lists when applied. The pipeline runs it as the part `full-recording`, in the fixed caller `automation.yml` of the [pipeline kit](../../pipeline/consumer-kit/README.md#the-fixed-callers). Switch it on as [Switching it on](#switching-it-on) shows. It runs on the default branch on a weekly schedule on Mondays, where it records only in even ISO weeks, and whenever it is started by hand with `gh workflow run automation.yml -f part=full-recording [-f verify-token=true] --ref <branch>`.

The job `full-recording` assigns every entry file to shards with `select --full`. The job `full-recording-jobs` calls the shared workflow `suite-jobs.yml` with `full: true`, which runs the suite jobs of [The suite jobs](#the-suite-jobs) in the full-recording mode: each shard runs its setups and entry files under the recording, and the Windows job runs every Windows entry file in one job. The runner labels, the fetch depth and the setups are those of the suite list. The job `full-recording-close` on `ubuntu-slim` checks that every planned shard and Windows job produced a result, failing visibly and opening no pull request when any result is missing, joins the results and replaces the stored lists of each clean entry file (one that exited with `0` and whose test report shows no skipped or deselected test), applies complete suites while reporting partly recorded suites and keeping other lists with the reason, and renews the duration estimate of every entry file that exited with `0`: it corrects each measurement for the speed of its shard job (computed over clean entry files only), which `start --job` names, and changes a stored estimate only when the corrected value differs from it by more than 25%. On the default branch it applies the results to the suite list of the default branch's tip as `close` fetches it, and proposes the changed suite list as the pull request `chore: update the recorded path lists` from `chore/update-recorded-lists`, a commit onto that tip, with the report as its body. It then asks GitHub to merge that pull request by squash once its required checks have passed, with `gh pr merge --auto --squash` bound to the pushed commit, so that a head which moved since the push does not merge on that request. The repository setting [Allow auto-merge](#repository-settings) must allow it. When the default branch moved during the run, `close` selects the change since the recorded commit as a push would select it, and each entry file that the change selects keeps its stored reads beside the recorded ones, with the reason in the report; when that selection fails, every entry file keeps them. A result for a suite or an entry file that the tip no longer holds is left out and named in the report. A run on another branch applies to the commit it started from, uploads the joined result as the artifact `full-recording-joined` and opens no pull request.

The setups are the suite list's, as [Setups](#setups) states, and the pinned tools are those of the configuration file, as [Pinned tools](#pinned-tools) states; the full recording names no setup of its own. A setup that cannot run in the full recording is a question for the suite list, not for the workflow.

A run started by hand on the default branch with the input `verify-token` set to `true` mints the App's token even when the suite list did not change, and its run summary reports whether that succeeded; this verifies a new private key. Without a change it proposes no pull request.

The closing job makes the pull request as a GitHub App installed on the repository with Contents and Pull requests read and write, with the App's client id in the repository variable `PUBLICATION_CHECKS_APP_CLIENT_ID` and its private key in the repository secret `PUBLICATION_CHECKS_APP_PRIVATE_KEY`. Without them it uploads the joined result and fails with the App setting it needs. Both parts scan the commit before the push with the pinned Gitleaks, which reads `.gitleaks.toml` and `.gitleaks-baseline.json` by those fixed names when the repository holds them.

## Repository settings

The user configures these settings; the workflows change none of them:

- A ruleset on the default branch requires `ci / verdict` with GitHub Actions as its source. The ruleset editor lists a check only after it succeeded in the repository within the past 7 days, so the pipeline reaches the default branch and reports on a pull request before the ruleset names it.
- For the additions part and the full recording, the App installation, the repository variable and the repository secret that [The full recording](#the-full-recording) names.
- Allow auto-merge, in the repository's settings for pull requests, so that the pull request `chore: update the recorded path lists` merges itself once its required checks have passed, as [The full recording](#the-full-recording) states. Without it the closing job fails with the App setting it needs.

## When `v1` moves

The recording step, the suite jobs, the pinned tools, the language steps, the pipeline parts and the hygiene checks come from `CrankyAnt/SharedWorkflows` at `v1`, not from the repository. `plan` and `select` take one platform digest per operating system (`platform:linux`, `platform:windows`), one tool digest per pinned tool (`tool:<name>`), one language-step digest per shared language step (`language:<name>`), one part digest per pipeline part (`part:<name>`) and one hygiene-check digest per hygiene check (`hygiene:<name>`), without the selection code, and compare them with the digests of the last passed default-branch record (or, on a merge whose tree passed on its pull request or onto a newer base, with the digests the pull request was tested under); a digest missing on either side counts as moved. When a digest differs, only the checks that use it run (or every check when that record or its digests cannot be read).

No move of `v1` sends the repository an event, and Shared Workflows asks GitHub nothing about the repositories that call it. After a move that changes a digest, each pull-request run reruns the checks affected by that digest until a run on the default branch, a push or a Dependabot merge, has passed under the new digests. Every pull-request run that starts after that run takes it as the last passed run on the base branch and runs its normal selection. The merge of the first pull request after the move is such a push: its tree equals the tree that passed on the pull request under the new digests, so it runs no suite again and passes.

When `v1` moves during a run, a suite job fails on its digests; start the whole run again, since a re-run of the failed jobs keeps the earlier `select`.

## A skipped suite

When a run skipped a suite that had to run, read the summary of its `select` job: the changed files and each suite with its decision and reason. A read that no recording sees, such as a branch that no run takes, goes into the suite's extra reads, or into `always` when every suite reads it. A read that the recording should have seen is a fault of the shared part: report it to the user with the summary and the recording of the run.
