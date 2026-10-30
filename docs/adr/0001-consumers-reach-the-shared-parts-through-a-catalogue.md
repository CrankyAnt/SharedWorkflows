# Consumers reach the shared parts through a catalogue, apart from their maintenance

An agent in a consumer learns what Shared Workflows offers through one catalogue, [catalogue.json](../../catalogue.json), read at `v1`, and adopts a shared part through that part's consumer kit in `actions/<part>/consumer-kit/`: general templates and a short explanation of what the part does, what it needs from a repository and when it applies. The catalogue and the consumer kits are part of the shared surface, so a consumer reads what matches the workflows it runs. The documentation for maintaining the shared parts, [docs/maintaining.md](../maintaining.md), stays apart from the catalogue and the kits, and that maintenance happens only in this repository. A consumer's own CI guide names where the catalogue is, the parts the repository uses, the principles of its CI and the route for reports, and describes no part beyond those. An agent in a consumer changes no shared part: it reports a fault of a shared part, or a candidate for a new one, to the user, who decides whether it becomes an issue in this repository.

## Considered Options

- A catalogue written into every consumer's CI guide goes stale and makes every new shared part a pull request in each consumer.
- Sending a consumer's agent to the maintenance documentation mixes the consumer's needs with maintenance: an agent that read only the technical files at `v1` adopted the suite selection by copying this repository's own validation workflow, with its checkout paths and specifics.
- Leaving the adoption of a shared part to the user alone keeps an agent from recognising when its repository needs one.
