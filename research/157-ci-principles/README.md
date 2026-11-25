# SharedWorkflows#157: public research record

**rounds-1-2-consolidated.md** holds the consolidated findings of the first two rounds of the review of SharedWorkflows#157: the status of 25 gap clusters (T1-T25) and 21 single findings, the contradictions C1-C16 resolved against the code, the corrections each reviewer made, the root causes, the recommendations with their support, the verdicts on the goal and principles in `docs/agents/ci.md`, the open questions and 27 candidate issues. Reviewers are named A, B and C. Single findings that carried the prefixes F, S and N in the working record now carry A, B and C, so an ID such as A1 or C1 can also name a recommendation or a contradiction; read each ID within its table.

**round-3-code-review.md** holds the comparison of the three reviewers' round-3 code reviews against `docs/agents/ci.md` at commit `5bb8fe1`. It holds the defect and inefficiency matrix (clusters K01-K91) with each reviewer's IDs and locations, the proposed fix per cluster, the approach findings, the conflicts checked against the code, a citation check, the deduplicated fix candidates in section 6, and the open questions in section 7.

**technical-decisions.md** holds the settled answers to the open points of round 3 (T1-T17), each checked against the goal and principles in `docs/agents/ci.md` and against ADR 0006. Each entry names the principle it serves and the finding or cluster it resolves. The repair adapts the existing system rather than replacing it.

**fact-check.md** holds the checks of the checkable open items in section 7 of the consolidated record: access to billed minutes, a comparable push-rerun rate, the additions loop on `main`, the measured instance of the `behind` lookup, the per-platform digest case, merge-queue availability, growth over time, single-sourced figures, a second configuration edit on 20 November, and minor counts. Each figure cites a run ID, commit or pull request. Minute figures describe CI runs. Item 2 records the access that was missing when it was checked.

## Conclusion

- Three review rounds established 25 gap clusters (18 established, 7 partly supported, none refuted or open), 21 single findings (3 refuted), 16 contradictions resolved against the code, and 27 candidate issues.
- The round-3 comparison checked the cited lines at `5bb8fe1` and grouped the findings into clusters K01-K91, with conflicts resolved against the code.
- Against the goal, one of six parts is reached (central checks are reused), four are missed and one is contested.
- Of the ten principles, eight are gaps, Purpose is open and Tests shows no established gap.
- On this evidence the CI guide now states a measurable goal and revised principles (SharedWorkflows#168, `v1` = `5bb8fe1`), and ADR 0006 records the trust rule for reuse; the verdicts above are measured against the earlier principles.
- The repair adapts the existing system: recorded lists stay in the tracked suite list (T1), and the fixed callers are not pinned to a commit (T3).
- The deduplicated fix candidates in `round-3-code-review.md` section 6, together with `technical-decisions.md`, are the input of the repair spec.
- `technical-decisions.md` settles the open points of `round-3-code-review.md` section 7; what remains open there is a measurement that the repair's own runs and its goal measurement provide.
