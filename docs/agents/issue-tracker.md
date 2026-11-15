# Issue Tracker: GitHub

Issues, specs, Work Briefs, maps, and tickets live in GitHub Issues. Repository visibility and the publication policy for tracked and shared surfaces are recorded in `docs/agents/publication.md`; delivery path, publish boundary, and validation policy are recorded in `docs/agents/delivery.md`.

A change that adds or removes a label set that the repository's own issue forms, workflows or tools assign, or an intake rule, adds or removes its entry under `## Repository vocabularies and intake rules` in the same change. An issue form assigns only labels that this file or `docs/agents/triage-labels.md` records.

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

This label belongs only to finding issues, which hold in-flow work found while a spec runs, as `route-work` defines. Such issues carry it from creation, are not triaged, and never carry a Type, Triage, Ready, Status or Resolution label; triage discovery skips them as another workflow's vocabulary. The skill that publishes a spec, tickets or a map for the work links it from the finding issue, closes the finding issue as completed and then removes `flow:finding`; until then the label stays.

| Default label | Colour | Meaning |
| --- | --- | --- |
| `flow:finding` | `d93f0b` | In-flow work found in the development flow while a spec runs, waiting for the skill its body names |

## Repository vocabularies and intake rules

### Requests for shared CI

A request is an issue with the label `shared-ci` in a consumer, as the [catalogue](../../catalogue.json) tells an agent there under `requests`. No agent writes a request into this repository, so triage here also collects the open `shared-ci` issues from the owner's other repositories. It does so with the owner's own login, because a consumer can be private and no credential of this repository reaches it. The search is the issue search of the GitHub integration, as [Tool routing](#tool-routing) says, for the open issues with the label `shared-ci` across the owner's repositories:

```text
is:issue is:open label:shared-ci user:CrankyAnt
```

Only when the integration is unavailable or lacks the issue search, the same query runs through `gh`, and triage reports the fallback:

```text
gh search issues --owner CrankyAnt --label shared-ci --state open
```

For each request that it takes up, triage writes a public issue in this repository with a Work Brief in general terms: it names no other repository and holds no name, path or content of a private one; the publication checks fail on a reference to a repository of the owner that is not public. Triage then comments on the request in the consumer, with the owner's own login: the comment links the public issue and states the part's final name, and for an input that an existing part lacks the final name of the setting. The consumer's temporary entry names the part, or `<part>.<setting>`, under `waits-for`, and the switch-over check finds only those names. The agent in the consumer that switches the part on closes the request.

Triage, which decides, declines a request in the consumer itself: it closes the request as not planned, with a comment that states the decline, again with the owner's own login. Only a request closed that way allows the consumer a permanent entry in its local workflow register.

The label `shared-ci` belongs to the consumers' trackers. No issue of this repository carries it.

## Map operations

- Use one `planning:map` issue with child decision tickets labelled by planning type.
- Use native sub-issues and issue dependencies. If the purpose-built integration lacks a required relationship operation, use `gh`; add body relationship fallbacks only when GitHub itself lacks the native relationship.
- Claim a planning ticket by assigning it to the driving developer before work.
- The frontier is the ordered set of open, unblocked, unassigned children.
- Resolve with a professional evidence-based comment, close the child, and append only a gist and link to the map's decision index.
