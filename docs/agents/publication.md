# Publication Policy

**Repository visibility:** public

**Tracked or shared surfaces:** the tracked files, the tag `v1`, and the issues, pull requests and comments of `CrankyAnt/SharedWorkflows`

**Local-only surface:** `.docs/`

## Publication authority

**Two-way actions:** autonomous

**One-way actions that require a user question:**

- Destructive or hard-to-reverse operations.
- Creating, moving or rolling back `v1`, the promotion by hand of `docs/maintaining.md` included; the automatic move of `promote-v1.yml` after a pins-only commit is not one.
- Changing repository settings, rulesets, the GitHub App, its installation, or the repository variable and secret it uses.

**Standalone PR merge:** squash merge

**Ticket to integration merge:** squash merge; the ticket's closing comment names the one landing commit

**Integration to default merge:** merge commit; it keeps the ticket commits' SHAs and adds one first-parent spec commit

Treat tracked files and tracker content as potentially public. Publish professional, evidence-based conclusions without private deliberation, personal judgments, or unnecessary person-sensitive context. State each decision and fact as what holds, not as an act of deciding, with its reason where a reader needs one. Attribute no decision, choice, agreement or approval to any person, role or group, say nothing of when one was made, and leave out anyone's word, wish, preference, request or approval wherever a reader could take it as a ground for one. Of equivalent routes, state only the one taken, as what holds and not as a choice, without the alternatives, any hint of them, or the deliberation. Show exact wording for approval when it could materially affect a person or organisation.

Local-only files may preserve fuller internal reasoning, but never secrets, credentials, or sensitive personal data.

## Superseding decisions

When an authorized new decision changes one recorded in a published spec, the workflow publishing the replacement record also adds one pointer from the original spec to that record. Preserve the original decision content. After publication, read back the replacement and pointer; later readers follow that chain. New work that changes no earlier decision needs its own record, not a spec edit.
