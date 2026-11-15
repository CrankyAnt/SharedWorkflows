# A consumer's validation runs its Linux entry files in timed shards on one runner label

The suite jobs, the shared workflow `suite-jobs.yml`, run the selected Linux entry files for the suite selection in the pipeline and for the full recording: one shard job per item of the `shards` output of the selection, each running its entry files two at a time, and one Windows job that runs every selected Windows entry file from the `windows` output. `select` sizes the shards from the stored duration estimates so that each shard's estimated time stays under a bound, so a run that selects most entry files takes about as long as its largest shard instead of its slowest job. In a consumer, a pull-request run that selected every suite took 5 min 19 s, with its jobs between 170 s and 285 s, while each job ran its entry files one after another.

Every shard job runs on the one runner label that the suite list gives for `linux`, and the Windows job on the one it gives for `windows`. A shard holds entry files of any suite, so a shard job runs only the setups its entry files state, each setup one script that the suite list names, and no step of the suite jobs is gated on a suite. A change to a step of the suite jobs that applies before or while its entry files run therefore runs every suite; a change to a setup's script runs the entry files that state its setup.

A consumer's own validation workflow in an earlier form, the sharded copy of the consumer kit or one job per suite group with a gate per suite, keeps working at `v1`, because `select` still reads its gates.

## Considered Options

- One job per suite group, each with its own runner label and gates per suite, lets a change to one suite's step run only that suite, but a run that selects most suites takes as long as the slowest group, and each consumer balances its groups by hand.
- A shard matrix per runner label would allow several Linux labels, but the runner label is part of the reuse key, and no consumer of the suite selection uses more than one Linux label, service containers, a job container or a matrix of its own.
