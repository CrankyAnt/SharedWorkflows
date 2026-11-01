# Python language step

The consumer kit of the Python language step, a shared part of the [catalogue](../../../catalogue.json). A repository with Python files adopts it by adding one step, [python-language-step.yml](python-language-step.yml), to a job of its validation workflow. The step calls the shared step `CrankyAnt/SharedWorkflows/actions/python-language-step@v1`.

## What the step runs

The step installs its pinned tools into the runner's temporary directory with the job's own Python, `python3`, or `python` on Windows. It installs no interpreter, so the job installs Python before the step and keeps that Python as the job's own. It then runs these checks over the repository:

- `ruff check` with the repository's ruff configuration, or ruff's default rules without one. It writes no cache into the checkout.
- `mypy`, only when the repository configures it at its root. It runs on the job's own `python3` and checks against the packages that interpreter has.
- `ruff format --check`, only when the step passes `format-check: "true"`.

Every check runs, and the step fails when one fails or when a tool is missing.

## What turns each check on

| Check | Turned on by |
| --- | --- |
| `ruff check` | always; the repository's ruff configuration, such as `ruff.toml`, `.ruff.toml` or a `[tool.ruff]` table in `pyproject.toml`, sets its rules |
| `mypy` | a `mypy.ini` or `.mypy.ini` at the repository root, a `[tool.mypy]` table in `pyproject.toml` or a `[mypy]` section in `setup.cfg`; its `files` option names what it checks, and without one it checks the root |
| `ruff format --check` | the input `format-check: "true"` on the step |

A repository that wants ruff format checked passes the input:

```yaml
    uses: CrankyAnt/SharedWorkflows/actions/python-language-step@v1
    with:
      format-check: "true"
```

## How a validation workflow calls it

Add the step of [python-language-step.yml](python-language-step.yml) to the shard job of the repository's validation workflow, after the step that checks the recording. The job installs Python before the step, and the step runs outside the suite recording, so its reads join no suite's path list.

The step runs under `if: ${{ !cancelled() && fromJSON(inputs.language-steps).python == 'run' }}`. The validation workflow takes the input `language-steps` (`type: string`, `required: false`), which the validation caller passes from the `language-steps` output of the shared `suite-selection.yml`; the caller names the step there with `language-steps: python`. That output holds `run` when a changed file is a Python file or Python configuration. In the shard job the condition also holds `matrix.shard.shard == 1`, so shard 1 alone runs the step; the suite selection gives one shard without entry files when only the step is selected.

A job outside the suite selection that already installs Python calls the step without the `if`.

## Where it runs

The step runs on the job's runner, Linux or Windows, in a job that has Python installed. With the suite selection, that is shard 1 of the validation workflow, where the verdict reports its result. The step adds no check to the default-branch ruleset: its result is part of that job.

## When `v1` moves

A move of `v1` changes the later runs of the step without a change in the repository. A new ruff version can report a finding that the repository already holds; fix it in the same pull request, or set the rule in the repository's ruff configuration. A failure that the repository cannot fix this way is a fault of the shared part: report it to the user.
