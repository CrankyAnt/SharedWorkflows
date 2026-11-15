"""The setup ``python``: gives the entry files the Python that the job installed, with the lookup report in it."""

import os
import sys
from pathlib import Path

interpreter = Path(os.environ["SETUP_TOOL"])
directories = [interpreter.parent, *([interpreter.parent / "Scripts"] if sys.platform == "win32" else [])]
with open(os.environ["SETUP_PROVIDES"], "a", encoding="utf-8") as provides:
    for directory in directories:
        provides.write(f"path={directory}\n")
    provides.write(f"python={interpreter}\n")
