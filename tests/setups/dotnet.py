"""The setup ``dotnet``: gives the entry files the .NET SDK that the job installed."""

import os
import subprocess
import sys
from pathlib import Path

SETUP_DIRECTORIES = (
    ("DOTNET_CLI_HOME", "dotnet-home"),
    ("NUGET_PACKAGES", "nuget/packages"),
    ("NUGET_HTTP_CACHE_PATH", "nuget/http-cache"),
    ("NUGET_PLUGINS_CACHE_PATH", "nuget/plugins-cache"),
)

root = os.environ["SETUP_TOOL"]
setup_root = Path(os.environ["SETUP_DIRECTORY"])
variables = {"DOTNET_ROOT": root}
with open(os.environ["SETUP_PROVIDES"], "a", encoding="utf-8") as provides:
    provides.write(f"path={root}\n")
    provides.write(f"variable=DOTNET_ROOT={root}\n")
    for variable, subdir in SETUP_DIRECTORIES:
        directory = setup_root / subdir
        directory.mkdir(parents=True, exist_ok=True)
        variables[variable] = str(directory)
        provides.write(f"variable={variable}={directory}\n")
        provides.write(f"write={directory}\n")

# Run NuGet's one-time user-profile migration sentinel during setup (before recording starts)
# so recorded entry files do not write AppData/Local/NuGet/Migrations/1.
dotnet = Path(root) / ("dotnet.exe" if sys.platform == "win32" else "dotnet")
if dotnet.is_file():
    subprocess.run([str(dotnet), "nuget", "locals", "all", "--list"], env={**os.environ, **variables},
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
