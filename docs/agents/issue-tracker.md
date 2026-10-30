# Issue Tracker: GitHub

Issues, specs, Work Briefs, maps, and tickets live in GitHub Issues. Repository visibility and the publication policy for tracked and shared surfaces are recorded in `docs/agents/publication.md`; delivery path, publish boundary, and validation policy are recorded in `docs/agents/delivery.md`.

## Tool routing

Use local Git for status, diff, branches, staging, commits, and pushes. Use the active purpose-built GitHub integration first for issues, comments, labels, sub-issues, dependencies, pull requests, and merges. Use `gh` only when the integration is unavailable or lacks the required operation, and report the fallback. Use the normal credential store; never copy tokens into prompts, environment variables, or plaintext configuration.

## Request surface

**External pull requests are a request surface:** no

When enabled, triage external contributor PRs through the same semantic labels as issues. Leave owner, member, and collaborator work out of discovery unless explicitly named.

## Semantic operations

- Publish a spec, Work Brief, ticket, map, or resolution as the corresponding GitHub issue or comment.
- Apply labels from `docs/agents/triage-labels.md` to ordinary requests and implementation contracts and from the vocabularies below to the issues each names; flow skills add no other label to any issue.

GitHub issue history is durable. Do not create a duplicate Markdown document for each GitHub issue.

## Planning vocabulary

These labels belong only to maps and their decision tickets. They never carry Type, Triage, Ready, Status, or Resolution labels. Ordinary specs, Work Briefs, and delivery tickets never carry this vocabulary. Preserve the planning label after closure; the assignee is the claim, not another label.

| Default label | Colour | Meaning |
| --- | --- | --- |
| `planning:map` | `0052cc` | Canonical map for a large uncertain effort |
| `planning:research` | `006b75` | Evidence-gathering decision ticket |
| `planning:prototype` | `b60205` | Prototype decision ticket |
| `planning:grilling` | `7057ff` | Interactive decision ticket |
| `planning:task` | `1d76db` | Concrete prerequisite for a map decision |

| Map lifecycle | Owned labels |
| --- | --- |
| Open map | `planning:map` only |
| Open decision ticket | exactly one non-map planning label |
| Closed map or decision ticket | retain the same planning label only |

## Research vocabulary

This label belongs only to research issues opened outside a `map-the-work` map, as `docs/agents/research.md` defines. Such issues are not triaged and never carry a Triage, Ready, Status or Resolution label; triage discovery skips them as another workflow's vocabulary.

| Default label | Colour | Meaning |
| --- | --- | --- |
| `research:standalone` | `0e7c86` | Research issue outside a map; conclusion in its closing comment |

## Finding vocabulary

This label belongs only to finding issues, which hold independent in-flow work found during delivery as `route-work` defines. Such issues carry it from creation, are not triaged, and never carry a Type, Triage, Ready, Status or Resolution label; triage discovery skips them as another workflow's vocabulary. The skill that publishes a spec, tickets or a map for the work links it from the finding issue, closes the finding issue as completed and then removes `flow:finding`; until then the label stays.

| Default label | Colour | Meaning |
| --- | --- | --- |
| `flow:finding` | `d93f0b` | Independent work found during delivery; waits for the skill its body names |

## Repository vocabularies and intake rules

None.

## Map operations

- Use one `planning:map` issue with child decision tickets labelled by planning type.
- Use native sub-issues and issue dependencies. If the purpose-built integration lacks a required relationship operation, use `gh`; add body relationship fallbacks only when GitHub itself lacks the native relationship.
- Claim a planning ticket by assigning it to the driving developer before work.
- The frontier is the ordered set of open, unblocked, unassigned children.
- Resolve with a professional evidence-based comment, close the child, and append only a gist and link to the map's decision index.
