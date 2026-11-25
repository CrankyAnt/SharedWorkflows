# Round 3 of SharedWorkflows#157: comparison of the three code reviews

Sources: the round-3 review brief; the yardstick `## Goal` and `## Principles` in `docs/agents/ci.md:3-20` of SharedWorkflows at `5bb8fe1`; the three reviewers' round-3 reports. The comparison added no findings. Where this document says "code:", the comparison read the cited lines in the clone to check a citation or settle a disputed fact. Line numbers refer to `5bb8fe1`.

Reviewer IDs:

- reviewer A: its own IDs (P1, S1, R1, H1, AU1, D1, RL1, PV1, SY1, PT1, OW1). Its section 2.12 is cited as W1 (package entry) and W2 (collector entry), section 2.13 as M1. Reviewer A marks each finding (V) when it read the lines itself and (D) when a delegated reviewer did.
- reviewer B: B01 to B24.
- reviewer C: C plus section and item, so C1.1 is item 1 of reviewer C's section 2.1, and C12.2 is item 2 of section 2.12. A letter names a sub-item, for example C1.8c.

## 1. Defects and inefficiencies matrix

Clusters group findings by code path and failure, not by wording. A `conflict` entry names what differs. Section 4 resolves each conflict against the code.

### Pipeline, verdict and callers

| # | Cluster | Location | reviewer A | reviewer B | reviewer C | Agr. | Conflict |
| --- | --- | --- | --- | --- | --- | --- | --- |
| K01 | The verdict passes when a planned job was skipped or the plan output is missing | `plan.py:515-534`, `pipeline.yml:604-606` | P1 | B01 | C1.1, C1.9d (legacy `suite-verdict.yml`) | 3/3 | — |
| K02 | A superseded PR run still gets a verdict: red when its parts were cancelled, or stale when a part disappeared on the newer head | `ci.yml:1-20`, `pipeline.yml:9-60,144-146,674`, `plan.py:486-490` | — | B03 | C1.2 | 2/3 | Different scenarios, compatible (section 4, X01) |
| K03 | Results of a cancelled run are written to the PR cache and later reused | `pipeline.yml:621-670`, `reuse.py:642-664` | — | B03 | C2.4a | 2/3 | — |
| K04 | "Re-run failed jobs" keeps the first attempt's selection and reuse decisions | `pipeline.yml:571-577` | S12 | B03 | — | 2/3 | — |
| K05 | Three decisions in three jobs with three full clones; lookup and dispatch check run twice | `pipeline.yml:9-50,278-336,505-600` | P2 | B02 | C1.3 | 3/3 | Repair (reviewer A, reviewer B) or restructuring (reviewer C), see section 3 |
| K06 | A merge of an identical tree reruns every non-suite part; no push lookup in `plan.py` | `plan.py:420-459` | P3, D2, SY1 | B04 | C1.4 | 3/3 | — |
| K07 | A part with `paths` misses changes to its shared code and floating tool versions | `plan.py:420-434` | P4 | B04 | — | 2/3 | — |
| K08 | A part without `paths` runs on every change and is reported as "switched on", not as unknown inputs | `plan.py:427-428` | P5 | B04 | — | 2/3 | — |
| K09 | A `paths` pattern that matches no file switches a part off silently | plan check | P6 | — | — | 1/3 | — |
| K10 | No job has `timeout-minutes`; the recorder reads child output until EOF | `pipeline.yml`, `suite-jobs.yml`, `recording.py:533-540` | P7 | — | — | 1/3 | — |
| K11 | `python-test` and `python-verify` check out and install the same project in two jobs | `pipeline.yml:197-276` | P8 | B05 | C1.8a, C10.1 | 3/3 | — |
| K12 | Tool and package installs are uncached (hygiene tools, commitlint, ruff, mypy, NuGet, npm) | `pipeline.yml:190`, `install_tools.py`, `python_language_step.py` | P8 (low), H7 | — | C1.8b, C4.4b, C10.1 | 2/3 | Fix differs: cache (reviewer C) or bundle commitlint and measure first (reviewer A) |
| K13 | The verdict and hygiene summaries do not show per-check outcomes, reasons and failures | `plan.py:537-549` | S12, H8 | — | C1.5 | 2/3 | — |
| K14 | A dispatch with an empty SHA skips the dispatch check; a failed dispatch check still runs most hygiene steps | `pipeline.yml:30,280,459` | — | — | C1.6 | 1/3 | — |
| K15 | Consumer checks run on events where they cannot change the verdict; register check has no retry | `pipeline.yml:411-434`, `consumer_checks.py` | H6 | — | C1.7 | 2/3 | — |
| K16 | Runner bugs: corrupt TRX hidden by another report, `npm` reinstalled unconditionally, empty `test` script reflected, unbounded `custom` setups | `dotnet_test.py:46-55`, `node_test.py:118-151`, `pipeline.yml:67-126` | — | (B05 judges these checks appropriate) | C1.8c-e | 1/3 | reviewer B and reviewer C judge the same code differently (X02) |
| K17 | Suite jobs: empty shard still records, Windows upload fails after a setup failure, cache prefix missing | `suite-jobs.yml:119-206,295-351` | — | — | C1.9a-c | 1/3 | — |
| K18 | Legacy `suite-selection.yml` and `suite-verdict.yml`: older copies with no accountable party, removal condition or end date | `suite-selection.yml`, `suite-verdict.yml`, `shared-surface.json:33-34` | S12 | (keeps a "compatibility adapter", section 3) | C1.9d, C2.8, C8.1 | 2/3 | Retire (reviewer A, reviewer C) or keep as adapter (reviewer B) |
| K19 | Fixed callers hardcode `main`; a renamed default branch gets no CI | `ci.yml:4-9`, `consumer-kit/ci.yml:4-10` | — | B03 | — | 1/3 | — |

### Suite selection and reuse

| # | Cluster | Location | reviewer A | reviewer B | reviewer C | Agr. | Conflict |
| --- | --- | --- | --- | --- | --- | --- | --- |
| K20 | A `behind` merge narrows selection without proof that the landing base passed (suites and hygiene) | `reuse.py:216-217`, `suite_selection.py:1793-1796,1427-1440`, `hygiene.py:558-560` | S1 (last clause) | B06 | C2.2, C4.3b | 3/3 | — (reviewer C adds: `both_sides` drops `undetermined` when one side has it) |
| K21 | A `behind` merge compares the digest with the base's last run, not the PR's tested run: units tested under older tooling are skipped, or every suite reruns | `reuse.py:473-476`, `suite_selection.py:1811-1818` | S1 | — | — | 1/3 | — |
| K22 | An identical tree whose digest cannot be read falls back to the base digest | `reuse.py:221-233`, `suite_selection.py:1816` | S2 | — | — | 1/3 | — |
| K23 | One digest over all platforms, selection code, comments and ETW build output | `recording.py:2689-2713`, `suite_selection.py:1807-1837` | S3 | B07 | C3.1 | 3/3 | — |
| K24 | Tool pins, the language step and the runner image are not selection inputs | `suite_selection.py:1821-1835`, `pins.json`, `reuse.py:576-587` | S4, H8 | B07 | — | 2/3 | reviewer B adds the runner image under an unchanged label |
| K25a | Reuse after applied additions (`e3762cf`) works for a keyed unit with unchanged inputs | `suite_selection.py:1442-1530,2056-2123`, `reuse.py:642-663` | S5 | B09 | C2.1 (trace) | 3/3 | — |
| K25b | A unit without a stored list (new suite or entry) is never keyed, so its first pass is not reused after additions | `suite_selection.py:1461-1465,1516` | — | B09 | C2.1 | 2/3 | — |
| K25c | A unit reused in the additions run keeps its old key and is not re-keyed | `reuse.py:655` | S5 hole 2 | — | — | 1/3 | — |
| K25d | A new module-lookup name changes the key of every unit that looks up in that directory | `reuse.py:557`, `suite_selection.py:1497` | S5 hole 3 | — | — | 1/3 | — |
| K26 | The lookup scrapes checkout logs, walks at most 20 commits, and counts no fallback | `reuse.py:41-45,186-233,379-422` | S7 | B08 | C2.5, C2.6a | 3/3 | — (reviewer C adds: the walk stops at the first passed run without a digest step) |
| K27 | No default-branch result record; PRs restore only their own cache | `pipeline.yml:571-577,623,665-670` | S6 | B08 (brief) | C2.3 | 3/3 | — |
| K28 | `SHARD_BOUND = 120` s does not fit the 3-minute goal; Windows runs serially without a bound | `suite_selection.py:59-63,1341-1416` | S8 | (against Windows shards) | C2.7b | 2/3 | reviewer B: "Do not add Windows shards by default" (X03) |
| K29 | Entries without a stored duration are packed at the OS mean, or at 0 s when none exists | `suite_selection.py:1383-1397` | S9 | — | C2.7a | 2/3 | Fix differs (own shard plus durations from every run, or a non-zero default) |
| K30 | `Tree.matching` and related scans walk the whole tree per pattern | `reuse.py:543-557`, `unrecorded.py:369,491` | S12 | — | C2.7c | 2/3 | — |
| K31 | Selection repeats Git and derivation work: `git show` per read path and side, `tool_versions` twice, `unrecorded.parts` twice | `unrecorded.py:104-121`, `suite_selection.py:1821-1835,1983,2000,2018-2021` | — | B10 | C2.7c | 2/3 | Each names different repetitions in `run_select` |
| K32 | An estimates-only suite-list change is removed from the match even for units that read the file; pooled selection starts sibling entries | `suite_selection.py:1897-1939,1234-1250` | — | B10 | — | 1/3 | — |
| K33 | A removed `always` pattern selects nothing for the reads it covered | `unrecorded.py:155-187`, `recording.py:1885` | S10 | — | C2.6b | 2/3 | — |
| K34 | Files named by ungated steps select nothing | `suite_selection.py:1142` | S11 | — | — | 1/3 | — |
| K35 | Any `.gitignore` change runs every suite | `suite_selection.py:1150-1152` | S12 | — | — | 1/3 | — |
| K36 | `SETUP_TERM` misses `.pinned`; `changed_step_gate` ignores top-level `env`, `defaults`, `permissions` | `unrecorded.py:59`, `ci_definition.py:554-626` | — | — | C2.6c | 1/3 | — |
| K37 | The language step is skipped when a moved digest forces every suite | `suite_selection.py:1811-1841,1327-1330` | — | — | C2.6d | 1/3 | — |

### Recording

| # | Cluster | Location | reviewer A | reviewer B | reviewer C | Agr. | Conflict |
| --- | --- | --- | --- | --- | --- | --- | --- |
| K38 | A normal run can only add reads; only the full recording replaces | `recording.py:2079,2126-2155,2592-2664` | R1 | B12 | C3.4 | 3/3 | — |
| K39 | PR runs trace with `MID_SET`, which lacks `stat`, `lstat`, `readlink`, `readlinkat` | `recording.py:69-70` | R2 | — | C3.4 | 2/3 | Which gap matters (X04) |
| K40 | `skip_count` misses pytest's quiet summary (and dotnet, node, PowerShell), so lists are kept and no duration is stored | `recording.py:113,311-325` | R4 | B12 | C3.4 | 2/3 on pytest, 1/3 on other runners | reviewer C calls pytest recognised (X05) |
| K41 | Git revision arguments (`<sha>:<path>`) become existence reads | `recording.py:1454-1510,1593` | R3 | B12 | C3.5b, C12.1 | 3/3 | Count in consumer P2: 540 (reviewer A) or 277 (reviewer C) (X06) |
| K42 | Write opens are discarded, so writes outside the setup go unchecked and an `O_RDWR\|O_CREAT` read is dropped | `recording.py:86,884-890`, `Program.cs:166` | — | B11 | C3.3 | 2/3 | — |
| K43 | Python lookup report drops lines under thread contention; a worker thread's listing hides the main thread's `listdir` | `shared_lookup_report.py:46`, `recording.py:1203-1207,1574-1578` | — | — | C3.2 | 1/3 | — |
| K44 | Classification gaps and quadratic list operations in `apply` | `recording.py:1461-1510,1701-1788,2290-2340,1851-1899` | — | — | C3.5a,c,d,e | 1/3 | — |
| K45 | ETW consumer: unchecked event lengths, missing `STATUS_NOT_A_DIRECTORY`, PID reuse, share-mode misclassification | `Program.cs:86-237`, `recording.py:1056-1057,2008-2023` | — | (B11: stop and loss handling correct) | C3.6 | 1/3 | — |
| K46 | One unit's recording error fails the whole job and discards its passes | recording check | R5 | — | — | 1/3 | — |
| K47 | Fixed per-entry cost (one `recording.py exec` per entry, four `jq` calls) and a fixed two workers | `suite-jobs.yml:147-159`, `suite_selection.py:63` | R6, R7 | (notes two workers neutrally) | — | 1/3 | — |
| K48 | Full-recording close: a failed shard or suite blocks or silently thins the list update | `automation.yml:488-546`, `recording.py:2320-2326,2592-2664` | R9 | (moved-head path "appropriate") | C5.2 | 2/3 | `apply_full` raises (reviewer A) or skips silently (reviewer C) (X07) |
| K49 | The weekly schedule starts a full-recording runner on odd weeks only to compute week parity | `automation.yml:437-448` | R9 | B15 | C5.1c | 3/3 | — |
| K50 | Full-recording passes are not stored; deleted paths stay listed | full recording | R9, R10 | — | — | 1/3 | — |

### Repository hygiene

| # | Cluster | Location | reviewer A | reviewer B | reviewer C | Agr. | Conflict |
| --- | --- | --- | --- | --- | --- | --- | --- |
| K51 | Gitleaks scans every ref's full history on every run | `hygiene.py:98-106` | H1 | B14 | C4.1 | 3/3 | Whether an identical tree may skip the scan (X08) |
| K52 | `check-commits.cjs` starts processes per commit (100 s at about 258 commits) | `check-commits.cjs:9-23` | H2 | B14 | C4.2a | 3/3 | Processes per commit (X09) |
| K53 | Comment stripping hides `${{ }}` in a `run:` block, so actionlint and zizmor are skipped | `hygiene.py:243-261` | H3 | — | — | 1/3 | — |
| K54 | A title or body edit reruns code checks and can clear a failure (standalone), or is never checked (pipeline) | `ci.yml:4-9`, `repository-hygiene.yml:74-242`, `hygiene.py:603` | P9, H4 | B23 | C4.3a | 3/3 | — |
| K55 | Missing `!cancelled()` and a shared `install` step: one failure skips other checks; a failed `changes` step selects nothing | `pipeline.yml:323,356-378,457-504` | H5 | — | C4.4a | 2/3 | — |
| K56 | REUSE selection misses header edits in existing files | `hygiene.py:416-479` | H8 | B14 | — | 2/3 | — |
| K57 | A PR can add its own finding to the Gitleaks ignore file | Gitleaks config | H8 | — | — | 1/3 | — |
| K58 | Bugs in `link_check.py`, `dependabot_coverage.py`, `hygiene.py`, `update_pins.py`, `update_spec.py` | as cited by reviewer C | — | — | C4.5 | 1/3 | — |
| K59 | commitlint revert patterns; `check-references.cjs` per-commit `git log` and long retries | `commitlint.config.cjs:1`, `check-references.cjs:74-182` | — | — | C4.2b,c | 1/3 | — |

### Automation, Dependabot auto-merge, release, promotion, sync

| # | Cluster | Location | reviewer A | reviewer B | reviewer C | Agr. | Conflict |
| --- | --- | --- | --- | --- | --- | --- | --- |
| K60 | The automation `plan` job starts for every failed same-repo PR run, also without additions configured, and on every `issues: opened` | `automation.yml:15-16` | AU1 | B15 | C5.1a,d | 3/3 | — |
| K61 | Auto-merge listeners start on failed and cancelled runs | `automation.yml:16,43`, `dependabot-auto-merge.yml:34` | AU2 | — | C5.1b, C6.1 | 2/3 | — |
| K62 | Issue-title part: clones to read one file (reviewer A); overwrites a newer title (reviewer B); validates every component, logs to stdout, no retry (reviewer C) | `automation.yml`, `issue_title.py` | AU3 | B15 | C5.4 | 1/3 each | Three separate defects |
| K63 | Additions: an `apply` failure posts no comment; the dispatch route checks out the branch tip without the head check; split concurrency | `automation.yml:91-93,139,377` | — | (B15: head checks correct) | C5.3a,b | 1/3 | X10 |
| K64 | ETW rebuild routing is a failed-run condition duplicated in `automation.yml`, product-specific in shared code | `automation.yml:152-181`, `rebuild-etw-consumer.yml:16-65` | (OW2: move rebuild into automation) | B13 | C5.3c | 2/3 | Fixes contradict (X11) |
| K65 | The merge succeeds, the dispatch fails, and the commit on `main` gets no run | `auto_merge.py:443-482` | D2 | B16 | C6.2b | 3/3 | Fixes differ (section 2) |
| K66 | Auto-merge accepts any required check, not `ci / verdict`; only the first commit's signature is verified | `auto_merge.py:188-201,274-291` | D1 | B16 (check identity) | — | 2/3 (check identity), 1/3 (signature) | — |
| K67 | SharedWorkflows runs auto-merge twice (caller and automation) | `dependabot-auto-merge-caller.yml`, `automation-caller.yml` | OW2 | B16 | C6.1 | 3/3 | — |
| K68 | `auto_merge.py` hardcodes SharedWorkflows paths; `--disable-auto-merge` and version-prefix edge cases | `auto_merge.py:47-49,144,242-244,427-455` | — | — | C6.2a,c,d | 1/3 | — |
| K69 | The release caller starts on every tag, `v1` moves included, and clones fully before deciding | `consumer-kit/release.yml:4-8`, `release.yml:9-29` | RL1 | B17 | C7.2b | 3/3 | — |
| K70 | Release binds only the peeled commit: the verified tag object (reviewer B) or its tag name (reviewer A) is not rechecked | `release.py:191-217,301-328` | RL2 | B17 | — | 2/3 | Different specifics, both hold |
| K71 | Release publishes without checking that the tagged commit passed CI | `release.py:121-150,191-217,505-555` | — | — | C7.1 | 1/3 | — |
| K72 | Release: duplicate draft on retry, no `set -euo pipefail`, runner size, no retry (reviewer C); `previous_release` pages everything (reviewer B) | `release.py:272-288,349-364,515-520`, `release.yml:114-122` | — | B17 | C7.2a,c,d | 1/3 each | — |
| K73 | `v1` moves with no consumer plan or canary; the read class includes files that change consumer results or that no CI exercises | `shared-surface.json`, `promote_v1.py:89` | PV1 | — | C8.1 | 2/3 | — |
| K74 | `await` polls three workflows; a timeout or failure leaves `v1` behind with a green run | `promote_v1.py:89,251-280,905-909` | PV2 | B20 | C8.2 | 3/3 (polling), 1/3 (silent green) | 10 min (reviewer A) or 540 s (reviewer C) (X12) |
| K75 | Manual promotion does not recheck the verdict at the tag write | `promote_v1.py:583-625,759-786,842-860` | — | B20 | — | 1/3 | — |
| K76 | The `template` class covers whole consumer-kit folders | `shared-surface.json:44-61` | — | — | C8.2 | 1/3 | — |
| K77 | Sync reads at one tip and proposes on another; any `main` change forces a new sync head | `sync.py:279-297,432-465` | — | B21 | — | 1/3 | — |
| K78 | Sync: sequential per-file calls, PR lookup twice, `issues?state=all`, no retry, stale PR body, issues never close, incomplete `adoption.json` | `sync.py:108-123,294-298,460,478-497` | — | — | C9.1, C9.2 | 1/3 | — |
| K79 | A CI guide wording change becomes a sync PR and full part reruns in every consumer | sync, `plan.py` | SY1 | — | — | 1/3 | Depends on K06 and K08 |
| K80 | The ruleset check reads only the template-named ruleset, and the template requires more than `ci / verdict` | `sync.py:385-393`, `ruleset-main-template.json:19-24` | SY2 | — | C11.2a | 2/3 | — |

### Python tests, SharedWorkflows' own CI, measurement, consumer test code

| # | Cluster | Location | reviewer A | reviewer B | reviewer C | Agr. | Conflict |
| --- | --- | --- | --- | --- | --- | --- | --- |
| K81 | The standalone Python decision is a file-type rule: Markdown-only changes skip, everything else runs | `python_tests.py:89-99` | PT1, PT2 | B05 | C10.2a | 3/3 | Direction of the fix (X13) |
| K82 | Malformed mypy configuration crashes the language step; standalone workflow clones fully | `python_language_step.py:50-67`, `python-tests.yml:50-89` | — | — | C10.2b,c | 1/3 | — |
| K83 | SharedWorkflows requires three checks | `docs/agents/ci.md:48` | OW1 | B23 | C11.2a | 3/3 | — |
| K84 | Duplicate routes (hygiene, auto-merge, Python tests) run with no accountable party, removal condition or end date | the three callers, `repository-hygiene.yml` | OW2, PT3, H8 | B23 | C11.2a | 3/3 | — |
| K85 | Fixture parts start on any change under `actions/pipeline/` | `.github/ci-config.yml:3-58` | OW3 | — | C11.2b | 2/3 | — |
| K86 | `automation-caller.yml` listens to three workflows | `automation-caller.yml:5-7` | OW4 | — | C5.1a | 2/3 | — |
| K87 | `tests/suites.json` still lists the deleted `validate.yml` | `tests/suites.json:112,400,409` | — | — | C11.2c | 1/3 | — |
| K88 | Pin and specification updaters repeat their queries in the write job | `update-ci-pins.yml:33-70`, `update-publication-spec.yml:34-78` | — | B24 | (C4.5d,e: other bugs in the same updaters) | 1/3 | — |
| K89 | Nothing measures the goal or counts fallbacks | absent | M1 | B22 | C11.1 | 3/3 | Where the measurement runs (section 2) |
| K90 | consumer P2 `test_build_package_preserves_runs.py` (67.7 s): one `git show` per packaged blob, full package builds for 11 malformed-record cases | `build_package.py:107-162,302-318,359-394`, test `:211-244` | W1 | B18 | C12.1 | 3/3 | Process counts (X14) |
| K91 | consumer P2 `test_collector64_run.py` (41.1 s): the same eight launch simulations run in three or four tests | test `:398-490` | W2 | B19 | C12.2 | 3/3 | — (reviewer A adds `test_preflight` builds a repository per test, 1/3) |

## 2. Proposed fixes per cluster

"Agree" means the reviewers propose the same mechanism. "Differ" means compatible but different approaches. "Contradict" means one fix rules out another. Single-reviewer clusters with an uncontested fix are listed briefly at the end.

| # | reviewer A | reviewer B | reviewer C | Relation | Principle |
| --- | --- | --- | --- | --- | --- |
| K01 | Require `success` for every part with `run: true`; pass the unit count and require suite jobs to succeed when above zero | Require positive evidence per planned leaf, including skips, setup failures and timeouts; make cache persistence failure optional | Parse `job_plan` and fail when absent; require `success` for planned parts; reject unexpected runs; require suite jobs when shards were emitted | Agree; reviewer B also adds that a results-store failure should not turn the verdict red | One verdict |
| K02 | — | Move supersession to a PR-scoped workflow group in the fixed caller; attempt identity in plan and verdict | Workflow-level concurrency in both `ci.yml` files; drop per-part groups; verdict `if: !cancelled()` | Agree on a caller-level group; reviewer C gives the exact expression | Jobs |
| K03 | — | Verify the producing attempt was complete and not cancelled before restoring | Gate the results job on jobs `success` or `failure`; check cancelled in `store`; put the job in the PR group | Agree | Reuse |
| K04 | (noted only) | Require a complete fresh execution for a re-run to get a verdict | — | — | Reuse |
| K05 | Move lookup, shared lookup, select and `hygiene.py changes` into `plan`; hygiene checks as steps of `plan` when no tests; fold results into `verdict` | One read-only plan job; keep separate check jobs where measured parallelism pays | One planning job with one checkout; run lightweight hygiene in it when no tests are selected; mind the 1 MB output limit | Agree on one plan job; differ on whether hygiene checks run inside it (reviewer A, reviewer C) or stay separate (reviewer B) | Selection, Jobs |
| K06 | `plan.py plan --lookup`; on `identical` every part is `run: false` | Bridge part inputs to the same PR and base evidence as recorded checks | Feed the push lookup to `make_plan`; `identical` and attempt 1 sets parts off; `behind` only after the landing base passed | Agree | Reuse |
| K07 | Digest per part over the shared files it runs, compared like the shared digest; require exact versions | Derive project and execution input sets per part; broader execution until known | — | Agree in aim; reviewer A names a digest, reviewer B an input set | Sound selection |
| K08 | Reason "inputs unknown", listed as a departure | Report unknown inputs as a separate reason; keep a measured always-run choice | — | Agree | Shared CI, Sound selection |
| K11 | One job per project with language step and tests as steps under `!cancelled()` | One compatible environment per root, version and platform when it protects wall time | One job per runner, version and directory, verify and test independent | Agree | Jobs |
| K12 | Caching "must be measured"; bundle commitlint and use the runner's Node | — | Cache tool directories, venvs, NuGet, npm by pin and lock hash; per-tool install outcomes | Differ | Jobs, Goal |
| K13 | List failed entries and per-entry reasons | — | Structured outcome lines from every job, rendered in `verdict_markdown` | Agree | One verdict |
| K15 | Run when `.github/` changed or on schedule; retry | — | One `consumer_checks.py all`; skip on non-enforced events; retries; fix three logic bugs | Agree on skipping; reviewer C adds merging and bug fixes | Selection, Goal |
| K18 | Name an accountable party, removal condition and end date, or remove | Keep a compatibility adapter for the legacy suite workflow | Retire, or record accountable party and end date and fix `cancelled` guard; reclassify out of `read` | Contradict on keep or retire | Changing CI |
| K20 | Check a passed run exists for `landing_base`, else run `sides[0]` | Carry exact green-base status; run the PR side while pending; account for unverified base work in the verdict | Query a passed run on `landing` before returning `behind`, else `pull-request`; keep `undetermined` | Agree; reviewer B adds base-side accounting | Reuse |
| K21 | Lookup returns `tested_run` and `tested_digest` for `behind`; full PR side when it differs | — | — | — | Reuse |
| K23 | `shared_digest(platform)`; move the suite-list parser out so selection code leaves the digest | Separate per-platform recorder, per-execution-step and interpretation identities | Per-OS digests; exclude non-runtime files; split `suite-jobs.yml` by job; or hash stripped Python | Agree on per-platform split; differ in granularity | Sound selection |
| K24 | Print a digest per pinned tool and language step; select units that state the tool | Include pinned assets and resolved environment versions; keep work selected until identity is known | — | Agree | Sound selection |
| K25b | — | Emit execution context for unkeyed units without lookup; key them under complete additions; share the apply routine | Emit unstored units under a sentinel key that `apply_reuse` never looks up | Agree | Reuse |
| K25c | `select` outputs reused units with context; `applied_keys` covers them | — | — | — | Reuse |
| K25d | Key a unit on the names it looked up | — | — | — | Reuse |
| K26 | PR run uploads a small artifact (merge, tree, digests) read through the artifacts API; a `::notice` per fallback | Structured records tied to tree, scope, identity and a non-cancelled attempt; exact evidence first; count fallbacks | Continue the walk past runs without a digest step; paginate jobs; structured default-branch cache record; per-reason counters | Agree on structured records; differ on store (artifact, unspecified, cache) | Sound selection, Measurement |
| K27 | Store `suite-results-default-<run id>` on passed default-branch runs; read the union; keep digests there | A protected-base record for exact reuse | Results job on passed default-branch runs, attempt 1; read-only fallback for PRs | Agree | Reuse |
| K28 | Bound from the goal (about 100 s after K05, about 70 s before); shard Windows; cap shard count | Do not add Windows shards by default; remove work first | Lower to about 75 to 90 s; deduct setup and language step from shard 1 | Agree on lowering the Linux bound (reviewer A, reviewer C); contradict on Windows (reviewer A, reviewer B) | Goal, Jobs |
| K29 | Every run writes measured seconds; unknown entries get their own shard; longest first | — | Conservative non-zero default estimate | Differ | Jobs, Measurement |
| K30 | One index | — | Fast path for literal paths; index directory children | Agree | Measurement and growth |
| K31 | — | Reuse a revision snapshot; batch blob reads | Hoist `content_digest`; reuse the `now` parts | Agree in kind | Jobs |
| K33 | A change of `always` runs every unit once | — | Compare `previous.always - current.always` | Differ (all units or the affected) | Sound selection |
| K38 | Check emits `replace` per clean, complete entry; blocked until K39 | Structured executed and skipped inventory; per-unit completeness; replace on complete evidence | Evaluate `WIDE_SET` on PR runs; replace on clean passes | Agree on replacement from complete runs; differ on how completeness is proven | Sound selection |
| K39 | Trace `readlink` and `readlinkat` in every run | — | Evaluate full `WIDE_SET` on PR runs | Differ | Sound selection |
| K40 | Accept the bare pytest line; strip colour codes | Structured inventory instead of output parsing | Parsers or TAP/TRX reporters for node and dotnet | Differ (patch the parser, replace it, extend it) | Sound selection |
| K41 | Drop an absent probe whose name equals a process argument containing `:`, `..` or `://` | Typed Git operands as tree or object dependencies | Fall back to `**` for non-git `.git` readers and unknown `cat-file` IDs | Differ | Tests, Sound selection |
| K42 | — | Collect writes; classify against per-entry grants; report first, then block evidence | Isolate `HOME`/`XDG_*`/`APPDATA`; record writes; fail `check` on writes outside setup | Agree; reviewer C adds environment isolation | Tests |
| K48 | Skip and report the failing suite; store the close's passes | — | `nullglob`; fail the job when any shard failed or results are missing | Contradict: reviewer A continues with the rest, reviewer C aborts | One verdict, Sound selection |
| K49 | Compute parity in the job condition | Gate cadence while planning | Move the even-week check into `plan` | Agree | Goal |
| K51 | `merge-base..head` on PRs, `before..after` on pushes; full scan on config, baseline or pin change and weekly; skip on identical tree | Trusted history boundary; scan newly reachable commits; full scan on rules, baseline, tool or history change; "do not equate unchanged tree with unchanged history" | `merge-base..head` and `landing..head`; `--all` on config, baseline or pin change or unknown base; skip on identical and `edited` | Agree on ranges; contradict on identical-tree skip | Goal, Selection |
| K52 | One `git log`; commitlint's lint function in one process | Batch Git reads and lint in one Node process; reuse results by exact message, rule and tool | One `git log --no-merges`; `@commitlint/load` and `lint` in process; check `result.error` | Agree; reviewer B adds result reuse | Goal |
| K54 | `ci.yml` starts on `edited`; that run plans only title and body checks; `verdict` takes over the newest code results of the head, failures included; mind cancellation | Plan title edits separately with prior code outcome of the same tree; cannot clear a code failure | Run only title checks; record failed outcomes too; read the live title; guard `record` | Agree | Selection |
| K55 | Add the conditions; a failed `changes` step selects every check | — | Per-tool install outcomes; conservative fallback when lookup fails | Agree | Jobs, Sound selection |
| K56 | Select on header edits and `.license` files | Add changed license-bearing content and tool identity | — | Agree | Purpose |
| K60 | Each job reads configuration itself; listener only for runs with additions artifacts or Dependabot | One short read-only eligibility read; move conditions into the plan | Scope `workflow_run` to CI for additions; conclusion checks | Agree | Goal |
| K61 | (noted) | — | Add `conclusion == 'success'` | Agree | Goal |
| K64 | Move the ETW rebuild into the automation workflow | One trusted decision from an explicit ETW build-mismatch result; build once | Extract the product-specific ETW check out of shared `automation.yml` | Contradict (reviewer A moves it in, reviewer C moves it out) | Shared CI, Jobs |
| K65 | The scheduled run detects a default-branch head without a verdict | Record merge and delivery state; reconcile by landed SHA; or an App merge that yields a normal push | Retry the dispatch; clear remediation on permanent failure | Differ (detect later, reconcile, retry) | One verdict |
| K66 | Require the verdict by name; verify every commit | Verify the expected check identity and head | — | Agree on check identity | One verdict |
| K67 | Delete the caller once K54 handles `edited` | Retire duplicate routes with the gate migration | Gate on success; retire the caller | Agree | Changing CI |
| K69 | Filter tags in the caller; decide before the clone | Make the caller conditional on adoption through the sync | `ubuntu-slim`, sparse read of `ci-config.yml` before unshallowing | Differ in mechanism | Goal, Jobs |
| K70 | Compare tag name in the tag object with the pushed ref | Carry and recheck the outer tag object ID and commit | — | Differ (complementary) | Purpose |
| K73 | Promotion states per platform and part which digests move; a digest-changing move goes out through the sync PR; batch, at most one a day | — | Reclassify unexercised workflows; canary consumer check; fail on goal guardrail misses | Differ | Changing CI |
| K74 | Listen to the completed run | Consume the completed verdict instead of polling | Exit non-zero when `await` leaves `v1` behind | Agree on replacing polling (reviewer A, reviewer B); reviewer C fixes the exit code | Changing CI, One verdict |
| K80 | Check that the default branch requires exactly the verdict | — | Consolidate on `ci / verdict` | Agree | One verdict |
| K81 | No file-type rule; use the part's inputs | Reuse the planner's project inputs | Return `False` for an empty diff; scope to configured `paths` or retire the standalone workflow | Contradict on Markdown and empty diff (X13); agree on "use declared inputs" | Sound selection |
| K83/K84 | Require `ci / verdict` only; delete hygiene and auto-merge callers after K54; keep standalone Python tests as a contract test; move ETW rebuild into automation | One verdict; keep fixture coverage per entry point only on contract changes; accountable party, condition, end date for overlap | `ci / verdict` only; remove the three callers or record accountable party and end date | Agree | One verdict, Changing CI |
| K85 | (noted) | — | Narrow `paths` to each fixture's scripts | Agree | Selection |
| K89 | Weekly job in each consumer on the existing cron; reads four weeks, attempt 1; one issue on a miss | Per-run records, aggregated on a schedule outside the critical path; count planned-but-unstarted work and default-branch commits without a verdict | Weekly job in the management repository beside `sync.yml`; per-repository targets and fallback counts; issue on miss | Differ on location and data source | Measurement and growth |
| K90 | Call the validator directly for 10 of 11 cases; build once per module; `git cat-file --batch`; move installer logic to a small fixture repository | Direct validator tests with one end-to-end refusal; batch Git reads in `snapshot` and `verify` | `git cat-file --batch`; validate `previous_install_files` before `snapshot`; mock `snapshot` outside one or two focused tests | Agree on direct validator tests and batched reads; differ on mocking and fixture repository | Tests |
| K91 | Compute simulations once in `setUpClass` | Run the eight launches once; share immutable snapshots; keep the distinct runs | Run each fraction once; split classes into entry files if still slow | Agree; reviewer C adds splitting | Tests |

Single-reviewer fixes, uncontested: K09 warn on empty patterns (reviewer A). K10 `timeout-minutes` from the plan and a per-entry limit (reviewer A). K14 run the dispatch check unconditionally and gate hygiene on plan success (reviewer C). K16, K17, K36, K37, K43 to K45, K58, K59, K68, K71, K72, K78, K82, K87 as reviewer C states them. K19 enforce `main` in sync or route by the real default branch (reviewer B). K22 treat an unreadable digest as moved (reviewer A). K32 honour content reads of the suite list (reviewer B). K34 add files that steps name to that step's part (reviewer A). K35 narrow `.gitignore` handling to matched reads (reviewer A). K46 report per unit (reviewer A). K47 one driver per shard and `nproc` workers (reviewer A). K50 store close passes, turn deleted paths into existence reads (reviewer A). K53 never treat a line with `${{` as a comment (reviewer A). K57 is noted without a separate fix. K62 fetch the file (reviewer A); re-read before PATCH (reviewer B). K63 set `reason=failed` on apply failure, unify concurrency, check the PR head on dispatch (reviewer C). K75 recheck the verdict at the tag write (reviewer B). K76 narrow the template class (reviewer C). K77 read at one commit, compare the effective patch, protect the ref (reviewer B). K79 relies on K06 and K08 (reviewer A). K88 carry one verified candidate into the write job (reviewer B).

## 3. Approach findings

| Approach | Who calls it a design fault | Replacement | Who agrees | Who disagrees |
| --- | --- | --- | --- | --- |
| A1. Recorded read lists live in the tracked suite list, so a new read fails the run and costs a second run | reviewer A (section 3, "the one place where repair does not reach the goal"), reviewer C (approach 2) | Lists as run output in a default-branch record (reviewer A) or run outputs and default-branch cache (reviewer C); retire the additions loop and most of the full recording. reviewer A: not before K27 exists and measurement shows how often additions still fail runs. reviewer C also proposes static import-graph selection for small suites | reviewer A, reviewer C | reviewer B: "Existing scoped cache and tracked suite lists remain useful. Replacing the technology would not repair accountability, completeness or wrong invalidation boundaries by itself" |
| A2. Five serial stages (plan, then hygiene and selection, then results, then verdict) cannot meet 3 minutes or 1 minute | reviewer C (approach 1, structural); reviewer A (section 3: "a repair, but the precondition for S8") | One planning job (all three); light checks inside it when no tests run (reviewer A, reviewer C) | all three on consolidation | Disagreement is only whether this is a repair or a restructuring |
| A3. The moving `v1` tag delivers tooling with no changed file, so each tooling input needs its own out-of-tree comparison | reviewer A | Fixed callers pin a commit; the sync moves the pin, so every tooling change is a changed file (SY3) | none explicitly; reviewer B and reviewer C repair the digests instead (K23, K24) | — |
| A4. State reconstructed from logs, plus one monolithic digest | reviewer C (approach 3); reviewer B (approach 2: "the complete shared recording bundle is the wrong unit of identity") | A versioned structured record of tree, per-OS and per-tool digests and unit keys (reviewer C); a protected record of per-scope identity (reviewer B) | reviewer A proposes the same fixes as repairs (S3, S7) | — |
| A5. The plan is a part switchboard and the verdict aggregates whatever jobs started | reviewer B (approach 1) | Decide each check in one read-only operation; one normalised record for schedule, execution, report and reuse | reviewer A and reviewer C fix the same symptoms as repairs (P1, C1.1) | — |
| A6. Full-history Gitleaks and uncached installs grow with history | reviewer C (approach 4) | Range scans and caches | reviewer A, reviewer B treat the Gitleaks part as a repair (H1, B14); reviewer A doubts cache gains without measurement | — |
| A7. Protecting a future outcome by running a separate job or keeping an old caller | reviewer B (approach 3) | Keep separate jobs only for isolation, privilege, platform or measured time; retire duplicates during promotion | reviewer A OW2, reviewer C C11.2 (as repairs) | — |
| A8. Slow consumer entries: fix the test code before finer selection or more shards | reviewer B (approach 4) | Remove repeated work first | reviewer A (W1, W2) implicitly | reviewer C proposes splitting entry files if still above about 15 s (not a contradiction; it follows the fix) |

reviewer A also remarks on the yardstick itself: for a `behind` merge, a unit only the change affects has the same inputs in the merged and tested trees, so "everything the change can affect until the newer base has passed" reruns it without a risk reviewer A can name. reviewer A applied the clause as written. See section 7.

## 4. Singles and conflicts

### Conflicts, checked against the code

| # | Cluster | Claims | Code shows | Verdict |
| --- | --- | --- | --- | --- |
| X01 | K02 | reviewer B: a removed part leaves the old run able to finish and report. reviewer C: cancelled parts make the old verdict red; a slower older `plan` can cancel a newer run | `ci.yml` and `plan` have no concurrency group; part groups come from `plan.py:486-490` and start only after `plan`; `verdict` runs `if: always()` (`pipeline.yml:674`) and treats `cancelled` as failing (`plan.py:62`) | Both hold; they describe different outcomes of the same missing workflow-level group. reviewer C's out-of-order cancellation follows from GitHub's group semantics; no run exercised it |
| X02 | K16 | reviewer B: `dotnet_test.py` checks the executed count and `node_test.py` validates metadata; both appropriate. reviewer C: an unreadable TRX counts zero while another counts above zero; `test` may be empty; npm is reinstalled unconditionally | `executed_tests` prints and skips unreadable reports and sums the rest (`dotnet_test.py:45-55`); `read_project` requires only the key `test` (`node_test.py:97`); `npm install --global` runs whenever `packageManager` is set | Both hold: the checks reviewer B judges appropriate exist, and the gaps reviewer C names are in them |
| X03 | K28 | reviewer A: shard Windows like Linux. reviewer B: do not add Windows shards by default | Windows is one job, entries run one after another, estimate is the sum (`suite_selection.py:1412-1416`) | Not a code fact; a judgement. Settled by measured Windows job duration against the goal (section 7) |
| X04 | K39 | reviewer A: the gap is `readlink` and `readlinkat`, since stat calls arrive as `newfstatat` and `statx`. reviewer C: `MID_SET` omits `stat`, `lstat`, `readlink`, `readlinkat`, so PR runs cannot replace lists | `MID_SET` holds `newfstatat` and `statx`; `WIDE_SET` adds the four (`recording.py:69-70`) | Both statements of the list hold. Whether `stat` and `lstat` matter depends on the C library, which reviewer A says it did not test |
| X05 | K40 | reviewer A, reviewer B: pytest's `-q` line `34 passed in 1.23s` is not recognised. reviewer C: `skip_count` recognises unittest, pytest and TAP | `PYTEST_SUMMARY = ^=+ .* in [\d.]+s\b.*=+$` (`recording.py:113`) requires the `=` frame | reviewer A and reviewer B hold; reviewer C's statement is true only for the framed summary |
| X06 | K41 | reviewer A: consumer P2 holds 540 revision probes. reviewer C: 277 | In reviewer A's copy of the consumer P2 suite list, strings matching `^[0-9a-f]{7,64}:` occur 540 times, 270 unique | Counting basis differs (occurrences against roughly unique strings); no substantive disagreement |
| X07 | K48 | reviewer A: a suite without a list and with missing results makes `apply_full` raise and blocks the recording. reviewer C: a failed shard's suites are skipped silently, the job exits 0 and opens an auto-merging PR; without `nullglob` an empty glob crashes | `start_list` raises when a listless suite has unrecorded entries (`recording.py:2320-2326`); `apply_full` iterates only suites that appear in the documents, so a suite absent from every document is untouched and unreported; the close job runs on `!cancelled()` without checking `full-recording-jobs` and sets `globstar` without `nullglob` (`automation.yml:490,522`) | Both hold, for different scenarios (partially recorded listless suite, wholly missing shard). the statement about the moved-head path covers only that path, which also holds |
| X08 | K51 | reviewer A, reviewer C: skip the scan on an identical tree. reviewer B: "Do not equate unchanged tree with unchanged history" | `gitleaks git --log-opts=--all` scans every reachable ref (`hygiene.py:98-106`) | Not a code fact. The skip is safe if every commit of the merged branch was already scanned; reviewer B points out that ref movement can add history without changing the tree. Open decision (section 7) |
| X09 | K52 | reviewer A: one Node and three Git processes per commit. reviewer C: three subprocesses per commit (two Git, one Node) | Per commit: `git rev-list --parents`, `git log -1`, one Node commitlint (`check-commits.cjs:10,16-18`), plus one initial `git rev-list` for all commits | reviewer C holds; reviewer A overcounts by one Git process per commit. The measured 100 s is shared by all three |
| X10 | K63 | reviewer B: the open-PR and head checks prevent stale evidence. reviewer C: the dispatch route checks out the branch tip without those checks | The head-SHA and open-PR checks run only `if [ "$GITHUB_EVENT_NAME" = workflow_run ]` (`automation.yml:140-150`); the dispatch route uses `ref=$head_branch` and proposes on a separate branch | Both hold, for different event routes |
| X11 | K64 | reviewer A: move the rebuild into the automation workflow. reviewer C: take the ETW logic out of shared `automation.yml`. reviewer B: one decision from an ETW build-mismatch result | The rebuild condition is duplicated, marked "equal in rebuild-etw-consumer.yml and automation.yml" (`automation.yml:152-181`) | Duplication confirmed; the fixes contradict. Open decision |
| X12 | K74 | reviewer A: polls for up to ten minutes. reviewer C: deadline 540 s, then exits 0 | `CI_TIMEOUT = 600`, `REQUEST_TIMEOUT = 20`, deadline `timeout - 3 * request_timeout` = 540 s (`promote_v1.py:100-102,259`); `await` returns 0 with `move=false` | Both hold: 600 s budget, 540 s polling deadline. reviewer C's exit-code claim holds |
| X13 | K81 | reviewer A, reviewer B: skipping Markdown-only changes is unsound because a test can read Markdown. reviewer C: running on any non-Markdown file is over-broad, and an empty diff should not run | `decision` returns run for `None`, for an empty list, and for any non-`.md` file; skip only when all files end in `.md` (`python_tests.py:89-99`) | The code facts all hold. The fixes point in opposite directions on Markdown and on the empty diff. Sound selection ("selects more, never less") supports reviewer A and reviewer B on Markdown; the empty-diff case depends on whether an empty list can mean "unknown" |
| X14 | K90 | reviewer A: about 4,770 Git processes (about 97 per build, 96 per verify; 2,134 from the 11 malformed cases). reviewer C: about 4,048 `git show` (37 builds and 7 verifies at 92 files) plus about 200 others | Both derive the count from the same source; reviewer A's notes include `rev-parse` and `ls-tree` per build; the comparison did not recount every call | Estimates of the same mechanism, within about 12 %. Neither changes the fix. reviewer B gives no count |
| X15 | K18 | reviewer B keeps a compatibility adapter for the legacy suite workflow; reviewer A and reviewer C propose retiring it or dating it | `suite-selection.yml` has no `pinned-tools` input (no `pinned` in the file), and both legacy files are `read` class in `shared-surface.json:33-34` | Code supports reviewer C's factual claims; keep or retire is an open decision |

### Singles (1/3), with evidence and confidence

Code check column: "holds" means the comparison read the cited lines and they show what the reviewer says; "not checked" means the comparison did not read them.

| # | Reviewer | Claim | Evidence | Confidence | Code check |
| --- | --- | --- | --- | --- | --- |
| K09 | reviewer A P6 | Unmatched `paths` switches a part off silently | plan check | medium (D) | not checked |
| K10 | reviewer A P7 | No timeouts; a held pipe hangs a shard for 360 min | no `timeout-minutes` in `pipeline.yml`, `suite-jobs.yml`; `recording.py:533-540` | high (V) | holds (only `promote-v1.yml` and `sync.yml` set timeouts) |
| K14 | reviewer C C1.6 | Empty dispatch SHA skips the check; failed check still runs hygiene steps | `pipeline.yml:30,280,459` | high | holds |
| K16 | reviewer C C1.8c-e | Runner bugs | `dotnet_test.py:45-55`, `node_test.py:97,129-139` | high | holds (X02) |
| K17 | reviewer C C1.9a-c | Empty shard records; Windows upload fails after setup failure; cache prefix missing | `suite-jobs.yml:119-206,295-351` | high | not checked |
| K19 | reviewer B B03 | Callers hardcode `main` | `ci.yml:4-9` | high | holds |
| K21 | reviewer A S1 | `behind` compares with base digest | `reuse.py:476`, `suite_selection.py:1811-1818` | high (V) | holds: `tested_digest` returns a value only for `identical` |
| K22 | reviewer A S2 | Unreadable digest on identical tree falls back to base | `reuse.py:221-233`, `suite_selection.py:1812-1818` | high (V) | holds |
| K25c | reviewer A S5 hole 2 | Reused units are not re-keyed | `reuse.py:655` | medium (D) | holds: `record` copies `reused` keys; `applied` covers only `units` |
| K25d | reviewer A S5 hole 3 | New lookup name changes other units' keys | `reuse.py:557`; `unit_reads` uses `suite_list.lookups_in(directory)` | medium (D) | holds for the mechanism |
| K32 | reviewer B B10 | Estimates-only change hides a real content read | `suite_selection.py:1897-1939` | high | holds (the comment says "no suite read it") |
| K34 | reviewer A S11 | Ungated step files select nothing | `suite_selection.py:1142` | medium (D) | holds (`_, gated = ...`) |
| K35 | reviewer A S12 | `.gitignore` change runs every suite | `suite_selection.py:1148-1152` | (D) | holds |
| K36 | reviewer C C2.6c | `SETUP_TERM` misses `.pinned` | `unrecorded.py:59` | high | holds for the regular expression |
| K37 | reviewer C C2.6d | Language step skipped when digest forces every suite | `suite_selection.py:1327-1330,1811-1818` | high | holds for `identical` (changed is empty, no `started`) |
| K43 | reviewer C C3.2 | Lookup drops and listing credit to both PID and TID | `shared_lookup_report.py:46`, `recording.py:1203-1207` | high | holds (non-blocking acquire returns; listing credited to `report.stem` and `fields[1]`) |
| K44 | reviewer C C3.5 | Classification gaps, quadratic apply | many lines | high | not checked |
| K45 | reviewer C C3.6 | ETW consumer safety | `Program.cs` | high | not checked |
| K46 | reviewer A R5 | One recording error fails the job | — | medium (D) | not checked |
| K47 | reviewer A R6, R7 | Per-entry overhead; two workers | `suite-jobs.yml:147-159` | medium, high | holds (four `jq` calls, `-P 2`) |
| K50 | reviewer A R9, R10 | Close passes not stored; 286 deleted paths listed | — | (D), low weight | not checked |
| K53 | reviewer A H3 | `# ${{ }}` stripped as comment | `hygiene.py:243-261` | medium-high | holds for the function |
| K57 | reviewer A H8 | PR can add its own Gitleaks ignore entry | — | (D) | not checked |
| K58, K59 | reviewer C C4.5, C4.2b,c | Tool bugs | as cited | high | not checked |
| K62 | reviewer A AU3, reviewer B B15, reviewer C C5.4 | Three separate issue-title defects | `issue_title.py` | (D), high, high | not checked |
| K63 | reviewer C C5.3 | Additions apply failure silent, dispatch route unchecked | `automation.yml:139-150,377` | high | holds (X10; `reason=failed` requires `APPLIED = success`) |
| K68 | reviewer C C6.2 | Hardcoded SharedWorkflows paths | `auto_merge.py:47-49,242-244` | high | holds |
| K71 | reviewer C C7.1 | Release does not check CI | `release.py:191-217` | high | holds (no CI query in `release.py`) |
| K72 | reviewer C C7.2, reviewer B B17 | Draft collision, pipefail, paging | as cited | high, medium | `release.yml:21` full clone holds; rest not checked |
| K75 | reviewer B B20 | Manual promotion skips the verdict recheck | `promote_v1.py:842-860,759-786` | high | not checked beyond function locations |
| K76 | reviewer C C8.2 | Template class too broad | `shared-surface.json:44-61` | high | not checked |
| K77 | reviewer B B21 | Sync two-tip race; unrelated main change forces a new head | `sync.py:279-297` | high | holds (tree built on the current tip, compared with the branch tree) |
| K78 | reviewer C C9 | Sync scaling and housekeeping | `sync.py:294-295,460,484` | high | holds for the double lookup, `state=all` and force push without body update |
| K79 | reviewer A SY1 | Wording change triggers full reruns in consumers | — | (D) | depends on K06 and K08 |
| K82 | reviewer C C10.2b,c | Config parse crash | `python_language_step.py:50-67` | high | not checked |
| K87 | reviewer C C11.2c | `validate.yml` still listed | `tests/suites.json:112,400,409` | high | holds (file absent from `.github/workflows/`) |
| K88 | reviewer B B24 | Updaters repeat queries | `update-ci-pins.yml:33-70` | high | not checked |

## 5. Citation check

The comparison read the cited lines in the clone at `5bb8fe1`. "Holds" means file, line and content agree; "off" means the content exists at another line.

### reviewer A (32 checked)

| Citation | Result |
| --- | --- |
| `plan.py:526-534` verdict fails only on failure or cancelled | holds |
| `pipeline.yml:606` suite jobs gated on shard output | holds |
| `pipeline.yml:26, 303, 534` full clones | holds |
| `pipeline.yml:322, 552` two lookups | holds |
| `plan.py:427-428` "switched on" | holds |
| no `timeout-minutes` in `pipeline.yml`, `suite-jobs.yml` | holds |
| `recording.py:533-540` reads until EOF | holds |
| `ci.yml:4-9` no `edited` | holds |
| `reuse.py:473-476` tested digest only for identical | holds |
| `suite_selection.py:1811-1818` digest comparison | holds |
| `suite_selection.py:1427-1439` `both_sides` | holds |
| `reuse.py:221-233` `run_digest` gives None | holds |
| `recording.py:2691-2713` shared files and digest | holds |
| `recording.py:65-66` imports `suite_selection` | holds |
| `reuse.py:655` store copies reused keys | holds |
| `reuse.py:557` lookups per directory | holds |
| `suite_selection.py:59, 63, 1350` bound, workers, oversize shard | holds |
| `suite_selection.py:1385-1396` mean estimate | holds |
| `suite_selection.py:1412-1416` Windows serial | holds |
| `recording.py:69, 70` `MID_SET`, `WIDE_SET` | holds |
| `recording.py:113, 311-325` pytest pattern, `skip_count` | holds |
| `hygiene.py:100` `--log-opts=--all` | holds |
| `check-commits.cjs:9-23` per-commit loop | holds; "three Git processes per commit" is off by one (X09) |
| `hygiene.py:243-261` `without_comment` | holds |
| `automation.yml:15-16` plan condition | holds |
| `suite-jobs.yml:147-153, 159` `jq` and `xargs -P 2` | holds |
| `reuse.py:41-45` log patterns, walk bound | holds |
| `suite_selection.py:1142` ungated discarded | holds |
| `suite_selection.py:1150-1152` `.gitignore` | holds |
| `suite_selection.py:1757-1759` no `--results` with `--lookup` | holds |
| `pipeline.yml:571-577, 665-670` restore and save on PR only | holds |
| `docs/agents/ci.md:44` three required checks | off: the three checks are at `:48`; `:44` describes the second hygiene run |

### reviewer B (30 checked)

| Citation | Result |
| --- | --- |
| `plan.py:515-566` `job_plan` feeds only the summary | holds |
| `pipeline.yml:604-619` matrix guard | holds |
| `pipeline.yml:621-688` results job `!cancelled()` | holds |
| `plan.py:486-490` concurrency per part and PR | holds |
| `pipeline.yml:135-146, 199-210` job index in group | holds (144-146, 208-210) |
| `pipeline.yml:571-577` attempt-gated restore | holds |
| `reuse.py:590-601` record shape validation only | holds |
| `ci.yml:4-9` hardcoded `main` | holds |
| `plan.py:420-434` "switched on" | holds |
| `python_tests.py:101-137` Markdown rule | off: the rule is in `decision` at `:89-99`; `:101-130` is `decide`, which calls it |
| `pipeline.yml:197-276` two Python jobs | holds |
| `reuse.py:211-218` `behind` by ancestry | holds |
| `suite_selection.py:1787-1818` intersection | holds |
| `reuse.py:576-587` context uses runner label | holds |
| `reuse.py:186-203, 221-233` log text | holds |
| `reuse.py:379-418` per-commit query, bound 20 | holds |
| `suite_selection.py:1461-1465` unkeyed units skipped | holds |
| `reuse.py:642-663` store | holds |
| `suite_selection.py:1507-1530` `applied_keys` | holds |
| `suite_selection.py:1897-1939` estimates-only | holds |
| `suite_selection.py:1821-1835, 2018-2021` `tool_versions` twice | holds |
| `recording.py:86, 771-888` write flags, `parse_trace` | holds |
| `recording.py:113, 311-324` pytest | holds |
| `hygiene.py:98-106` Gitleaks | holds |
| `check-commits.cjs:1-24` | holds |
| `automation.yml:15-44, 423-452` plan, parity | holds |
| `auto_merge.py:443-468, 471-482` merge, dispatch, open PRs only | holds |
| `release.py:191-217, 301-328` peeled commit, tag check | holds |
| `consumer-kit/release.yml:4-8` every tag | holds |
| `sync.py:279-297, 432-465` propose, read | holds |

### reviewer C (32 checked)

| Citation | Result |
| --- | --- |
| `plan.py:515-534` | holds |
| `pipeline.yml:674` `if: always()` | holds |
| `plan.py:62` `FAILING_RESULTS` | holds |
| `pipeline.yml:26, 303, 534, 322, 552, 336, 600` | holds |
| `pipeline.yml:30` dispatch condition on payload SHA | holds |
| `pipeline.yml:280, 459` hygiene `!cancelled()`, install gate | holds |
| `pipeline.yml:190` `package-manager-cache: false` | holds |
| `pipeline.yml:623` results gate | holds |
| `reuse.py:410-414` walk stops without digest step | holds |
| `reuse.py:186` `per_page=1` | holds |
| `suite_selection.py:1461, 1464, 1516` | holds |
| `reuse.py:216-217` `behind` | holds |
| `suite_selection.py:1438` `undetermined` in `both_sides` | holds |
| `suite_selection.py:59-63, 1383-1397` | holds; "packs all unmeasured tests into 1 shard" is true only when the OS has no stored estimate (mean 0) |
| `reuse.py:543-546` `Tree.matching` | holds |
| `recording.py:68-71` `MID_SET` | holds |
| `recording.py:311-324` `skip_count` | holds; pytest recognition is limited to the framed summary (X05) |
| `recording.py:884-890` write opens skipped | holds |
| `recording.py:1203-1207`, `shared_lookup_report.py:46` | holds |
| `hygiene.py:98-105, 558-560` | holds |
| `check-commits.cjs:9-19` three subprocesses per commit | holds |
| `automation.yml:16, 43` no conclusion check | holds |
| `automation.yml:490, 522` close condition, no `nullglob` | holds |
| `automation.yml:377` reason condition | holds |
| `auto_merge.py:47-49, 242-244, 454, 464-467` | holds |
| `release.py:191-217` no CI check; `release.yml` `fetch-depth: 0` | holds (line 21) |
| `promote_v1.py:259, 905-909` | holds |
| `shared-surface.json:33-34`, `promote_v1.py:89` | holds |
| `sync.py:294-295, 460, 484` | holds |
| `python_tests.py:93-94` empty diff runs | holds |
| `tests/suites.json:112, 400` `validate.yml` | holds |
| `suite-selection.yml` lacks `pinned-tools` | holds |

All three reports cite accurately. Two citations point at the wrong lines (reviewer A `ci.md:44`, reviewer B `python_tests.py:101-137`), with the content present nearby. One count is off (reviewer A X09). Two reviewer C wordings overstate what the cited code does (single shard, pytest).

## 6. Fix candidates, deduplicated

Order follows the reviewers' own rankings: each candidate's position is the consensus of where the reviewers placed it (trust first, then time on the critical path, then avoidable work). "Disagree" marks items where the reviewers' solutions differ or contradict. Effect estimates are the reviewers' estimates, not measurements.

1. **Make the verdict require every planned check (K01).** The verdict passes when a planned part or the suite jobs were skipped, or the plan output is missing (`plan.py:526-534`). Solution: compare each job's result with the plan and the selected unit count; fail on a missing plan; reject unplanned runs. Principle: One verdict. Support: reviewer A, reviewer B, reviewer C (all rank it first). Effect: about 20 lines (reviewer C estimate).
2. **Require a passed landing base before narrowing a `behind` merge (K20, with K21 and K22).** Suites and hygiene intersect both sides on ancestry alone. reviewer A adds that `behind` uses the base digest instead of the PR's tested digest, and that an unreadable digest on an identical tree falls back to the base. Solution: check for a passed run on the landing base, else run the PR side; carry the tested run and digest for `behind`; treat an unreadable digest as moved. Principle: Reuse. Support: reviewer A, reviewer B, reviewer C (K21, K22 reviewer A only). Disagree: reviewer B also proposes unverified base work accounted for in the verdict. Effect: one API call per `behind` push (reviewer C estimate).
3. **Give the fixed caller one PR-scoped concurrency group and never reuse cancelled results (K02, K03, K04).** Superseded runs get a red or stale verdict, cancelled results enter the cache, and a partial re-run keeps old reuse decisions. Solution: workflow-level group in both `ci.yml` files, `verdict` on `!cancelled()`, results job skipped on cancelled or skipped jobs, attempt identity for re-runs. Principle: Jobs, Reuse. Support: reviewer B, reviewer C; reviewer A (K04 only). Coordinate with candidate 12 so an `edited` run cannot cancel code checks (all three note this).
4. **Make tooling and part inputs visible to selection (K07, K08, K23, K24).** One digest covers all platforms and selection code; pins, the language step, runner images and shared part code select nothing; parts without `paths` run unreported. Solution: per-platform digests without selection code; per-tool and per-language-step digests; per-part input digests; report unknown inputs. Principle: Sound selection, Shared CI. Support: reviewer A, reviewer B, reviewer C (K23 all; K07, K08, K24 reviewer A and reviewer B). Disagree: reviewer A also proposes pinning a commit in the fixed callers and moving it through the sync (A3), which covers the whole class; reviewer B and reviewer C repair the digests.
5. **Decide the whole plan in one job and take over passes for all parts (K05, K06).** Three jobs decide with three full clones and duplicate lookups, and an identical merge reruns every non-suite part. Solution: lookup, shared lookup, `select` and `hygiene.py changes` in `plan`; `plan.py` reads the lookup and turns parts off on `identical`. Principle: Selection, Reuse, Jobs. Support: reviewer A, reviewer B, reviewer C. Disagree: whether hygiene checks run inside `plan` (reviewer A, reviewer C) or stay separate (reviewer B); reviewer C calls it a restructuring. Effect: 15 to 40 s and one or two runner starts per run (reviewer C estimate); about 40 s before the first test today (reviewer A).
6. **Scan only new history with Gitleaks (K51).** `--log-opts=--all` grows with history (18 to 37 s, 28 s median in consumer P2). Solution: scan `merge-base..head` and the pushed range; full scan on configuration, baseline or pin change. Principle: Goal, Selection. Support: reviewer A, reviewer B, reviewer C. Disagree: skip on an identical tree (reviewer A, reviewer C) or not (reviewer B); weekly full scan (reviewer A) or only on change. Effect: 15 to 35 s per consumer P2 run (reviewer C estimate).
7. **Lint all commit messages in one process (K52, K59).** Three processes per commit, 100 s at 258 commits. Solution: one `git log`, in-process commitlint, check spawn errors. Principle: Goal. Support: reviewer A, reviewer B, reviewer C; reviewer B adds reuse by message. Effect: 100 s to 1 to 3 s (reviewer C estimate).
8. **Set job and entry timeouts (K10).** The default is 360 minutes. Solution: `timeout-minutes` from the plan, a per-entry limit in `run_exec`. Principle: One verdict, Goal. Support: reviewer A.
9. **Fit shards to the goal (K28, K29, K47).** The 120 s bound leaves too little after about 90 s of fixed chain; unmeasured entries count at the mean (a 152 s entry planned as 5.9 s). Solution: lower the bound (reviewer A about 70 to 100 s, reviewer C 75 to 90 s), record measured durations every run, sort longest first; `nproc` workers (reviewer A). Principle: Goal, Jobs, Measurement. Support: reviewer A, reviewer C. Disagree: unknown entries get their own shard (reviewer A) or a non-zero default (reviewer C); Windows sharding (reviewer A for, reviewer B against).
10. **Replace log scraping with a structured default-branch record and count fallbacks (K26, K27).** The lookup reads checkout logs, stops after 20 commits or at the first run without a digest step, and counts nothing; no default-branch record exists. Solution: store passed keys and digests on passed default-branch runs; read the union on PRs; emit a counted notice per fallback. Principle: Reuse, Sound selection, Measurement. Support: reviewer A, reviewer B, reviewer C. Disagree: artifact (reviewer A) or cache (reviewer C) as the store.
11. **Check where tests write (K42).** Write opens are discarded, so a test that writes outside its setup still yields reads, and an `O_RDWR|O_CREAT` read is lost. Solution: record writes, judge them against setup grants, report first and then block; isolate home directories (reviewer C). Principle: Tests. Support: reviewer B, reviewer C.
12. **Run only title and body checks on an edit and carry code results forward (K54).** The standalone workflow reruns code checks and can clear a failure; the pipeline never checks an edit. Solution: `ci.yml` on `edited`; that run takes over code results of the same head, failures included. Principle: Selection. Support: reviewer A, reviewer B, reviewer C.
13. **Guarantee a default-branch verdict after an automatic merge (K65, K66).** A failed dispatch after a merge leaves `main` without a run; auto-merge accepts any required check. Solution: require `ci / verdict` by name; then detect (reviewer A), reconcile (reviewer B) or retry (reviewer C) the missing run. Principle: One verdict. Support: reviewer A, reviewer B, reviewer C. Disagree: on the recovery mechanism.
14. **Give SharedWorkflows one verdict and retire its duplicate routes (K67, K80, K83 to K86).** Three required checks, duplicate hygiene, auto-merge and Python routes with no end date, broad fixture paths. Solution: require only `ci / verdict`; remove the callers after candidate 12; narrow fixture `paths`. Principle: One verdict, Changing CI. Support: reviewer A, reviewer B, reviewer C. Effect: 2 to 6 jobs per PR, 30 to 90 s on doc edits in `actions/pipeline/` (reviewer C estimate).
15. **Let complete normal runs replace lists, and stop needless additions (K38 to K41).** Only the full recording replaces; pytest's quiet summary is missed; links are not traced on PR runs; Git revision arguments become reads. Solution: replacement from runs proven complete; fix or replace the summary parser; trace links; drop or type revision operands. Principle: Sound selection, Tests. Support: reviewer A, reviewer B, reviewer C. Disagree: how completeness is proven (parser fix, structured inventory, `WIDE_SET`) and how revision operands are handled.
16. **Key first-time and reused passes after additions (K25b to K25d).** New suites and entries, and units reused in the additions run, are not re-keyed. Solution: emit unkeyed units with their context under a key that is never looked up; re-key reused units; key lookups on names used. Principle: Reuse. Support: reviewer B, reviewer C (K25b); reviewer A (K25c, K25d).
17. **Measure the goal (K89).** Nothing measures the targets or counts fallbacks. Solution: a scheduled job over four weeks of first attempts that reports both targets and fallback counts and opens an issue on a miss. Principle: Measurement and growth. Support: reviewer A, reviewer B, reviewer C. Disagree: per consumer (reviewer A), management repository (reviewer C), per-run records then aggregation (reviewer B). Effect: about 200 lines and a weekly small job (reviewer C estimate).
18. **Cut repeated work in two consumer P2 entry files (K90, K91), consumer repository.** One `git show` per blob and full builds for malformed cases; the same eight simulations run three or four times. Solution: direct validator tests with one end-to-end refusal; one `git cat-file --batch`; compute simulations once per class. Principle: Tests. Support: reviewer A, reviewer B, reviewer C. Effect: package entry 67.7 s to 5 to 8 s (reviewer A) or under 5 s (reviewer C); collector 25 to 30 s less (reviewer A) or 41.1 s to 15 to 20 s (reviewer C). Disagree: mocking `snapshot` (reviewer C) or a fixture repository (reviewer A).
19. **Promote `v1` on a completed verdict with a plan of what it moves (K73 to K76).** `await` polls three workflows and exits 0 when it gives up; the read class moves parts that change consumer results; manual promotion skips the recheck. Solution: consume the completed verdict; fail visibly; recheck at the tag write; state moved digests or canary. Principle: Changing CI, One verdict. Support: reviewer A, reviewer B, reviewer C (polling); others single.
20. **Release: decide before cloning and bind the verified tag (K69 to K72).** Every tag starts a full clone; only the peeled commit is rechecked; no CI check. Solution: filter or adopt-gate the caller; carry the tag object and name; reviewer C also requires a passed verdict. Principle: Goal, Purpose. Support: reviewer A, reviewer B, reviewer C (K69); reviewer A, reviewer B (K70); reviewer C (K71).
21. **Start automation jobs only when they have work (K49, K60, K61).** The plan job starts for every failed run, the full recording for odd weeks, auto-merge for failed runs. Solution: conclusion and parity checks in job conditions; listen only for relevant workflows. Principle: Goal. Support: reviewer A, reviewer B, reviewer C.
22. **Install a Python project once and cache tools (K11, K12).** Two jobs install the same project; installs are uncached. Solution: one job per project; caches by pin and lock hash. Principle: Jobs. Support: reviewer A, reviewer B, reviewer C (K11); reviewer A, reviewer C (K12). Disagree: reviewer A proposes measuring the cache gain and bundling commitlint. Effect: 10 to 30 s per run from caches (reviewer C estimate).
23. **Close the selection blind spots (K32 to K37, K53, K81).** Hidden expressions, Markdown-only Python skips, `always` removal, ungated step files, estimates-only changes, `.pinned`, top-level `env`, language step on moved digest. Principle: Sound selection. Support: mostly single reviewers; K33 reviewer A and reviewer C; K81 all three. Disagree: K81 direction (X13).
24. **Fix recorder soundness singles (K43 to K45).** Thread-contention drops, listing credit, classification gaps, ETW safety. Principle: Sound selection. Support: reviewer C.
25. **Make the full-recording close robust (K48, K50).** Partial results and a raising listless suite. Principle: One verdict. Support: reviewer A, reviewer C. Disagree: continue with the rest and report (reviewer A) or abort (reviewer C).
26. **Decouple hygiene steps (K55, K14).** One install failure skips four checks; a failed lookup selects nothing; a failed dispatch still runs steps. Principle: Jobs, Sound selection. Support: reviewer A, reviewer C.
27. **Route the ETW rebuild once (K64).** Duplicated failed-run condition. Principle: Shared CI, Jobs. Support: reviewer B, reviewer C; reviewer A. Disagree: into automation (reviewer A), out of shared automation (reviewer C), from a build-mismatch result (reviewer B).
28. **Retire or date the legacy suite workflows (K18).** Principle: Changing CI. Support: reviewer A, reviewer C. Disagree: reviewer B keeps an adapter.
29. **Sync from one snapshot, with fewer calls (K77, K78, K79).** Principle: Changing CI, growth. Support: single reviewers.
30. **Show per-check outcomes in the verdict (K13).** Principle: One verdict. Support: reviewer A, reviewer C.
31. **Skip consumer checks where they cannot change the verdict (K15).** Principle: Selection. Support: reviewer A, reviewer C.
32. **Fix the remaining tool bugs (K16, K17, K58, K62, K63, K68, K82, K87, K88).** Principle: Purpose, Jobs. Support: single reviewers, mostly reviewer C.

## 7. Open

| Question | Why open | What settles it |
| --- | --- | --- |
| Should tracked lists leave the tree (A1)? | reviewer A and reviewer C say yes; reviewer B says no; reviewer A says only after candidate 10 and a measurement | The rate of runs that fail on additions after candidates 15 and 16, from candidate 17 |
| Is the `behind` clause "everything the change can affect until the newer base has passed" needed for units only the change affects? | reviewer A argues it reruns them without a nameable risk | A decision on the principle's reading sets candidate 2's scope |
| May Gitleaks skip an identical tree (X08)? | reviewer B warns that history can change without the tree | A decision on commits that reach a ref without a PR scan |
| Shard Windows (X03)? | reviewer A for, reviewer B against | Measured Windows job duration against the 3-minute target |
| Do caches, `nproc` workers and a shallow Gitleaks clone pay? | reviewer A gives low confidence; reviewer C gives estimates | One measured comparison each, through normal runs after the change |
| Which of `stat` and `lstat` need tracing on PR runs (X04)? | Depends on the runner's C library | One trace of a representative test on the runner image |
| Python standalone selection: Markdown and empty diff (X13)? | Fixes point in opposite directions | Retiring the standalone route (candidate 14) removes the question; otherwise the choice follows Sound selection |
| ETW rebuild location (X11) and legacy adapter (X15)? | Contradicting fixes | Open decision |
| Hygiene checks inside `plan` or in their own job (K05)? | reviewer A and reviewer C put them in `plan` when no tests run; reviewer B keeps separate jobs | Measured no-test run time after consolidation against the 1-minute target |
| Recovery after a failed dispatch (K65)? | Detect, reconcile, retry or App merge | Open decision; the App-merge route needs approval (reviewer B) |
| Measurement location and data source (K89)? | Three proposals | Open decision on where the job and its token live |
| Runner image identity (K24) | reviewer B says obtaining all runtime inputs cheaply is uncertain | Whether a run object exposes the image version; reviewer A lists the same as unverified |
| Exact consumer P2 process counts (X14) and reviewer A's 84 s outside pytest's timer | Estimates; not needed for the fix | One traced run of the entry, if the number is needed |
| Claims the comparison did not check (marked "not checked" in section 4) | Outside the eight-per-report sample | Read the cited lines before ticketing those items |
