# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring the codebase.

**Layout:** single-context

## Before exploring, read these

- **`GLOSSARY.md`** at the repo root, or
- **`GLOSSARY-MAP.md`** at the repo root if it exists: it points at one `GLOSSARY.md` per context. Read each one relevant to the topic.
- **`docs/adr/`**: read ADRs that touch the area you're about to work in. In multi-context repos, also check `src/<context>/docs/adr/` for context-scoped decisions.
- **`SCOPE.md`** at the repo root: the product's edges. Read it when you write or recommend new work. A builder is bounded by its contract, and new work found during delivery is tested against the edges of the contract under delivery instead; triage applies this file to incidental findings.

If any of these files don't exist, **proceed silently**. Don't flag their absence; don't suggest creating them upfront. The `model-domain` skill (reached via `grill-and-document`) creates them lazily when terms or decisions actually get resolved, and `SCOPE.md` only through `write-spec` or `grill-and-document`.

## File structure

Single-context repo (most repos):

```
/
├── GLOSSARY.md
├── docs/adr/
│   ├── 0001-event-sourced-orders.md
│   └── 0002-postgres-for-write-model.md
└── src/
```

Multi-context repo (presence of `GLOSSARY-MAP.md` at the root):

```
/
├── GLOSSARY-MAP.md
├── docs/adr/                          ← system-wide decisions
└── src/
    ├── ordering/
    │   ├── GLOSSARY.md
    │   └── docs/adr/                  ← context-specific decisions
    └── billing/
        ├── GLOSSARY.md
        └── docs/adr/
```

## Use the glossary's vocabulary

When your output names a domain concept (in an issue title, a refactor proposal, a hypothesis, a test name), use the term as defined in `GLOSSARY.md`. Don't drift to synonyms the glossary explicitly avoids.

If the concept you need isn't in the glossary yet, that's a signal: either you're inventing language the project doesn't use (reconsider) or there's a real gap (note it for `model-domain`).

## Flag ADR conflicts

If your output contradicts an existing ADR, surface it explicitly rather than silently overriding:

> _Contradicts ADR-0007 (event-sourced orders), but worth reopening because…_

## Flag scope crossings

If your output would take the product past an edge in `SCOPE.md`, put it to the user as one question: a conscious extension, or not. Never cross an edge silently, and never reject work for it on your own.
