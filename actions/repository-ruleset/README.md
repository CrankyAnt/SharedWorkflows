# Repository ruleset

The ruleset template for the default branch of a consumer of Shared Workflows. Its subject is one repository. The template holds what every such repository gets; an agent inspects the repository, fills in its required checks, shows the result and, after the user's approval, creates or updates the ruleset through the GitHub API.

## The template

`ruleset-main-template.json` holds:

- `deletion` and `non_fast_forward` on the default branch;
- the required checks `hygiene / hygiene` and `validation / validation`, with GitHub Actions (integration `15368`) as their source: the check of the hygiene caller that every consumer installs, and the verdict's check of the validation caller that a consumer installs with the suite selection;
- a bypass for the repository admin role in mode `always`.

The bypass lets the owner, and every agent that acts with the owner's token, push to the default branch. A bypass actor can also merge a pull request whose required checks have not passed.

Dependabot and outside contributors have no bypass. The Dependabot auto-merge workflow merges only when an active ruleset rule requires a check, and GitHub refuses its merge while a required check is pending or failed, so a qualifying Dependabot pull request merges only after its required checks pass.

## Fill the template for a repository

1. Read the callers of shared workflows under `.github/workflows/` on the default branch. The hygiene caller gives the required check `hygiene / hygiene`. The validation caller gives several checks, of which only the verdict's check `validation / validation` is required. The auto-merge caller gives no required check. A required check is named `<caller job> / <reusable workflow job>`. Drop a template check whose caller is not installed.
2. A check of the repository's own workflows is required only when the user asks for it and the workflow runs on every pull request to the default branch. A workflow whose `paths` or `branches` filter can leave a pull request without that check never becomes required: the missing check would block that pull request.
3. Read every ruleset of the repository, disabled ones included: `gh api repos/<owner>/<repo>/rulesets` and then each ruleset by its id. `gh api repos/<owner>/<repo>/rules/branches/<branch>` shows active rules only.
4. Show the user the filled ruleset and, for an existing ruleset, each field that changes with its old and new value. Any other difference from the template is a deviation: name it with its reason in the preview, and after approval record it with its reason under `## Repository choices` in the repository's `docs/agents/ci.md`.
5. With the user's approval, create the ruleset with `gh api -X POST repos/<owner>/<repo>/rulesets --input <file>` or update it with `gh api -X PUT repos/<owner>/<repo>/rulesets/<id> --input <file>`. Read it back and compare every field with the preview.

## When to activate

Set `enforcement` to `active` only when the callers are on the default branch and each required check has reported on a pull request since. Until then, create the ruleset with `enforcement` set to `disabled`, in place of the template's `active`; that difference is no deviation to record under step 4. An active ruleset that requires a check that never reports blocks every pull request; on the pull request that installs the callers, a failing hygiene check would block its own merge.
