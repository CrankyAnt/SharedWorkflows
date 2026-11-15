#!/usr/bin/env bash
# The fixture script that this repository's own CI runs in the free job: it passes when the named setup is in place.
set -euo pipefail
version="$(python3 --version)"
echo "The fixture script of the free job ran on $version."
case "$version" in
  "Python 3.14."*) ;;
  *) echo "Expected the setup python-3.14, found $version." >&2; exit 1 ;;
esac
