# Python language step

The consumer kit of the Python language step, a shared part of the [catalogue](../../../catalogue.json). A repository with Python files adopts it in the pipeline, which runs the step itself, as [How the pipeline runs it](#how-the-pipeline-runs-it) states, or, outside the pipeline, by adding one step, [python-language-step.yml](python-language-step.yml), to a job that installs Python. The step calls the shared step `CrankyAnt/SharedWorkflows/actions/python-language-step@v1`.

## What the step runs

The step installs its pinned tools into the runner's temporary directory with the job's own Python, `python3`, or `python` on Windows. It installs no interpreter, so the job installs Python before the step and keeps that Python as the job's own. It then runs these checks from its root, the repository root unless the step passes `root`:

- `ruff check` with the repository's ruff configuration, or ruff's default rules without one. It writes no cache into the checkout.
- `mypy`, only when the repository configures it at the step's root. It runs on the job's own `python3` and checks against the packages that interpreter has.
- `ruff format --check`, only when the step passes `format-check: "true"`.

Every check runs, and the step fails when one fails or when a tool is missing.

## What turns each check on

| Check | Turned on by |
| --- | --- |
| `ruff check` | always; the repository's ruff configuration, such as `ruff.toml`, `.ruff.toml` or a `[tool.ruff]` table in `pyproject.toml`, sets its rules |
| `mypy` | a `mypy.ini` or `.mypy.ini` at the step's root, a `[tool.mypy]` table in `pyproject.toml` or a `[mypy]` section in `setup.cfg`; its `files` option names what it checks, and without one it checks the root |
| `ruff format --check` | the input `format-check: "true"` on the step |

A repository that wants ruff format checked passes the input:

```yaml
    uses: CrankyAnt/SharedWorkflows/actions/python-language-step@v1
    with:
      format-check: "true"
```

A repository whose Python project lies in a directory below the repository root, with its own ruff and mypy configuration there, passes that directory as `root`, relative to the repository root. ruff and mypy then run from that directory, ruff checks only the files below it, and mypy runs when that directory configures it:

```yaml
    uses: CrankyAnt/SharedWorkflows/actions/python-language-step@v1
    with:
      root: backend
```

Without `root`, the step checks the repository root.

## How the pipeline runs it

A repository that runs the suite selection as the part `suite-selection` of the pipeline installs no step: it names `python` under `language-steps` of that part in `.github/ci-config.yml`. The suite selection selects the step when a changed file is a Python file or Python configuration, and gives one shard without entry files when only the step is selected. Shard 1 of the shared suite jobs runs it after the step that checks the recording and outside the suite recording, so its reads join no suite's path list. The parts `python-verify` and `python-test` of the pipeline run the step on each project they check.

Outside the pipeline, a job that already installs Python calls the step of [python-language-step.yml](python-language-step.yml) without its `if`: that condition reads the input `language-steps` of the shared suite jobs, which no other job has.

## Where it runs

The step runs on the job's runner, Linux or Windows, in a job that has Python installed. In the pipeline, that is shard 1 of the suite jobs or the job of `python-verify` or `python-test`, and `ci / verdict` reports its result. The step adds no check to the default-branch ruleset: its result is part of that job.

## When a repository adopted it earlier

A repository whose validation workflow from an earlier kit of the suite selection holds the step removes it with that workflow when it switches to the part `suite-selection`, as the [suite selection kit](../../suite-selection/consumer-kit/README.md#when-a-repository-adopted-it-earlier) states, and names `python` under `language-steps` of that part.

A repository that installed the step in a job of its own brings it onto the current template in one pull request. It compares the installed step with the template it came from, read at `v1`:

| Template | Install target |
| --- | --- |
| [python-language-step.yml](python-language-step.yml) | the step in the job that installs Python, without its `if` |

Every difference that the template makes is taken over, apart from the repository's own values, which carry over:

- the `with:` of the step, such as `format-check: "true"` or `root: backend`;
- the step's place in its job and its omitted `if`, as [How the pipeline runs it](#how-the-pipeline-runs-it) states;
- the repository's ruff and mypy configuration, which come from no template and stay as they are.

The result works when the check of the job that holds the step passes on that pull request.

## When `v1` moves

A move of `v1` changes the later runs of the step without a change in the repository. A new ruff version can report a finding that the repository already holds; fix it in the same pull request, or set the rule in the repository's ruff configuration. A failure that the repository cannot fix this way is a fault of the shared part: report it to the user.
