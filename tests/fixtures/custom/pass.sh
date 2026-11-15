#!/usr/bin/env bash
# The fixture script that this repository's own CI runs in the free job: it passes when it runs in Bash from the
# repository root, both named setups, python-3.13 and then python-3.14, were installed in that order, and the Python
# command names the later one.
set -euo pipefail
[ -n "${BASH_VERSION:-}" ] || { echo "Expected Bash." >&2; exit 1; }
[ -f .github/ci-config.yml ] || { echo "Expected the repository root, found $PWD." >&2; exit 1; }

# Each Python setup puts its folder in front of PATH, so the later setup's folder comes first.
position() {
  local index=0 entry entries
  IFS=: read -ra entries <<<"$PATH"
  for entry in "${entries[@]}"; do
    case "$entry" in */Python/"$1".*) echo "$index"; return ;; esac
    index=$((index + 1))
  done
  echo "Expected the setup python-$1 in PATH, found none." >&2
  exit 1
}
earlier="$(position 3.13)"
later="$(position 3.14)"
[ "$later" -lt "$earlier" ] || { echo "Expected python-3.14 to be installed after python-3.13." >&2; exit 1; }

# setup-python names the command python3 on Linux and macOS, and python on Windows.
python=python3
if ! "$python" --version >/dev/null 2>&1; then python=python; fi
version="$("$python" --version 2>&1)"
echo "The fixture script of the free job ran in Bash $BASH_VERSION on ${RUNNER_OS:-an unknown system} with $version;" \
  "python-3.14 comes before python-3.13 in PATH."
case "$version" in
  "Python 3.14."*) ;;
  *) echo "Expected the later setup python-3.14, found $version." >&2; exit 1 ;;
esac
