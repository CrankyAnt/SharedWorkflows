"""The setup ``dotnet``: gives the entry files the .NET SDK that the job installed."""

import os

root = os.environ["SETUP_TOOL"]
with open(os.environ["SETUP_PROVIDES"], "a", encoding="utf-8") as provides:
    provides.write(f"path={root}\n")
    provides.write(f"variable=DOTNET_ROOT={root}\n")
