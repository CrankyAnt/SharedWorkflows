# Delivery

Every implementation Work Brief, spec, or delivery ticket records:

```markdown
## Delivery strategy

**Delivery path:** direct-main | dedicated-branch | none
**Publish boundary:** commit | commit-and-push | draft-pr | pr | merge | none
**Required validation:** <commands or policy>
**Reason:** <risk-based reason>
```

Use direct-main only for a recorded small, local, reversible change with no schema, data, dependency, security, authentication, public-API, or parallel-work risk, after relevant validation passes and when repository rules permit it. The builder commits to the default branch and the user or delegating agent checks the result afterwards. Use `none` on both lines only for work whose only output is tracker content, such as comments or new issues; its builder changes no repository, stops after the published result and its independent receipt by a fresh subagent, and leaves closing the request to the user or delegating agent. Under dedicated-branch, the builder owns a branch and works up to the recorded boundary. Before it decides a PR's state, a fresh subagent receives the work under the receive contract and ticks only demonstrated criteria. Under `pr` the builder opens a ready PR once every criterion is ticked and the user or delegating agent merges; under `merge` the builder merges after full receipt and the pre-merge gate. Missing evidence or an open decision keeps the PR draft.

Planning tickets inherit the invoking map's delivery strategy, or the repository strategy below when the map records no override. Apply an unambiguous direct-main exception autonomously; ask only when the applicable strategy genuinely cannot be determined.

Default to `merge`: the builder merges after independent receipt and the pre-merge gate. Choose `pr` only when the user or delegating agent must make the merge decision itself after complete evidence, and `draft-pr` for review before the evidence is complete. The boundary is chosen when the contract is written; a delegating agent follows it or changes the contract, never a prompt override. Builders never tick their own criteria and do not route findings. Missing acceptance evidence keeps the final PR draft.

## Repository strategy

**Delivery path:** dedicated-branch
**Publish boundary:** merge
**Required validation:** the commands and workflows under `## Testing instructions` in `AGENTS.md`
**Reason:** every change touches the shared surface or this repository's own CI, and CI on the pull request is the only gate; a merge to `main` reaches consumers only when `v1` moves.

- CI is the only gate. While building, run the entry files of the changed behaviour, as "Validation" in `docs/maintaining.md` states; no local copy of the suite selection runs before a push.
- Additions that a pull request's recording check reports are applied to the suite list `tests/suites.json` in the same pull request.
- Creating or moving `v1`, apart from the automatic move of `promote-v1.yml`, is a one-way action under `docs/agents/publication.md`.

## Gitleaks before a push

Before every `git push` that sends commits, Gitleaks must pass from the repository root. This covers a branch push, a forced push, a push of a ref other than `HEAD`, and a tag push. It does not cover operations on the provider, such as merging a pull request there or editing a file in its web interface, nor deleting a remote branch or tag.

First refresh the remote-tracking refs of the target remote. Then scan, for each pushed ref, the commits reachable from it that no remote-tracking ref of that remote contains; a push of several refs lists them all before `--not`:

```text
git fetch --prune <remote>
gitleaks git --log-opts="<ref> --not --remotes=<remote>" --redact --no-banner --no-color
```

Setup records the repository's Gitleaks options here; add them to the command: `--config .gitleaks.toml --baseline-path .gitleaks-baseline.json`. Agents use the locally installed Gitleaks. When Gitleaks is not installed, the push waits and the agent reports it. This gate applies to direct-main and dedicated-branch delivery; local commits may precede it.
