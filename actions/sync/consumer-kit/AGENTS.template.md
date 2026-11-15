# Management repository

This private repository runs the sync of Shared Workflows and holds nothing else: the caller `.github/workflows/sync.yml`, the environment `sync` with the sync App's Client ID and private key, this file and the README. It develops no product.

- Change no file here except to bring it in line with the management repository kit of Shared Workflows at `v1`: `actions/sync/consumer-kit/`. `.github/workflows/sync.yml` equals the kit's `sync.yml` byte for byte.
- Add no workflow, no secret outside the environment `sync` and no other code.
- Keep the repository private. Its issues and run logs name the repositories that the sync reaches; copy no such name, and no content of those repositories, into a public place.
- Never print, copy or store the App's private key. A key that may have leaked is rotated as the README says.
- An issue here is a finding of the sync. The README says what each kind asks for; close the issue when it is handled.
- Changing a repository's settings or ruleset, the App, its installation or the environment is a question for the user first.
