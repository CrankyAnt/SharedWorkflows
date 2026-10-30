# The test suites

`.github/workflows/validate.yml` runs this repository's suites. Its job `select` selects the entry files that a change reaches from the suite list `tests/suites.json`; the Linux entry files run in shards on `ubuntu-26.04` and the Windows entry files in one job on `windows-2025`, each entry file under the recording step. This file says which suites there are, how a suite is added, removed and kept in step with what it reads, and how an entry file runs locally with the tools its cases need. "Suite list" and "Job layout" in `docs/maintaining.md` document the suite-list format, the selection rules and the jobs; this file points to them and does not repeat them.

## The suites in `tests/suites.json`

A suite is one item of `suites` in `tests/suites.json`. It states its name, its entry files, its `command`, the `setups` it needs, a duration estimate per entry file in `entry_durations`, and its stored path list with any directory memberships, existence reads, lookup directories and extra reads. A Windows suite states `"os": "windows"`; `windows` and `etw-consumer-build` are such suites. The `always` list is empty.

Every entry file runs as its own process with its suite's `command` followed by the entry file's path: `python3 -m unittest -v <entry file>` for the Linux suites and `python -m unittest -v <entry file>` for `windows` and `etw-consumer-build`. A shard runs the setups that its entry files need, then the entry files two at a time, then the recording's `check`. The setups of this repository are `python`, `node`, `commitlint`, `actionlint`, `zizmor`, `gitleaks`, `reuse`, `ruff`, `mypy` and, on Windows, `dotnet`. An entry file that needs a setup the rest of its suite does not, such as a hygiene entry file that needs `actionlint`, states it in `entry_setups`.

Several suites are selected per entry file (`entry_selection`), among them `ci-policy`, `hygiene`, `publication-checks` and `suite-selection`. Each entry file has its own stored list (`entry_paths`), and a change starts only the entry files whose own lists it reaches. `entry_extra_paths` and `entry_directory_memberships` record reads that apply to only some entry files.

`ci-policy` (`tests/test_ci_policy_*.py`) is the light suite: it reads tracked files in its own process, with no tool installation, no network and no fixture Git repository, and holds the checks over every workflow and over the shared surface of `actions/`. Each part's suite parses the invocations of its scripts in its shared workflows with the scripts' own parsers (`tests/workflow_commands.py`), so a change to a part's workflow runs `ci-policy` and that part's suite. The link check over this checkout's Markdown is a check of the hygiene caller, not of `validate.yml`; its cases on fixture Markdown are `tests/test_link_check.py` in the `hygiene` suite.

The first Linux shard also runs the Python language step outside the recording: ruff with the rule sets `F` and `E9` of `ruff.toml`, without mypy. The `python-floor` suite checks the shared Python code against Python 3.12.

### Adding a suite

A new suite is one item in `tests/suites.json` with `name`, `entries`, `command`, `setups`, `os` for a Windows suite, and `entry_selection` for a suite selected per entry file. It has no `paths` at first, so it runs on every change. The recording `check` of its first run names its reads as additions; apply them with `recording.py apply`, as "Validation" in `docs/maintaining.md` says, in the change that adds the suite. A suite whose entry files need a setup that no suite has yet also needs that setup in the shard job of `validate.yml` and of `full-recording.yml`, which `ci-policy` keeps equal.

A new entry file in an existing suite joins that suite's `entries` the same way, with its own list from the additions of its first run.

### Listing tracked paths

A test lists tracked paths with `git ls-files` and a pathspec of what its result depends on, such as the paths it checks or the patterns of the files it looks for, from the repository root and without Git's own options such as `-C`. It lists every tracked path only when its result depends on the whole tracked set. "Recording step" in `docs/maintaining.md` says which reads the recording takes from each pathspec.

### Removing a suite

A suite leaves `tests/suites.json` with its entry files, unless another suite names them. A setup that no other suite needs leaves the shard jobs of `validate.yml` and `full-recording.yml`.

## Recording-step tests

The entry files of the recording step, `tests/test_recording_*.py` in the `suite-selection` suite, start no tracer and run under the recording like every other entry file. The classification rules run on stored strace recordings and on recorded neutral ETW event files; the wiring and full-recording tests run against the stand-in `strace` of `tests/strace_stand_in.py`, which `RecordingFixture` in `tests/recording_fixtures.py` puts first on the fixture's `PATH`. A new command of these tests belongs to a `StoredFixture` and needs a stored trace; "Renew the stored strace recordings" in `docs/maintaining.md` describes the stored files and their regeneration script. The live ETW cases of `tests/test_windows_recording.py`, the attribution of two entry files that run at the same time included, run under the Windows job's own ETW session.

## Windows suite

The Windows suite is `tests/test_windows_recording.py`, run by the `windows` job of `validate.yml` on `windows-2025` under the ETW recording. It covers the recording step on Windows. The job sets `TEMP` and `TMP` to the runner's long-form temporary directory, because the runner's own `TEMP` is an 8.3 short path.

## ETW consumer build suite

The `etw-consumer-build` suite is `tests/test_etw_consumer_build.py`: it rebuilds the ETW consumer of the recording step from its source with the pinned .NET SDK and fails when the result differs from the committed build in `actions/recording/etw-consumer/build/`. It runs in the `windows` job after the `dotnet` setup. Its rebuild test skips without `dotnet` on `PATH` or off Windows; its test of the `dotnet publish` command line runs everywhere, with `subprocess.run` replaced. Its extra read is the SDK pin `global.json`, which the setup reads outside the recording; the configuration files that the build probes in the project's directory and above come from the .NET probe rules of the recording step.

## Running the suites

CI is the only gate, and no local copy of the CI's selection runs before a push. While building, run the entry files of the changed behaviour, each with its suite's `command` from `tests/suites.json`, from the repository root. Python commands set `PYTHONPYCACHEPREFIX` to an external cache directory. The Windows suites run on Windows with `python -m unittest -v <entry file>`.

```text
python3 -m unittest -v tests/test_suite_selection.py
```

The `-v` output of a `python -m unittest` entry file lists every skipped case with its reason.

### Tools the suites need

The suites need Python, and selected publication entry files need Node.js and npm. Selected hygiene and publication entry files also need tools that a checkout does not contain:

- Hygiene (`hygiene`). Only the actionlint, zizmor, Gitleaks and REUSE entry files need installed executables; the actionlint entry file also needs ShellCheck. Other hygiene entry files use repository files, Git or fixture archives. Those entry files take tools from `HYGIENE_TOOLS` when set, otherwise from `PATH`, and skip when a required executable is missing. Install the pinned versions for the platform, `windows-x64` or `linux-x64`, into an empty directory outside the repository, and set `HYGIENE_TOOLS` to it:

  ```text
  python actions/repository-hygiene/install_tools.py --pins actions/repository-hygiene/pins.json --platform <platform> --destination <directory>
  ```

- Publication checks (`publication-checks`). The two commitlint cases skip unless `PUBLICATION_CHECKS_TOOLS` names a directory where `npm ci` installed the locked toolchain; they do not look on `PATH`. Copy `package.json` and `package-lock.json` from `actions/publication-checks` into an empty directory outside the repository, install the toolchain there with the command of the `commitlint` setup, and set `PUBLICATION_CHECKS_TOOLS` to that directory. `--userconfig /dev/null` keeps the user's npm configuration out of the install; on Windows, `NUL` takes the place of `/dev/null`. A local run needs a Node.js version within `engines.node` of the part's `package.json`; CI runs the version that `node.version` in the pins file pins:

  ```text
  npm ci --ignore-scripts --no-audit --no-fund --userconfig /dev/null --prefix <directory>
  ```

- Windows suites (`windows`, `etw-consumer-build`). Windows; the cases skip on other systems. The live ETW recording cases of `tests/test_windows_recording.py` also need `dotnet` with the .NET runtime that the ETW consumer's project names, and administrator rights, and skip without them.

- Python language step and Python floor (`python-language-step`, `python-floor`). The pinned ruff and mypy from the `python` tools of the pins file, in `HYGIENE_TOOLS` or on `PATH`, which `install_tools.py` installs with `--tool ruff --tool mypy`; the cases skip without them.

With these tools, the only cases that still skip are those that depend on the platform or the Python version.
