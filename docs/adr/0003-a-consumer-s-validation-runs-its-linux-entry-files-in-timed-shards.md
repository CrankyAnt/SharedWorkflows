# A consumer's validation runs its Linux entry files in timed shards on one runner label

The validation workflow of the suite-selection consumer kit runs the selected Linux entry files as this repository's own `validate.yml` does: one shard job per item of the `shards` output of `suite-selection.yml`, each running its entry files two at a time, and one Windows job that runs every selected Windows entry file from the `windows` output. `select` sizes the shards from the stored duration estimates so that each shard's estimated time stays under a bound, so a run that selects most entry files takes about as long as its largest shard instead of its slowest job. In a consumer, a pull-request run that selected every suite took 5 min 19 s, with its jobs between 170 s and 285 s, while each job ran its entry files one after another.

Every shard job runs on the one runner label that the caller's `runner-labels` gives for `linux`, and the Windows job on the one given for `windows`. A shard holds entry files of any suite, so a shard job sets up only what its entry files state, through setup steps gated on the shard's `setups`, and no step of the validation workflow is gated on a suite. A change to a step of the validation workflow that applies before or while its entry files run therefore runs every suite; a change to a setup step runs the entry files that state its setup.

A validation workflow in the earlier form, with one job per suite group and a gate per suite, keeps working at `v1`, because `select` still reads its gates.

## Considered Options

- One job per suite group, each with its own runner label and gates per suite, lets a change to one suite's step run only that suite, but a run that selects most suites takes as long as the slowest group, and each consumer balances its groups by hand.
- A shard matrix per runner label would allow several Linux labels, but the runner label is part of the reuse key, and no consumer of the suite selection uses more than one Linux label, service containers, a job container or a matrix of its own.
