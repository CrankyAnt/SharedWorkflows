#!/usr/bin/env bash
# The fixture script that this repository's own CI runs in the free job: it passes when it runs in Bash from the
# repository root and the later of the two named setups, python-3.14, is the one that the Python command names.
set -euo pipefail
[ -n "${BASH_VERSION:-}" ] || { echo "Expected Bash." >&2; exit 1; }
[ -f .github/ci-config.yml ] || { echo "Expected the repository root, found $PWD." >&2; exit 1; }
# setup-python names the command python3 on Linux and macOS, and python on Windows.
python=python3
if ! "$python" --version >/dev/null 2>&1; then python=python; fi
version="$("$python" --version 2>&1)"
echo "The fixture script of the free job ran in Bash $BASH_VERSION on ${RUNNER_OS:-an unknown system} with $version."
case "$version" in
  "Python 3.14."*) ;;
  *) echo "Expected the later setup python-3.14, found $version." >&2; exit 1 ;;
esac
