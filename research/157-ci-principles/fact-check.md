# Fact check of the checkable items in rounds-1-2-consolidated.md, section 7

Scope: items 2, 3, 4, 6, 7, 19, 21, 22, 23 and 24 of section 7. No design opinions, no recommendations. All GitHub access was read-only (`gh api` GET requests, job logs, the docs site). Clock times are Europe/Amsterdam (CET, UTC+1 throughout this period) unless a raw timestamp is quoted. Run lists, job logs and scripts used for the checks are not published; each figure cites its run, commit or PR.

Data collected for this check:

- Run lists of all 16 repositories for 14-24 Nov with `referenced_workflows`. SharedWorkflows has 1,742 runs in that range; the API returns at most 1,000, which still reaches back to 15 Nov 12:37 CET and covers every window used below.
- Job details for the runs used, selected job logs, PR links.

## Item 2. Real billed minutes

**Finding.** Not readable with the current token. `GET /users/CrankyAnt/settings/billing/usage` (also with `?year=2026&month=11`), `GET /users/CrankyAnt/settings/billing/usage/summary?year=2026&month=11` and the older `GET /users/CrankyAnt/settings/billing/actions` all return HTTP 404. The response names the `user` scope, which the token in use does not have.

**What access is needed.** A classic token with the `user` scope (from the response header), or a fine-grained token with the user permission "Plan" (read), as the REST reference states for "Get billing usage report for a user" (https://docs.github.com/en/rest/billing/usage). The same page says that endpoint is only available to users with access to the enhanced billing platform. The report takes `year`, `month` and `day`, so 15-24 Nov can be read per day. Nothing was changed. The item stopped there.

**Confidence.** High on the missing scope. Whether the account is on the enhanced billing platform is unknown.

**Against rounds-1-2-consolidated.md.** Confirms the opening note and item 2: every minute figure stays an estimate. Adds the exact endpoint and the access it needs.

## Item 3. A comparable push-rerun rate

**Window and definition.** 15 Nov 20:00 CET to 24 Nov 02:15 CET (2026-11-15T19:00Z to 2026-11-24T01:15Z). Candidates: `CI` runs (`.github/workflows/ci.yml`) with event `push` or `repository_dispatch` on `main`. A run qualifies when the merged PR of its head commit (`GET /commits/{sha}/pulls`) has a passed `CI` `pull_request` run on its PR head commit (latest run for that head). It counts as a rerun when it still ran a test part (`python-test`, `dotnet-test`, `node-test`, `custom`) or a suite job (`suite-selection-jobs / shard N` or `/ windows`). `python-verify` is reported separately. Job-seconds are measured job durations (latest attempt). The standalone `Python tests` workflow in SharedWorkflows is not included. 

**Finding.** 129 candidate runs; 124 had a merged PR whose head passed CI; the other 5 were direct pushes without a PR (consumer P3 43789099043, consumer P4 43788735356, consumer P6 43789427480, WR_FreeMouse 43790501578, TowerGlance 43788562318) and fall outside the definition. **49 of the 124 reran a test part or suite job, spending 7,004 job-seconds in those jobs** (9,720 job-seconds for all jobs of those 49 runs). The 124 qualifying runs also spent 1,023 s in `python-verify`; 3 SharedWorkflows runs ran only `python-verify` and no test part (44633752215, 43967549441, 43775310398).

| Repository | Qualifying | Reran | Test/suite job-s | Run IDs (test/suite job-s; `d` = `dependabot-merged` dispatch) |
| --- | --- | --- | --- | --- |
| SharedWorkflows | 27 | 12 | 1,459 | 43752085937 (44), 43764554415 (63), 43769030469 (61), 43772711871 (66), 43856473682 (86), 43863691097 (81), 44256457266 (90), 44256931981 (164), 44453415119 (74), 44615336254 (83), 44623574311 (69), 44652722230 (578) |
| consumer P2 | 28 | 6 | 2,519 | 44310108409 (83), 44460541039 (505), 44460689694 (629), 44577131934 (427), 44578213366 (352), 44656821263 (523) |
| consumer P3 | 16 | 16 | 2,202 | 43754452155 (164), 43754963868 (136, d), 44257926354 (148), 44460433858 (127), 44539201470 (123, d), 44539240071 (112, d), 44582932133 (128), 44583086625 (142), 44583108238 (115), 44583140626 (160), 44583908414 (147), 44585709550 (156), 44589047069 (157), 44597705234 (135), 44656858519 (123), 44658073078 (129) |
| consumer P4 | 9 | 9 | 640 | 43752334668 (63), 43752605694 (67, d), 43752623895 (70, d), 43752722152 (72, d), 43783350901 (76), 44257693092 (74), 44460318639 (72), 44472280376 (74, d), 44656694808 (72) |
| consumer P5 | 3 | 3 | 127 | 43751409355 (44), 44257861836 (40), 44460376440 (43) |
| WR_FreeMouse | 3 | 3 | 57 | 43782393788 (19), 44257691776 (19), 44460317598 (19) |
| consumer P1 | 17 | 0 | 0 | |
| RailroaderScenePatcher | 4 | 0 | 0 | |
| consumer P6, consumer P7, consumer P8, McpForGiantsEditor, TowerGlance, three FS25 repos | 22 | 0 | 0 | |
| **Total** | **124** | **49** | **7,004** | |

Why this differs from reviewer A's 44 of 111: reviewer A's stored SharedWorkflows run list holds 300 runs and starts at 19 Nov 05:36 CET, so it saw 9 of the 27 SharedWorkflows pushes in the window. Reviewer A also counted the direct consumer P3 push 43789099043 and any non-fixed job, `python-verify` included.

**Confidence.** High for the counts and job-seconds. Medium for the dispatch-to-PR mapping: a `repository_dispatch` run's `head_sha` is the branch head at dispatch time, mapped to its PR through `commits/{sha}/pulls`.

**Against rounds-1-2-consolidated.md.** Replaces the three non-comparable figures in note T1 (reviewer A 44 of 111, reviewer B 18 of 58, reviewer C 39 of 101) with one figure on one definition: 49 of 124, 7,004 test/suite job-seconds. Confirms "consumer P1 0" and "consumer P3, consumer P4, consumer P5 and WR_FreeMouse reran on every push" (candidate issue 1) for pushes with a PR. Reviewer A's estimate of about 4,300 test-part job-seconds covers test parts only; on this definition test parts alone are 7,004 − 2,519 (consumer P2 suites) − 742 (SharedWorkflows suites in 44256931981 and 44652722230) = 3,743 s.

## Item 4. The additions loop at `main` after `v1` moved to `2b81d7c`

**Finding.** No instance exists yet. Consumer runs first used `v1` = `2b81d7c` on 20 Nov 02:50 CET (first observation in `referenced_workflows`). From then until the end of the collected data (24 Nov 05:52 CET), the three suite-selection repositories ran these `CI` runs: SharedWorkflows 2 (both PR runs, success), consumer P2 4 (all success), consumer P1 16 (2 failed).

- consumer P1 45312164297 (pull_request, 24 Nov 03:24 CET): shard 1 failed in "Run the shard's entry files under the recording, two at a time"; the recording check did not fail. A test failure.
- consumer P1 45326499470 (push, 24 Nov 05:27 CET): shard 1 failed in both the test-running step and "Check the recording against the suite list". Tests failed too.

So **0 failed runs failed only on the recording check** with passing tests, and no run carries the subject `chore: apply the recorded additions of run <id>`. No unit-level comparison of a failed run and its apply-head run can be made on code at `2b81d7c`.

**Confidence.** High (complete run lists for the three repositories in that period).

**Against rounds-1-2-consolidated.md.** Item 4 and note T12 stay open: what CC1 and `tests/test_suite_selection_reuse.py:588-608` describe has not run in production yet. The T12 counts (reviewer A 15, reviewer C 5 of 9, reviewer B 3 of 4, consumer P1 2 of 2, SharedWorkflows 4 of 7) all predate the move.

## Item 6. T6 measured instance, consumer P2 run 44460689694

**Finding.** Reviewer A's instance holds as stated.

- Push run 44460689694 (19 Nov 05:47 CET, `aa246c8`, squash of #749; `v1` = `377a775`). Lookup: `behind: pull request #749 was tested on db697b96… and landed on 02204796…`. Digest `da025f82…`. Shared lookup: `moved: the shared recording step differs from that of run 44454769048, the last passed run on main`. All six suites logged `run (every suite runs: the shared recording step differs from that of run 44454769048, the last passed run on main)`.
- PR run 44460245130 (19 Nov 05:43 CET, PR head `445d97c`, also `v1` = `377a775`). Digest `da025f82…`. It took run 44454769048 (`db697b9`, which ran on `v1` = `1519e29`) as the last passed `main` run, found `moved`, and all six suites logged the same reason. This PR run itself ran every suite: 581 job-seconds, 506 of them in suite jobs, wall 232 s.
- Push run cost: 708 job-seconds (629 in suite jobs: windows 82 s, shards 159, 151, 237 s), wall 287 s (created 05:47:15, updated 05:52:02 CET).

Context: `v1` moved to `377a775` about one minute before the PR run (first consumer observation 19 Nov 05:42 CET). Both runs compared with a `main` run on the previous `v1`. In the `behind` case the code passes no tested digest (`reuse.py:473-476` at `2b81d7c`: `tested_digest` returns a value only for `identical`), so the push compared with 44454769048 and not with the PR run's equal digest.

**Confidence.** High.

**Against rounds-1-2-consolidated.md.** T6 "measured instance: reviewer A only" is now verified by a second check: lookup `behind`, equal digest `da025f82` in PR and push run, six suites rerun for the `moved` reason, 708 job-seconds, wall 287 s. New detail: the PR run also ran every suite for the same `v1` move, so this instance combines T6 with T7.

## Item 7. Run 44652722230 under a per-OS digest

**Facts used.**

- Push 44652722230 (`71dd8de`, 20 Nov 02:19 CET): lookup `behind`, tested base `c950137`, landing base `e075434`. Digest `3a7eb94a…`, compared with run 44633752215 at `e075434`, whose digest is `7ed6afe0…`. Every suite ran for `moved`.
- `e075434..71dd8de` changes only `actions/recording/etw-consumer/build/EtwConsumer.deps.json` and `actions/recording/etw-consumer/global.json`. So the two digests differ only through `etw-consumer/`.
- `c950137..e075434` (`e3762cf`, `e075434`) changes `actions/recording/recording.py`, `suite-jobs.yml`, `reuse.py`, `suite_selection.py`, `tests/suites.json` and 18 other files.

**Method.** A Linux digest over the recording-step files that Linux jobs execute excludes `etw-consumer/`, so it would be equal for `e075434` and `71dd8de`: no `moved`. The selection then follows the `behind` rule (`suite_selection.py:1793-1796`, `both_sides` at `:1428-1441`): a unit runs only when it is selected both over `e075434..71dd8de` and over `c950137..e075434`. `suite_selection.py select` was run on `71dd8de` in a scratch clone with `tests/suites.json`, `--language-step python --pinned-tool gitleaks`, a `behind` lookup naming those bases, and no shared lookup. The selection output is not published. The scratch clone was removed afterwards.

**Finding.** Linux units selected: 4 entry files in 2 suites.

- `ci-policy`: `tests/test_ci_policy_suite_jobs.py`
- `suite-selection`: `tests/test_suite_selection.py`, `tests/test_suite_selection_reuse.py`, `tests/test_suite_selection_workflow.py`

The fifth Linux entry file that reads `etw-consumer/`, `tests/test_ci_policy_etw_consumer.py`, is not selected because nothing changed on the base side meets its reads. The 15 other Linux suites are skipped ("the pull request and the base since its tested base do not both affect it"). Windows: `windows` (2 entries), `etw-consumer-build`, `pinned-tools-windows` and `temporary-support-windows` run. Stored `entry_durations` of the 4 Linux entries: 39.79 + 15.32 + 1.14 + 0.39 = 56.6 s, of 181.2 s stored for Linux entries (seven Linux suites have no `entry_durations`). That is about 31% (estimate from stored durations; job setup not included). The real run spent 481 s in the two Linux shards (238 s and 243 s).

Under the other reading, a Linux digest that includes every file a Linux entry reads, `etw-consumer/` is in it, the digest moves, and every Linux unit runs as it did.

Side fact: the #163 failure (`tests/test_suite_selection_recording_check.py` in shard 1) sits in an entry file that this selection does not run.

**Confidence.** High for the selection under the stated reading (it is the real code's output). Medium for the time share (stored durations).

**Against rounds-1-2-consolidated.md.** Settles item 7 and C1: reviewer C's "Linux shards wholly avoidable" does not hold, since 4 Linux entry files would still run. Reviewer B's "unknown" is now about 57 s of 181 s stored Linux entry time (estimate). This matches reviewer A's K3 estimate of about a third.

## Item 19. Merge-queue availability

**Finding.** The rendered page https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue says merge queues are "available in any public repository owned by an organization" (the full note adds private repositories of organizations on GitHub Enterprise Cloud). The note is a product callout: it is missing from the page's Markdown body API, but present in the HTML. Repositories owned by a personal account are not named. `CrankyAnt` is a user account (rounds-1-2-rounds-1-2-consolidated.md, single B1).

**Confidence.** High.

**Against rounds-1-2-consolidated.md.** Confirms B1 and R-20 "refuted (availability)". Reviewer A's low-confidence belief is now confirmed. Item 19 is closed.

## Item 21. Growth over time

**Suite list size** (blob bytes, `git cat-file -s`):

| Repository | Point | Bytes | Stored strings |
| --- | --- | --- | --- |
| SharedWorkflows `tests/suites.json` | 1 Nov `e59227f` | 556,395 | 9,158 (10 suites) |
| | 14 Nov `cce45e1` | 595,412 | 9,852 (11 suites) |
| | 15 Nov `8b4be53` | 832,781 | 14,203 (15 suites) |
| | 18 Nov `efd39ed` | 881,829 | 14,702 (17 suites) |
| | 19 Nov `e3762cf` (last change before `2b81d7c`) | 973,869 | 16,165 (19 suites) |
| consumer P1 `tests/suites.json` | 1 Nov `0794864` | 680,189 | 12,483 |
| | 14 Nov `cf52498` | 732,391 | 13,370 |
| | 16 Nov `42fb78b` | 740,567 | 13,502 |
| | 23 Nov `4ba3b8e` | 727,723 | 13,280 |
| consumer P2 `.github/suites.json` (added 18 Nov) | 18 Nov `89e016b` | 4,568,586 | 78,475 |
| | 19 Nov `983011e` | 4,652,487 | 79,846 |
| | 20 Nov `b0ce7b1` | 4,655,443 | 79,903 |
| | 23 Nov `83d170b` | 4,662,679 | 79,907 |

SharedWorkflows grew 75% from 1 to 19 Nov, mostly from new suites. consumer P1 grew 7% and then shrank 2% after the 23 Nov list update. consumer P2 grew 2% in five days.

**Commit-check step ("Check every pull request commit") in RailroaderScenePatcher**, all 144 `CI` pull_request runs with that step, 16-24 Nov. Commit counts come from `git rev-list --count origin/main..<head>` in the local checkout (`origin/main` as last fetched, 19 Nov), so they are approximate.

| Day | Runs | Median s | Max s | Commits on head (approx.) |
| --- | --- | --- | --- | --- |
| 16 Nov | 28 | 12 | 22 | 0-66 |
| 17 Nov | 28 | 15.5 | 30 | 67-94 |
| 18 Nov | 41 | 30 | 44 | 85-124 |
| 19 Nov | 11 | 34 | 48 | 125-134 |
| 20 Nov | 19 | 39 | 66 | 135-184 |
| 22 Nov | 6 | 58 | 71 | 212-240 |
| 23 Nov | 6 | 82.5 | 98 | 242-267 |
| 24 Nov | 2 | 87 | 100 | 269-275 |

Median 0.27 s per commit over 139 runs (estimate, because the counts are approximate). The step grows with the PR's commit count: from 12 s to about 87 s over eight days on PR #66.

**Confidence.** High for sizes and step durations; medium for commit counts.

**Against rounds-1-2-consolidated.md.** Answers item 21 and T13's "growth over time is not measured (reviewer B)" for the commit check and the list sizes. Gitleaks growth was not measured.

## Item 22. Single-sourced figures

**22a. Reviewer C's 48.9% stale share of consumer P2's `suites.json`.** The arithmetic reproduces: at `83d170b`, removing reviewer C's categories from the re-serialised file (indent 2, equal to the 4,662,679-byte blob) removes 2,279,035 bytes, 48.9%. The 4.81 MB in reviewer C's round 1 is the CRLF working copy (blob plus 149,178 line ends), not the stored blob.

What the 48.9% holds (byte share when each category alone is removed):

- existence reads ending in `/conda-meta/history` or `/pyvenv.cfg`: 40.8 points (15,174 entry-level items)
- sibling `test_*.py` content reads: 6.1 (5,334 items)
- `<sha>:<path>` operands: 1.8 (277 entry-level, 276 suite-level)
- root `*.md` reads: 0.2 (339 items)
- deleted paths: 0.02 (27 items)

Reviewer C's set mixes rule-based categories with name patterns. Of the 15,174 conda/pyvenv reads, 12,943 to 13,249 match the two discard rules of `docs/maintaining.md:517` (added in `377a775`, #147, 19 Nov 05:36 CET); the rest do not. Root `*.md` reads are a name pattern, not a rule. On an alternative, rule-based set (deleted paths, existence reads that rule 517 discards, operands), the share is 46.4% when each entry's own directory memberships are used and 66.2% when the suite's memberships are added. Classification approximates the rule from stored memberships and the tree at `83d170b`.

Confidence: high on the arithmetic, medium on the categories. Against rounds-1-2-consolidated.md (T8, reviewer C K1): the figure is reproducible, but it depends on the definition. A rule-based count gives 46% to 66%, so "about half" holds as an order of magnitude. The file is 4.66 MB stored.

**22b. Reviewer A's nine `v1` promotions in four days.** Distinct `v1` targets that consumer runs used (`referenced_workflows`, first observation): `798c568` 15 Nov 09:59, `54ca174` 10:34, `ca3a473` 11:29, `5342738` 13:21, `559202f` 20:31, `ba3590f` 16 Nov 01:43, `c80551e` 13:12, `10a3d36` 17 Nov 00:27, `1519e29` 18 Nov 08:15, `377a775` 19 Nov 05:42, `2b81d7c` 20 Nov 02:50 (CET). That makes 9 targets from 15 Nov 10:34 to 19 Nov 05:42, within four days, and 11 from 15 to 20 Nov. Before that, 14 Nov added `17ff27d`, `1225500` and `bff747e`. These are lower bounds: a target that no consumer run used is not seen, and the tag has no activity history through the API (`/activity?ref=refs/tags/v1` returns nothing). The `Promote v1 after a read-class change` workflow moved `v1` once in this period (run 43700896121, `ca3a473`, 15 Nov). In all 30 later runs (to 20 Nov 02:48 CET) its `move` job was skipped, so later moves came by another route. Confidence: high as a lower bound. Against rounds-1-2-consolidated.md (T7, item 17): confirmed. The count depends on the window and is a minimum.

**22c. Reviewer C's per-entry times in run 45181162506.** Confirmed from the shard 1 log (job 137715323167): `Parser/Tests/test_build_package_preserves_runs.py` "34 passed in 67.68s". `Probe/Tests/test_collector64_run.py` "Ran 18 tests in 41.079s". 13 Linux entry groups, shard 1 job 199 s, run wall 256 s (13:09:46 to 13:14:02 UTC). At `83d170b`, `parser`, `tools`, `workshopitem` and `repository-checks` have no `entry_durations`; `probe` has 8 of 8, `reader` 19 of 20. Confidence: high. Against rounds-1-2-consolidated.md (T23): confirmed.

**22d. Reviewer A's 43 s commit-check median in RailroaderScenePatcher.** Reproduced exactly for reviewer A's 43 runs (19 Nov 01:39 to 24 Nov 02:12 CET): median 43 s, max 100 s. Over all 144 `CI` pull_request runs with the step (16-24 Nov) the median is 27 s. The figure depends on the sample period because the step grows with the commit count (item 21). Confidence: high. Against rounds-1-2-consolidated.md (T13): confirmed for its sample. Reviewer B's non-reproduction comes from a different sample, not a wrong number.

## Item 23. The second CI configuration edit on 20 Nov

**Search.** For all 15 consumers: default-branch commits from 20 Nov 00:00 to 24:00 CET touching `.github/ci-config.yml`, `.github/workflows/ci.yml` or `.github/workflows/automation.yml` (`commits?path=…&since=…&until=…`, `commits_20nov.tsv`). Also every PR open at any time on 20 Nov (18 PRs in 5 repositories) and its changed `.github/` files.

**Finding.** Only consumer P3 `2bb8d21` (PR #66, merged 20 Nov 03:03 CET, one commit `5fa8368` at 03:00 CET) changes `.github/ci-config.yml` that day. No fixed caller changed in any consumer that day. Other `.github/` changes on 20 Nov: consumer P3 #65 and consumer P4 #104 (the management repository's app, `.github/dependabot.yml` only) and consumer P2 `.github/suites.json` changes (#763 at 00:45, #780 at 02:53 CET; #796 open). RailroaderScenePatcher PR #66 (open) changes `.github/ci-config.yml`, but its commits touching it are dated 16-17 Nov (`a8a215a`, `ea5bbcf`, `fa02f81`, `0a056d5`).

**Confidence.** Medium-high. A branch that was force-pushed or deleted without a PR would not show.

**Against rounds-1-2-consolidated.md.** Confirms T20 and item 23: the second edit is not in GitHub's record of consumer default branches or PRs open that day.

## Item 24. Minor

- **Test file count at `2b81d7c`.** `git ls-tree -r` gives 121 files named `test_*.py`: 120 directly under `tests/` and 1 fixture, `tests/fixtures/python_tests/tests/test_greeting.py`. 31 are `tests/test_ci_policy_*.py`. Both 120 (reviewer A) and 121 (second count) are right; they count different scopes. Confidence high. Settles the "120 or 121" in T24 and K6.
- **SharedWorkflows#163** ("A recording-check test passes on a pull request and fails on a push to main", opened 20 Nov 02:26, closed 02:50 CET). It reports that `test_check_fails_names_the_exact_additions_and_apply_makes_the_same_recording_pass` takes the CI event from the run it runs in and failed in push run 44652722230 shard 1, which left `main` red on `71dd8de` and blocked `v1` promotion. Its fix is `2b81d7c` (#164, "Refs: #163"), which changes only `tests/recording_fixtures.py`, `tests/test_suite_selection_recording_check.py` and `tests/README.md`. The fixture no longer passes the job's `GITHUB_EVENT_NAME` and `GITHUB_RUN_ID` to the recorded commands; a test states its event with `ci_run`. It does not touch `reuse.py`, `suite_selection.py` or the push or shared lookup, so it does not concern the lookup chain. Confidence high. Against rounds-1-2-consolidated.md (reviewer A K7, "#163 unread"): resolved; #163 is a test-isolation fix, not a lookup repair.
- The exact process behind the stored Git operand (item 24, third part) was not in this check's list.

## Not settled

- **Item 2:** real billed minutes need the `user` scope (classic) or the "Plan" user permission (fine-grained), plus the enhanced billing platform. Not attempted further.
- **Item 4:** no failed run on `2b81d7c` code failed on the recording check alone, and no apply-head run exists yet, so the unit-level comparison cannot be made. It needs a future occurrence.
- **Item 21:** Gitleaks history-scan growth over time was not measured; only list sizes and the RailroaderScenePatcher commit check were.
- **Item 22a:** the stale share depends on the definition (46% to 66% rule-based against reviewer C's 48.9%). The stored data cannot decide which reads a current recording would keep.
- **Item 23:** a force-pushed or deleted branch outside a PR cannot be excluded.
