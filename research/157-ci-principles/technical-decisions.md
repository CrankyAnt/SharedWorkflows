# Technical decisions for the repair of the shared CI (SharedWorkflows#157)

Each open point from the round-3 comparison (`round-3-code-review.md`, sections 3, 4 and 7) is settled against the goal and principles in `docs/agents/ci.md` (at `v1` = `5bb8fe1`) and ADR 0006. The repair adapts the existing system; it is not a new form of CI.

## Approach

- **T1. The recorded lists stay in the tracked suite list for this repair (A1).** Complete runs replace a unit's lists (Sound selection), new suites and entry files get keys (Reuse), and the measurement counts runs that fail on additions alone (Measurement and growth). No run since `v1` moved to `2b81d7c` failed on additions alone, so moving the lists out of the tree is not shown to be needed; the measurement settles it later.
- **T2. One planning job decides every check of a run (A2, A5, K05).** It holds the push lookup, the shared lookup, suite selection and the hygiene change detection, with one checkout (Selection, Jobs). The light hygiene checks run inside that job; a check moves to its own job only where a measured duration would hold back the goal (Jobs).
- **T3. Tooling inputs are made visible through digests, not by pinning a commit in the fixed callers (A3, K07, K08, K23, K24).** Each platform and each tool or language step gets its own digest, without the selection code (Running everything). Pinning would change how every consumer receives shared CI, which this repair does not do.
- **T4. A structured default-branch record replaces the reading of logs (A4, K26, K27).** Passed default-branch runs store passed unit keys, the tested tree and the digests in a cache entry in the default branch's scope, which pull requests can restore and not write (ADR 0006, Reuse). The lookup reads that record, has no fixed walk limit, and counts every fallback by reason (Sound selection).

## Conflicts

- **T5. Windows is not sharded in this repair (X03).** A job is split only where its measured duration holds back the goal (Jobs); the Windows job is measured first.
- **T6. Which `stat` variants pull-request runs trace is settled by one trace of a representative test on the pinned runner image (X04).** The ticket records the question, the method and the result.
- **T7. Gitleaks scans the commits a run adds to a ref, never the whole history, and does not skip on an identical tree (X08, K51).** Its input is history, not the tree: a ref can gain commits without a changed tree (Selection). A full scan runs when the Gitleaks configuration, the baseline or the pinned version changes.
- **T8. The ETW consumer rebuild leaves the shared automation workflow (X11, K64).** It is a build artefact of Shared Workflows itself, which no consumer's product needs (Shared CI); it runs from one route in this repository.
- **T9. Shared Workflows' standalone Python tests caller is retired (X13, K67, K80).** Its tests run through the pipeline, so the Markdown and empty-diff questions of its own selection disappear (One verdict, Changing CI).
- **T10. The legacy suite workflows are retired (X15, K18).** A replacement removes the old part at once; where a consumer still calls one, the transition gets a named accountable party, a removal condition and an end date (Changing CI).
- **T11. Every default-branch change gets a verdict (K65, K66).** Auto-merge requires `ci / verdict` by name; a merge whose default-branch run did not start is detected and its run dispatched again (One verdict). No merge route through the App is added.
- **T12. The measurement runs as one scheduled workflow in the management repository over every consumer (K89).** It reports both targets per repository over four weeks of runs that reach a verdict, manual re-runs excluded, counts fallbacks, and opens an issue on a miss (Measurement and growth). The management repository already holds the access that reading every consumer needs.
- **T13. Shards are sized from durations measured in every run, and an entry without a measured duration gets its own shard until it has one (K28, K29, K47).** The bound leaves room for the fixed cost of the run within the goal (Jobs, Measurement and growth).
- **T14. An unreadable digest or record counts as changed (K22).** What selection cannot observe selects more (Sound selection).
- **T15. Caches keyed by pin and lock hash are used for tool and dependency installs (K11, K12).** Work several runs need with the same inputs is not repeated (goal); the effect is judged from the runs' own durations and the measurement (Changing CI).
- **T16. The full-recording close never proposes partial results (K48, K50).** A missing shard fails the close visibly and opens no pull request; a partly recorded suite keeps its lists and is reported, while complete suites are applied (One verdict, Sound selection).
- **T17. The Reuse principle's `behind` rule is reworded (R1):** "one tested on an older base runs what the change and the newer base can each affect, and takes over nothing the newer base can affect until that base has passed." The CI guide template and this repository's guide change on the integration branch; consumers receive it with the repair's `v1` promotion.
