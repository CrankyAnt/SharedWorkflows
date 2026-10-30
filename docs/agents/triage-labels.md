# Tracker Labels

Map these semantic roles to existing tracker labels where possible. The shown names are the defaults.

## Type — at most one

| Default label | Colour | Meaning |
| --- | --- | --- |
| `type:bug` | `d73a4a` | Existing behaviour is broken |
| `type:feature` | `1d76db` | A substantial new capability |
| `type:enhancement` | `a2eeef` | An improvement to existing behaviour |
| `type:maintenance` | `c5def5` | Internal upkeep without a user-facing capability |
| `type:question` | `d876e3` | A genuine question without convincing bug or change evidence |

Type may remain absent until the request is understood. A question that later proves to be a bug or change request replaces `type:question` with the correct type.

## Triage — temporary

| Default label | Colour | Meaning |
| --- | --- | --- |
| `triage:pending` | `fbca04` | Evaluation is in progress |
| `triage:needs-info` | `fef2c0` | Specific information is required from the reporter |

## Ready — at most one

| Default label | Colour | Meaning |
| --- | --- | --- |
| `ready:independent` | `0e8a16` | The contract is sufficient for independent execution |
| `ready:interactive` | `5319e7` | Execution requires live user judgement or access |

Both Ready labels require a complete contract (Work Brief, spec or ticket). `ready:interactive` describes an execution need, not an unresolved decision.

## Status

| Default label | Colour | Meaning |
| --- | --- | --- |
| `status:deferred` | `cfd3d7` | Valid work intentionally postponed for future consideration |

## Resolution — at most one on closure

| Default label | Colour | Meaning |
| --- | --- | --- |
| `resolution:not-planned` | `6e7781` | Deliberately outside current product scope |
| `resolution:duplicate` | `cfd3d7` | Already represented by another request |
| `resolution:invalid` | `e4e669` | The claim does not hold or is not actionable |
| `resolution:answered` | `0e8a16` | A genuine question was answered |

An unresolved `type:question` may carry `triage:needs-info`. If it remains a genuine question and is answered, close it with `resolution:answered`.

## Lifecycle sets

Treat labels as current semantic state, not an audit log; the tracker timeline preserves prior labels.

| Issue state | Required labels | Optional labels | Labels that must be absent |
| --- | --- | --- | --- |
| Triage | zero or one Type; exactly one Triage | none | Ready, Status, Resolution |
| Ready contract | exactly one Type; exactly one Ready | none | Triage, Status, Resolution |
| Active or pre-merge delivery | exactly one Type; exactly one Ready | none | Triage, Status, Resolution |
| Deferred | exactly one Type; `status:deferred` | none | Triage, Ready, Resolution |
| Closed other work as completed | exactly one Type | none | Triage, Ready, Status, Resolution |
| Closed without delivery | exactly one Type; exactly one Resolution | none | Triage, Ready, Status |

This vocabulary governs incoming-request triage and complete implementation contracts. A request that already carries Ready, or a label from another workflow's vocabulary, never receives a Triage label. Ready is applied only by `triage-requests`, `write-spec`, and `slice-into-tickets`. Other workflows have separate vocabularies in the tracker configuration.

Before publishing or closing an issue, reconcile it to the applicable row and stop on a conflicting label that cannot be resolved from evidence. Successful delivery uses the tracker's native `closed/completed` state; do not add a duplicate completed-resolution label.
