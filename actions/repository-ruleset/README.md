# Repository ruleset

The ruleset template for the default branch of a consumer of Shared Workflows. Its subject is one repository. The template holds what every such repository gets; an agent inspects the repository, fills in its required checks, shows the result and, after the user's approval, creates or updates the ruleset through the GitHub API.

## The template

`ruleset-main-template.json` holds:

- `deletion` and `non_fast_forward` on the default branch;
- the required checks `hygiene / hygiene`, `validation / validation`, `tests / tests` and `ci / verdict`, with GitHub Actions (integration `15368`) as their source: the check of the hygiene caller that every consumer installs, the verdict's check of the validation caller that a consumer installs with the suite selection in its earlier form, which the pipeline's part `suite-selection` replaces, the check of the Python tests caller that a consumer installs with Python tests, and the verdict's check of the caller `ci.yml` that calls the pipeline;
- a bypass for the repository admin role in mode `always`.

The bypass lets the owner, and every agent that acts with the owner's token, push to the default branch. A bypass actor can also merge a pull request whose required checks have not passed.

Dependabot and outside contributors have no bypass. The Dependabot auto-merge workflow merges only when an active ruleset rule requires a check, and GitHub refuses its merge while a required check is pending or failed, so a qualifying Dependabot pull request merges only after its required checks pass.

## Fill the template for a repository

1. Read the callers of shared workflows under `.github/workflows/` on the default branch. With the fixed callers there, the repository has adopted the pipeline, and the filled ruleset requires one check, `ci / verdict`, the verdict of the caller `ci.yml`: drop the template's other three checks. The sync compares the ruleset of such a repository with the ruleset template holding that one required check, and reports a ruleset with another set of required checks as a settings difference. A repository that has not adopted the pipeline requires the check of each caller it still holds: the hygiene caller gives `hygiene / hygiene`; the validation caller gives several checks, of which only the verdict's check `validation / validation` is required; the Python tests caller gives `tests / tests`, and one check more for each further caller job of a repository with several test roots, such as `tests-parser / tests`. The auto-merge caller gives no required check. A required check is named `<caller job> / <reusable workflow job>`. Drop a template check whose caller is not installed.
2. A check of the repository's own workflows is required only when the user asks for it and the workflow runs on every pull request to the default branch. A workflow whose `paths` or `branches` filter can leave a pull request without that check never becomes required: the missing check would block that pull request.
3. Read every ruleset of the repository, disabled ones included: `gh api repos/<owner>/<repo>/rulesets` and then each ruleset by its id. `gh api repos/<owner>/<repo>/rules/branches/<branch>` shows active rules only.
4. Show the user the filled ruleset and, for an existing ruleset, each field that changes with its old and new value. Any other difference from the template is not kept: the sync reports a difference from the template on every run, and its local mode replaces it with the template's value.
5. With the user's approval, create the ruleset with `gh api -X POST repos/<owner>/<repo>/rulesets --input <file>` or update it with `gh api -X PUT repos/<owner>/<repo>/rulesets/<id> --input <file>`. Read it back and compare every field with the preview.

## When to activate

Set `enforcement` to `active` only when the callers are on the default branch and each required check has reported on a pull request since. Until then, create the ruleset with `enforcement` set to `disabled`, in place of the template's `active`; that difference is the one temporary difference from the template. An active ruleset that requires a check that never reports blocks every pull request; on the pull request that installs the callers, a failing hygiene check would block its own merge.
