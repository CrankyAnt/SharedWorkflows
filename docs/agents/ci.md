# CI

## Principles

Every agent that adds or changes a check, a suite or a workflow holds to the [principles](../maintaining.md#principles) in `docs/maintaining.md`, which states them in full. In short:

- Every check has a purpose in the repository as it is now.
- A check runs only on changes that can change its result.
- A change to prose that no code reads starts only light checks.
- Each suite has one subject and every test a place in a suite.
- A job on another platform runs only where its result can differ there.
- What is meaningful for repositories in general belongs in the shared workflows.
- Documentation follows what is built.

## Shared callers

This repository calls its own shared workflows by local path, `uses: $/.github/workflows/<file>`, from its callers and its validation caller, as [Calling the workflows](../maintaining.md#calling-the-workflows) says:

| Caller | Check | Inputs |
| --- | --- | --- |
| `repository-hygiene-caller.yml` | `hygiene / hygiene` | `gitleaks-config: .gitleaks.toml`, `gitleaks-baseline: .gitleaks-baseline.json` |
| `dependabot-auto-merge-caller.yml` | `auto-merge / auto-merge`, `auto-merge / merge` | none |
| `validate.yml` | `validation / validation` | on `select`: `suites: tests/suites.json`, `language-steps: python`, `runner-labels: linux=ubuntu-26.04 windows=windows-2025`, `setup-versions: python=3.13` |

`docs/maintaining.md` describes what each caller runs:

- `repository-hygiene-caller.yml`: [Repository hygiene](../maintaining.md#repository-hygiene), with the floor and the [publication checks](../maintaining.md#publication-checks).
- `dependabot-auto-merge-caller.yml`: [Dependabot auto-merge](../maintaining.md#dependabot-auto-merge).
- `validate.yml`: [Job layout](../maintaining.md#job-layout) and [Suite selection](../maintaining.md#suite-selection).

The hygiene caller and the validation caller also run on the [`dependabot-merged` event](../maintaining.md#the-dependabot-merged-event).

The checks to require in a ruleset on the default branch are `hygiene / hygiene` and `validation / validation`; [Change a check name](../maintaining.md#change-a-check-name) changes them.

## Inputs

The hygiene caller may set `zizmor-persona` to `pedantic` or `auditor`, for a stricter zizmor persona than the default, and `link-exclude` to the globs, one per line, of Markdown files the repository does not maintain, such as vendored documentation, whose relative links the link check then leaves alone. A caller that sets either records the input with its value and its reason under [Repository choices](#repository-choices). No other input is the repository's own choice.

The other inputs of the hygiene caller follow from the repository: `gitleaks-config` and `gitleaks-baseline` name the files that the Gitleaks command in `docs/agents/delivery.md` passes with `--config` and `--baseline-path`, and `coverage-exclude` lists the path of each package file that is a test fixture, one per line.

## Dependabot

A new package file that Dependabot supports gets an `updates` entry in `.github/dependabot.yml`. When no entry for its ecosystem covers its directory through `directory` or `directories`, extend the ecosystem's entry or add one, with `schedule`, and `cooldown` and `groups` where present, copied from an existing entry, preferring the `github-actions` entry. A package file that is a test fixture, which nothing installs, gets no entry; its path goes into the hygiene caller's `coverage-exclude`.

## Suite selection

`validate.yml` is the validation caller and `full-recording.yml` its [full recording](../maintaining.md#full-recording). `tests/README.md` describes the suites and how they run, and `docs/maintaining.md` the shared workflows and shared steps themselves.

## Repository choices

None.
