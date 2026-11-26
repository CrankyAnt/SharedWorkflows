"""The setup ``dotnet``: gives the entry files the .NET SDK that the job installed."""

import os
from pathlib import Path

SETUP_DIRECTORIES = (
    ("DOTNET_CLI_HOME", "dotnet-home"),
    ("NUGET_PACKAGES", "nuget/packages"),
    ("NUGET_HTTP_CACHE_PATH", "nuget/http-cache"),
    ("NUGET_PLUGINS_CACHE_PATH", "nuget/plugins-cache"),
)

root = os.environ["SETUP_TOOL"]
setup_root = Path(os.environ["SETUP_DIRECTORY"])
with open(os.environ["SETUP_PROVIDES"], "a", encoding="utf-8") as provides:
    provides.write(f"path={root}\n")
    provides.write(f"variable=DOTNET_ROOT={root}\n")
    for variable, subdir in SETUP_DIRECTORIES:
        directory = setup_root / subdir
        directory.mkdir(parents=True, exist_ok=True)
        provides.write(f"variable={variable}={directory}\n")
        provides.write(f"write={directory}\n")
