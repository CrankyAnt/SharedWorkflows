"""The setup ``node``: gives the entry files the Node.js that the job installed."""

import os

with open(os.environ["SETUP_PROVIDES"], "a", encoding="utf-8") as provides:
    provides.write(f"path={os.environ['SETUP_TOOL']}\n")
