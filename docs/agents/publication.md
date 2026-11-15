# Publication Policy

**Repository visibility:** public

**Tracked or shared surfaces:** the tracked files, the tag `v1`, and the issues, pull requests and comments of `CrankyAnt/SharedWorkflows`

**Local-only surface:** `.docs/`

## Publication authority

**Two-way actions:** autonomous

**One-way actions that require a user question:**

- Destructive or hard-to-reverse operations.
- Creating, moving or rolling back `v1` by hand, as `docs/maintaining.md` says: every promotion that changes the write class or the template class of the shared surface, every rollback, the first creation and every move that changes a check name. A promotion by hand that changes the template class also replaces the management repository's sync caller when it differs from the caller at the target, and starts the sync there; the answer to the question covers that write and that start.
- The automatic move of `promote-v1.yml` is not one: it moves `v1` only to a commit on `main` whose changes compared with `v1` stay in the read class, after the commit's CI on `main` passed. A merged Dependabot pull request moves `v1` only that way. One commit outside the read class moves the same way: a pins-only pull request that the updater App opened from `chore/update-ci-pins` and the repository owner merged.
- Changing the promotion class of a path in `actions/repository-hygiene/shared-surface.json` to the read class.
- Changing repository settings, rulesets, deploy keys, environments and their secrets, the GitHub App, its installation, or the repository variable and secret it uses, in this repository.

**Standalone PR merge:** squash merge

**Ticket to integration merge:** squash merge; the ticket's closing comment names the one landing commit

**Integration to default merge:** merge commit; it keeps the ticket commits' SHAs and adds one first-parent spec commit

Treat tracked files and tracker content as potentially public. Publish professional, evidence-based conclusions without private deliberation, personal judgments, or unnecessary person-sensitive context. State each decision and fact as what holds, not as an act of deciding, with its reason where a reader needs one. Attribute no decision, choice, agreement or approval to any person, role or group, say nothing of when one was made, and leave out anyone's word, wish, preference, request or approval wherever a reader could take it as a ground for one. Of equivalent routes, state only the one taken, as what holds and not as a choice, without the alternatives, any hint of them, or the deliberation. Show exact wording for approval when it could materially affect a person or organisation.

Local-only files may preserve fuller internal reasoning, but never secrets, credentials, or sensitive personal data.
