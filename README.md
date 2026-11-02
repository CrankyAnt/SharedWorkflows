# Shared Workflows

Shared Workflows provides reusable GitHub Actions workflows, shared steps and tools for repository maintainers and their coding agents. Consumers use the shared parts at `v1` to check repository hygiene, merge qualifying Dependabot updates and run affected test suites.

## Scope and authority

This repository owns the shared implementations and their consumer kits. Each consuming repository owns its caller workflows, test suites, configuration and repository settings. The [catalogue](catalogue.json) describes which shared parts apply and what each requires.

## Start here

Read the [catalogue at `v1`](https://github.com/CrankyAnt/SharedWorkflows/blob/v1/catalogue.json), choose the shared part that fits your repository, and follow its linked consumer kit at the same tag. Copy and configure the caller templates as the kit describes. The repository-hygiene kit also explains the required merge settings and default-branch ruleset.

To update an existing consumer, follow "When a repository adopted it earlier" in the relevant [consumer kit](#shared-parts).

To change the shared parts themselves, start with the [recurring maintenance tasks](docs/maintaining.md#recurring-tasks).

## Source and publication boundaries

The editable implementations live in [`.github/workflows/`](.github/workflows/) and [`actions/`](actions/). This repository tests its shared workflows and steps by local path. Consumers call the published versions at `v1`; merging a change to `main` does not by itself update that tag.

Consumer kits contain templates to copy into a consuming repository. The committed [ETW consumer build](actions/recording/etw-consumer/build/) is generated from its [source project](actions/recording/etw-consumer/EtwConsumer.csproj); rebuild it rather than editing the binaries. The [Conventional Branch specification](actions/publication-checks/spec.json) is an upstream snapshot with its own license and [source metadata](actions/publication-checks/spec-metadata.json).

## Shared parts

These links describe the current checkout. Use their `v1` versions when adopting a published part.

- [Repository hygiene](actions/repository-hygiene/consumer-kit/README.md), with the [ruleset template](actions/repository-ruleset/README.md) for the default branch
- [Dependabot auto-merge](actions/dependabot-auto-merge/consumer-kit/README.md)
- [Suite selection](actions/suite-selection/consumer-kit/README.md)
- [Python language step](actions/python-language-step/consumer-kit/README.md)

## Validation

Pull-request CI runs repository hygiene and selects affected tests through the validation caller. The required checks are `hygiene / hygiene` and `validation / validation`. See the [test-suite guide](tests/README.md) for local entry-file commands and prerequisites, and [Validation](docs/maintaining.md#validation) for the completion procedure.

## Publication

The moving `v1` tag is the consumer channel. Its [promotion and rollback procedure](docs/maintaining.md#the-v1-channel) controls when changes reach consuming repositories, including the automatic promotion of qualifying pins-only updates.

## Documentation

- [Maintenance guide](docs/maintaining.md) for shared-part behavior and maintenance tasks.
- [Test-suite guide](tests/README.md) for suites, tools and local validation.
- [Glossary](GLOSSARY.md) for repository terminology.
- [Architecture decisions](docs/adr/) for recorded design decisions.

## Reporting issues

Report defects and proposed improvements in [GitHub Issues](https://github.com/CrankyAnt/SharedWorkflows/issues). Include the affected shared part, the workflow run and the expected behavior. Keep credentials and other sensitive data out of reports and logs.

## Contributing

Read [AGENTS.md](AGENTS.md) and the [delivery rules](docs/agents/delivery.md) before changing shared parts. The [maintenance guide](docs/maintaining.md#recurring-tasks) routes each kind of change to its procedure.

## License and upstream ownership

Original project material is licensed under the [MIT License](LICENSE.md). Keep its copyright and permission notice when copying or redistributing it.

This repository uses per-file license assignments according to the REUSE specification. [REUSE.toml](REUSE.toml) is the authoritative machine-readable record of licenses and copyright holders; [LICENSES/](LICENSES/) contains the license texts.

Third-party material retains its upstream terms. The bundled [Conventional Branch specification](actions/publication-checks/spec.json) is an unmodified snapshot from [Conventional Branch](https://conventionalbranch.org/v1.1.0/spec.json), licensed under [CC-BY-4.0](LICENSES/CC-BY-4.0.txt). The ETW consumer build includes MIT-licensed dependency assemblies from Microsoft and the .NET Foundation, recorded in [packages.lock.json](actions/recording/etw-consumer/packages.lock.json). Their copyright notices remain in the per-file assignments in [REUSE.toml](REUSE.toml).
