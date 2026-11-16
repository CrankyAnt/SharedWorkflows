#!/usr/bin/env bash
# The setup commitlint: installs the locked commitlint toolchain with the npm of the Node.js that the job installed.
set -euo pipefail
export npm_config_update_notifier=false
cp actions/publication-checks/package.json actions/publication-checks/package-lock.json "$SETUP_DIRECTORY/"
cd "$SETUP_DIRECTORY"
npm ci --ignore-scripts --no-audit --no-fund --userconfig /dev/null --prefix "$SETUP_DIRECTORY"
echo "variable=PUBLICATION_CHECKS_TOOLS=$SETUP_DIRECTORY" >> "$SETUP_PROVIDES"
