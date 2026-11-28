#!/usr/bin/env bash
set -euo pipefail
python3 - <<'PY'
import importlib.util, json, os, statistics, subprocess, tempfile, time
from pathlib import Path

spec = importlib.util.spec_from_file_location("recording", Path("actions/recording/recording.py"))
assert spec and spec.loader
rec = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rec)

src = rec.user_cache_source(dict(os.environ))
print(f"XDG_CACHE_HOME={os.environ.get('XDG_CACHE_HOME', '')!r} HOME={os.environ.get('HOME', '')!r} source={src}")
if src and src.is_dir():
    entries = sorted(p.name for p in src.iterdir())
    total_bytes = sum(p.stat().st_size for p in src.rglob("*") if p.is_file() and not p.is_symlink())
    print(f"source entries={entries} total_bytes={total_bytes}")
else:
    print("source does not exist")

RUNS = 7
with tempfile.TemporaryDirectory(prefix="bench-222-") as tmp:
    base = Path(tmp)
    out = base / "recording"
    out.mkdir()
    (out / "environment.json").write_text(json.dumps(dict(os.environ)), encoding="utf-8")
    t0 = time.perf_counter()
    rec.snapshot_user_cache(out)
    snap_ms = (time.perf_counter() - t0) * 1000
    print(f"snapshot_user_cache_ms={snap_ms:.2f}")

    prep_times = []
    for i in range(RUNS):
        work = base / f"work-{i}"
        work.mkdir()
        t0 = time.perf_counter()
        rec.prepare_user_cache(out, work, dict(os.environ))
        prep_times.append((time.perf_counter() - t0) * 1000)
        rec.remove_work(work)
    print(f"prepare_user_cache_ms={[round(x, 2) for x in prep_times]} median={statistics.median(prep_times):.2f}")

    pwsh_cmd = ["pwsh", "-NoProfile", "-Command", "Get-Command Get-Nothing -ErrorAction SilentlyContinue"]
    empty_times = []
    for i in range(RUNS):
        empty_cache = base / f"pwsh-empty-{i}"
        empty_cache.mkdir()
        env = {**os.environ, "XDG_CACHE_HOME": str(empty_cache)}
        t0 = time.perf_counter()
        subprocess.run(pwsh_cmd, env=env, check=True)
        empty_times.append((time.perf_counter() - t0) * 1000)
    print(f"pwsh_empty_cache_ms={[round(x, 2) for x in empty_times]} median={statistics.median(empty_times):.2f}")

    filled_cache = base / "pwsh-filled"
    filled_cache.mkdir()
    fill_env = {**os.environ, "XDG_CACHE_HOME": str(filled_cache)}
    subprocess.run(
        ["pwsh", "-NoProfile", "-Command", "Get-Command Get-Nothing -ErrorAction SilentlyContinue; Start-Sleep -Seconds 18"],
        env=fill_env,
        check=True,
    )
    cache_files = list((filled_cache / "powershell").glob("ModuleAnalysisCache*"))
    print(f"filled_cache_files={[(p.name, p.stat().st_size) for p in cache_files]}")
    assert cache_files, "ModuleAnalysisCache was not written"

    filled_times = []
    for i in range(RUNS):
        t0 = time.perf_counter()
        subprocess.run(pwsh_cmd, env=fill_env, check=True)
        filled_times.append((time.perf_counter() - t0) * 1000)
    print(f"pwsh_filled_cache_ms={[round(x, 2) for x in filled_times]} median={statistics.median(filled_times):.2f}")
PY
