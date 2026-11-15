# A repository runs its Python tests through either the suite selection or Python tests

A repository runs its Python tests through exactly one shared part: the suite selection, which runs only the affected entry files and gives the verdict, or Python tests, the part `python-test`, which runs the whole suite of each configured project. The language checks are the part `python-verify`, which runs beside either. The suite selection pays only when running the affected suites saves more than its selection, recording and stored lists cost; a suite that runs in seconds saves nothing, so such a repository takes Python tests, and a repository whose suites outgrow it moves to the suite selection in one pull request that switches `python-test` off and the suite selection on in its configuration file. The catalogue entry of `python-test` excludes a repository that uses the suite selection.

## Considered Options

- Both at once would run every Python test twice on each change and leave unclear which part a failure belongs to.
- The suite selection for every size would give each repository with a short suite a suite list with recorded path lists, the full recording and the GitHub App, whose upkeep exceeds what the selection saves on such a suite.
