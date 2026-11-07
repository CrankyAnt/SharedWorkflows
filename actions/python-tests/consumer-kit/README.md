# Python tests

The consumer kit of Python tests, a shared part of the [catalogue](../../../catalogue.json). A repository adopts it through one caller, [python-tests.yml](python-tests.yml), which calls the shared workflow `CrankyAnt/SharedWorkflows/.github/workflows/python-tests.yml@v1`. Python tests runs a repository's Python test suite with the standard test runner and the Python language step in one job.

## When a repository adopts it

Python tests applies to a repository whose Python tests are short enough that running only the affected suites would not save more than the suite selection costs, such as a suite of pytest tests that runs in seconds. A repository runs its Python tests through either the suite selection or Python tests, never both. A repository that uses the suite selection does not adopt Python tests; its validation workflow runs the Python language step in shard 1 instead.

A repository whose own workflow already runs its tests adopts Python tests in place of that job: the caller replaces the job that installs Python and the dependencies and runs ruff, mypy and the tests.

## What the job runs

The shared job `tests` runs on one runner label, in this order:

1. It checks out the change and decides whether the tests run. They do not run when every changed file ends in `.md`, in any case, or when the run is a push or a `dependabot-merged` run whose tree already passed `tests / tests` on its pull request. In both cases the job succeeds without installing anything. Any other change runs them; no finer path selection applies.
2. It installs `python-version` with `actions/setup-python` as the job's own Python, without a cache.
3. It installs the dependencies with pip from `working-directory`: an `install` value that ends in `.txt` is a requirements file, installed with `pip install -r <file>`, and any other value is an extra of the project, installed with `pip install ".[<extra>]"`. Without `install`, nothing is installed.
4. It runs the [Python language step](../../python-language-step/consumer-kit/README.md) from `working-directory`: `ruff check`, `mypy` where that directory configures it, and `ruff format --check` with `format-check: "true"`.
5. It runs the tests in `working-directory`, also when the language step failed: `python -m pytest` when the project configures pytest, otherwise `python -m unittest discover`.

The project configures pytest with a `[tool.pytest]` or `[tool.pytest.ini_options]` table in `pyproject.toml`, a `pytest.toml`, `.pytest.toml`, `pytest.ini` or `.pytest.ini`, a `[pytest]` section in `tox.ini`, a `[tool:pytest]` section in `setup.cfg`, or a `conftest.py` in `working-directory`.

The job fails when the language step or the tests fail. It also fails with a message that names `working-directory` when the test runner finds no tests, and with a message that pytest is not installed when the project configures pytest but the installed dependencies do not provide it. pytest and its plugins come from the repository's own dependencies: list them in the requirements file or the extra that `install` names.

## The caller

Copy [python-tests.yml](python-tests.yml) to `.github/workflows/python-tests.yml` and replace its placeholders:

- `<default-branch>` with the repository's default branch;
- `<python-version>` with the Python version the repository tests on, `3.11` or newer, such as `3.14`, inside the quotes, so that a version such as `3.20` stays a string and does not become `3.2`;
- `<install>` with the requirements file or the extra that holds the test dependencies, or remove the line when nothing needs installing.

The caller sets `permissions: {}` at the top and `actions: read`, `contents: read` and `pull-requests: read` on its one job, which the push lookup needs. It passes no secrets and runs no step of its own. It runs on pull requests, on pushes to the default branch, and on the `repository_dispatch` event `dependabot-merged`, which Dependabot auto-merge sends after each merge; on that event the job checks out the commit that `client_payload.sha` names, or the default branch's tip without one, and fails without running the tests when `client_payload.sha` is malformed, absent from the checkout, or a commit off the default branch. A newer head of a pull request cancels the run of the older one; a run on the default branch is never cancelled.

The job id `tests` gives the check `tests / tests`, the check that Python tests adds to the checks the default-branch ruleset requires. With Dependabot auto-merge, the caller of that part names this caller's workflow, `Python tests`, among the workflows that produce a required check.

## Inputs

| Input | Default | What it sets |
| --- | --- | --- |
| `python-version` | required | the Python version that `actions/setup-python` installs, `3.11` or newer: the language step and the tests run on it |
| `working-directory` | `.` | the project's directory, relative to the repository root, from which the dependencies install and the language step and the tests run |
| `install` | empty | a requirements file ending in `.txt`, or an extra of the project; empty installs nothing |
| `runner-label` | `ubuntu-26.04` | the runner label of the job, such as `windows-2025` for tests that need Windows |
| `format-check` | `"false"` | `"true"` also runs `ruff format --check` |

The language step reads the ruff and mypy configuration from `working-directory`, as the [Python language step](../../python-language-step/consumer-kit/README.md#what-turns-each-check-on) says.

## Several test roots

A repository with several test roots, each with its own dependencies, adds one caller job per root, each with its own `working-directory`, `install` and job id, such as `tests-parser` and `tests-tools`. Each job gives its own check, such as `tests-parser / tests`, and the default-branch ruleset requires each of them. A single caller file may hold all these jobs.

## Moving to the suite selection

A repository whose tests grow until running only the affected suites saves more than the suite selection costs moves to the suite selection in one pull request: it adopts the suite selection as its consumer kit says, with the Python language step in shard 1 of the validation workflow, and removes the Python tests caller. Its default-branch ruleset then requires `validation / validation` in place of `tests / tests`; change the ruleset right before that pull request merges.

## When a repository adopted it earlier

A repository that adopted Python tests earlier brings its installed caller onto the current template in one pull request. It compares the installed file with the template it came from, read at `v1`:

| Template | Install target |
| --- | --- |
| [python-tests.yml](python-tests.yml) | `.github/workflows/python-tests.yml` |

Every difference that the template makes is taken over, apart from the repository's own values, which carry over:

- the default branch in place of `<default-branch>`;
- the inputs the caller sets under `with:`, each with its value: `python-version`, `install`, `working-directory`, `runner-label` and `format-check`;
- each further caller job of a repository with several test roots, with its job id and inputs.

The repository's ruff, mypy and pytest configuration and its dependency files come from no template and stay as they are.

The result works when `tests / tests`, and the check of every further caller job, passes on that pull request.

## When `v1` moves

A move of `v1` changes the later runs of the caller without a change in the repository. A new ruff version in the Python language step can report a finding that the repository already holds; fix it in the same pull request, or set the rule in the repository's ruff configuration, as the [Python language step](../../python-language-step/consumer-kit/README.md#when-v1-moves) says. A failure that the repository cannot fix this way is a fault of the shared part: report it to the user.
