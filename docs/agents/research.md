# Research

Research skills write "where the repo keeps notes". In this repository that means:

- **Research run** (a research issue and branch are named in the brief): the invoking workflow first creates the issue, with the question and its closure criteria, and a throwaway branch `research/<issue>-<name>`. The issue carries `planning:research` when it is a child ticket of a `map-the-work` map (an issue labelled `planning:map`), and `research:standalone` otherwise. Findings are committed on that branch, pushed, and never merged. The issue's closing comment holds the findings and the conclusion; the branch is supporting evidence and stays.
- **Fact lookup** (no research issue or branch in the brief, for example during grilling): write the file under `.docs/` and return the findings to the invoking workflow.
- **Sources**: material from outside the repository that the code or skills keep depending on, such as third-party API documentation, goes to `docs/sources/` on the default branch, with its source URL and retrieval date at the top. The researcher flags candidates; the invoking workflow decides and stores them through the delivery strategy.
- **Conclusions** go to their home: a spec, an ADR, `GLOSSARY.md` (under the receive rules), the out-of-scope records, `docs/sources/`, `docs/development/` or `docs/guides/`; a conclusion with no such home stays in the issue. The invoking workflow closes the issue when its closure criteria are met.
