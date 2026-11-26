"""The setup ``dotnet``: gives the entry files the .NET SDK that the job installed."""

import os
from pathlib import Path

NUGET_DIRECTORIES = (
    ("NUGET_PACKAGES", "packages"),
    ("NUGET_HTTP_CACHE_PATH", "http-cache"),
    ("NUGET_PLUGINS_CACHE_PATH", "plugins-cache"),
)

root = os.environ["SETUP_TOOL"]
nuget_root = Path(os.environ["SETUP_DIRECTORY"]) / "nuget"
with open(os.environ["SETUP_PROVIDES"], "a", encoding="utf-8") as provides:
    provides.write(f"path={root}\n")
    provides.write(f"variable=DOTNET_ROOT={root}\n")
    for variable, subdir in NUGET_DIRECTORIES:
        directory = nuget_root / subdir
        directory.mkdir(parents=True, exist_ok=True)
        provides.write(f"variable={variable}={directory}\n")
        provides.write(f"write={directory}\n")
