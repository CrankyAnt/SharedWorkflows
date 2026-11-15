# Consumers reach the shared parts through a catalogue, apart from their maintenance

An agent in a consumer learns what Shared Workflows offers through one catalogue, [catalogue.json](../../catalogue.json), read at `v1`, and switches a shared part on in its configuration file, as the catalogue describes what the part does, what it needs from a repository and when it applies. The catalogue holds the decision rule by which an agent switches a part on, uses the free job, or files a request. The catalogue and the consumer kits are part of the shared surface, so a consumer reads what matches the workflows it runs. The documentation for maintaining the shared parts, [docs/maintaining.md](../maintaining.md), stays apart from the catalogue and the kits, and that maintenance happens only in this repository. A consumer's own CI guide is a fixed pointer to the catalogue. An agent in a consumer changes no shared part: for a fault of a shared part or CI that no part covers yet, it files a request in its own repository, which this repository's triage turns into a public issue in general terms.

## Considered Options

- A catalogue written into every consumer's CI guide goes stale and makes every new shared part a pull request in each consumer.
- Sending a consumer's agent to the maintenance documentation mixes the consumer's needs with maintenance: an agent that read only the technical files at `v1` adopted the suite selection by copying this repository's own validation workflow, with its checkout paths and specifics.
- Leaving the adoption of a shared part to the user alone keeps an agent from recognising when its repository needs one.
- Reporting each candidate for a new shared part for a decision by hand depends on someone noticing it in each repository: consumers built workflows of their own for CI that several repositories share.
- Copying a consumer kit's templates into each consumer makes every change to a template a pull request in each consumer, and nothing shows a consumer that its copy is out of date.
