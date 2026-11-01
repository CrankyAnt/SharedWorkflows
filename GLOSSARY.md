# Shared Workflows

Shared Workflows is the set of workflows, steps and tools that this repository offers to other repositories at the tag `v1`, and the repositories that call them. This glossary defines its language; the maintenance procedure is [docs/maintaining.md](docs/maintaining.md).

## Language

### Callers

**Consumer**:
A repository that calls the shared workflows and shared steps at `v1`.
_Avoid_: Calling repository, client

**Shared workflow**:
A reusable workflow of this repository that a caller calls at `v1`, or that this repository's own CI calls by local path.
_Avoid_: Central workflow, reusable workflow

**Shared step**:
A step of this repository that a consumer's job uses at `v1`, or that this repository's own CI uses by local path.
_Avoid_: Central step, shared action

**Caller**:
A workflow that calls one shared workflow and runs no step of its own: in a consumer at `v1`, or in this repository by local path.
_Avoid_: Wrapper, caller repository

**Validation caller**:
The caller that runs the suite selection, calls the one validation workflow with its shards and its Windows entry files, and gives the verdict. This repository's own validation caller calls the shared workflows by local path and runs its suites itself.
_Avoid_: Validate caller, validation.yml

**Validation workflow**:
A consumer's one workflow that runs its selected entry files, started only by the validation caller: the Linux entry files in shard jobs on one runner label, and the Windows entry files in one Windows job.
_Avoid_: Test workflow, called workflow

**Repository workflow**:
A workflow of this repository that is no shared workflow and no caller and that runs only for this repository, such as the pins updater, `promote-v1.yml` or the full recording of this repository's own suites.
_Avoid_: Shared workflow, internal workflow, maintenance workflow

### Shared parts

**Shared part**:
A part of Shared Workflows that a consumer can adopt, such as repository hygiene, Dependabot auto-merge or the suite selection, with its shared workflows and shared steps.
_Avoid_: Central part, shared component, offering

**Part folder**:
The subfolder of `actions/` that holds one shared step: its composite action and the scripts and files it loads, and the consumer kit of its shared part where that kit lies.
_Avoid_: Part (for the folder), tool folder, Tool

**Catalogue**:
The one entry, read at `v1`, through which an agent in a consumer learns which shared parts exist, what each is for, and where its consumer kit is.
_Avoid_: Registry, index, shared surface

**Shared surface**:
Everything a consumer can reach at `v1`: the shared workflows, the shared steps with the files they load, the catalogue and the consumer kits, which a move of `v1` compares.
_Avoid_: Central surface, public API

**Consumer kit**:
What an agent in a consumer needs to adopt one shared part, its templates and their explanation, kept with that part and apart from the documentation for maintaining the shared parts.
_Avoid_: Consumer documentation, setup template

**Pin source**:
The workflow of this repository at `v1` whose action pins a consumer copies into a consumer kit template, one named per template; it holds action pins, where the pins file holds tool pins.
_Avoid_: Pin file, pin reference

### The v1 channel

**v1 channel**:
The tag `v1` through which every consumer receives the shared surface: a change under its paths reaches a caller only when `v1` moves, and every move changes the later runs of every caller.
_Avoid_: Release, release channel, v1 branch

**Promotion**:
A move of `v1` forward to a descendant commit whose CI on `main` passed: by hand after the user's yes, or automatically, by `promote-v1.yml`, for the updater App's pins pull request once the repository owner merged it. A move back to an earlier commit is a rollback.
_Avoid_: Release, publish, tag bump

**Sweep**:
The search through the owner's repositories, archived ones left out, for the consumers of the suite selection: each repository whose default-branch workflow files call the suite selection at `v1`. A manual move of `v1` sends a `v1-moved` dispatch run to each one whose calling workflow lists that event.
_Avoid_: Scan, consumer search

**Pins file**:
The file `actions/repository-hygiene/pins.json`, which holds every exact version and sha256 of the tools that the shared workflows and shared steps install.
_Avoid_: Lock file, versions file, pins.json

**Updater App**:
The GitHub App whose installation token the workflows that propose or push changes use, so that their pull request or push starts CI: the pins updater, the specification updater, the full recording, the additions workflow and the rebuild workflow of the ETW consumer. Only its pins pull request lets `promote-v1.yml` move `v1`.
_Avoid_: Bot account, publication-checks App

**Shared digest**:
The SHA-256 over the files of the recording step, at the revision of Shared Workflows that a run uses, and the modules it loads; a consumer runs every suite when it differs from the digest of the last passed run on its base branch. A job's log prints it as `shared CI digest <digest>`, the line that `suite_selection.py shared-lookup` reads, so that line keeps its text.
_Avoid_: Shared CI digest, recording hash

### Checks

**Shared check**:
A check that applies to every repository with a given property, such as all repositories, one language or one file kind, and that only Shared Workflows holds; a check that applies to one repository's product stays in that repository.
_Avoid_: Central check, common check, generic check

**Language step**:
The shared step that runs the shared checks of one language inside the consumer's job that installs that language's toolchain.
_Avoid_: Lint job, language job, language workflow

**Floor**:
The checks that every pull request runs, whatever it touches: the convention checks of its branch name, commit subjects and title, and the credential scan, which also runs on every other event.
_Avoid_: Baseline checks, always-on checks

**Check name**:
The name `<caller job> / <shared job>` that the check of a job in a shared workflow carries in a caller and by which a ruleset requires that check.
_Avoid_: Status check name, job name

**Verdict**:
The one check of a validation run that a repository requires: it passes only when the suite selection succeeded and no unit it started failed or was cancelled.
_Avoid_: Status check, gate job

### Runs

**Tested tree**:
The tree that a pull-request run checked out: the head merged with the base at the moment of the run.
_Avoid_: PR tree, head tree

**Dispatch run**:
A run on the default branch that a `repository_dispatch` event starts and that selects and checks as a push of one commit would: a `dependabot-merged` run on the commit of a Dependabot merge, or a `v1-moved` run on the default branch's tip after a manual move of `v1`.
_Avoid_: Repository dispatch, triggered run

**Push lookup**:
The step of a push run or a dispatch run that finds the pull-request run that tested the commit it runs on and names its case, `identical`, `behind`, `pull-request` or `own`, from which the suite selection and the hygiene job select.
_Avoid_: PR lookup, merge lookup

**Landing base**:
The commit that a push lands on: the push's previous head, or the commit's first parent when the push has no previous head or the run is a dispatch run.
_Avoid_: Push base, before commit

**Suite selection**:
The shared step that decides, from a change and the suite list, which units run and which reuse a passed result.
_Avoid_: Test impact analysis, path filter, derivation

**Unit**:
What the suite selection runs, skips or reuses as one: a suite, or an entry file of a suite selected per entry file.
_Avoid_: Job, test case

**Reuse**:
Taking a unit's passed result instead of running the unit again, when everything that result depends on equals the run that passed.
_Avoid_: Cache hit, skip

### Suites

**Suite**:
A named set of entry files with one subject and one command, which the suite selection runs as a whole or, for a suite selected per entry file, per entry file.
_Avoid_: Test group, job

**Suite list**:
The file of a repository that names every suite with its entry files, its command, the setups it needs, the duration estimate of each entry file and what it is selected on.
_Avoid_: Suite file, suites.json

**Setup**:
A named step that a job runs before its entry files, such as installing dependencies or building, which a suite or one of its entry files needs.
_Avoid_: Install step, prepare step

**Duration estimate**:
The traced test time that one entry file is expected to take, stored in the suite list. Only the full recording renews it, from the corrected value of a clean entry file, and only when that value differs from the stored estimate by more than 25%.
_Avoid_: Duration, test timing

**Shard**:
A share of the selected Linux entry files, with the setups they need, sized so that its estimated time stays under a bound; one shard job runs it.
_Avoid_: Partition, batch, chunk

**Shard job**:
A job that runs entry files in a validation run or a full recording: on Linux the job of one shard, on Windows the one job that runs every Windows entry file.
_Avoid_: Runner, worker, test job

**Speed factor**:
How much slower or faster one shard job ran in a full recording than the duration estimates expect: the median ratio of its clean entry files' measured times to their stored estimates, or 1 when it has no such entry file.
_Avoid_: Machine speed, runner speed

**Corrected value**:
An entry file's traced time in a full recording, brought to the trace set of the duration estimates and divided by the speed factor of its shard job, which is what the full recording compares with the stored duration estimate.
_Avoid_: Adjusted duration, normalised time

**Entry file**:
A file that a suite starts directly, such as a test module, a script or a test project, whose reads a recording keeps apart from those of the suite's other entry files.
_Avoid_: Entry, test file

**Suite selected per entry file**:
A suite whose entry files are each selected on their own reads, so that a run starts only the entry files a change reaches.
_Avoid_: Partial suite, split suite

### Tests

**Real-command test**:
A test that starts the code under test as its own process, the way a user or CI starts it.
_Avoid_: End-to-end test, integration test, slow test

**In-process test**:
A test that calls the code under test inside the test's own process.
_Avoid_: Unit test, fast test

**Shared-host test**:
A test that hands the code under test to a long-lived process that many tests share, instead of starting it as its own process.
_Avoid_: In-process test, batch test, warm test

**ETW access setup**:
The one-time setting, made from an elevated shell and undone by its own restore, that lets one local account start the recording step's live ETW session without elevation: membership of Performance Log Users and access to the kernel file, kernel process and system trace providers.
_Avoid_: ETW permissions, admin setup, elevation setup

### Reads

**Recording**:
The files, directory listings and existence probes that a suite's entry files and the setup steps of the job that runs it make during one run, as the operating system reports them, together with the module lookups that an interpreter reports for them.
_Avoid_: Trace, recorded derivation, dynamic derivation

**Recording step**:
The shared step that records what a job's entry files and setups read, and compares that recording with the stored lists.
_Avoid_: Tracer, recorder

**Full recording**:
The periodic run that records every entry file of a repository and, for each clean entry file, replaces its stored lists and renews its duration estimate, while every other entry file keeps both; the only run that removes a stored read.
_Avoid_: Re-seed, seeding, full trace

**Clean entry file**:
An entry file that, in a full recording, exited with 0 and whose test report shows no skipped test; only a clean entry file can lose a stored read or get a new duration estimate.
_Avoid_: Passing entry file, green entry file

**Stored lists**:
The path list, directory memberships, existence reads and lookup directories that the suite list holds for a suite or an entry file, written from recordings.
_Avoid_: Read lists, path lists (for all of them)

**Addition**:
A read in a recording that the stored lists do not hold; the recording step reports it, and the change adds it to the lists.
_Avoid_: New read, missing path

**Additions workflow**:
The workflow of a consumer, from the consumer kit of the suite selection, that applies the additions of a validation run to the suite list and proposes the change as a pull request onto the branch that reported them.
_Avoid_: Apply workflow, apply-additions.yml

**Setup read**:
A read that a step before the suites of a job makes, such as installing dependencies or building, and that belongs to the path list of every suite and entry file that needs the step.
_Avoid_: Global input, build read

**Path list**:
The patterns of the files whose content a suite reads, stored in the suite list and produced from recordings.
_Avoid_: Paths, read list

**Directory membership**:
The names that a directory, or a pattern over names, holds, as a suite reads them: adding, removing or renaming a matching name selects the suite, and a content change does not.
_Avoid_: Directory listing, glob read

**Existence read**:
Whether one path exists as a file or a directory, as a suite reads it: only its appearing or disappearing selects the suite.
_Avoid_: Existence predicate, existence check

**Module lookup**:
A module name that an interpreter looked for in a directory of its module search path without finding a module or package of that name there, a namespace portion counting as not found, as a suite reads it: adding a file or directory there that can carry that module name selects every suite that looks up modules in that directory.
_Avoid_: Import probe, shadow read

**Lookup directory**:
A checkout directory in which a suite, or an entry file of a suite selected per entry file, looks up modules, as its lookup reports name it.
_Avoid_: Search directory, import directory

**Lookup report**:
What an interpreter reports for each of its processes: the directories that its module resolution lists and the module names that it looks up in each, found or not. A recording takes its module lookups from it.
_Avoid_: Import trace, import log

**Extra read**:
A read that no recording sees, stated in the suite list by the change that adds the read, beside the recorded reads, as content patterns, directory memberships or existence reads.
_Avoid_: Extra paths, manual path
