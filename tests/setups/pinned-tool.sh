#!/usr/bin/env bash
# The setups of the pinned tools: installs the tool that the setup is named after, with ShellCheck beside actionlint.
set -euo pipefail
tools=(--tool "$SETUP_NAME")
if [[ "$SETUP_NAME" == actionlint ]]; then tools+=(--tool shellcheck); fi
python3 actions/repository-hygiene/install_tools.py --pins actions/repository-hygiene/pins.json --platform linux-x64 --destination "$SETUP_DIRECTORY" "${tools[@]}"
echo "path=$SETUP_DIRECTORY" >> "$SETUP_PROVIDES"
