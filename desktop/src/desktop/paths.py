"""Install-layout paths for the bundled desktop app.

Works both from source (``geolens-desktop/src/desktop``) and from the frozen
PyInstaller launcher, where the payload sits next to ``GeoLens.exe``.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

DESKTOP_DIR_NAME = "app"


def _candidate_bases() -> list[Path]:
    env = os.environ.get("GEOLENS_DESKTOP_HOME")
    if env:
        return [Path(env).resolve()]
    if getattr(sys, "frozen", False):
        return [Path(sys.executable).resolve().parent]
    # src/desktop/paths.py -> src/desktop -> src -> geolens-desktop
    return [Path(__file__).resolve().parents[2]]


def base_dir() -> Path:
    for candidate in _candidate_bases():
        if (candidate / "runtime").is_dir() or (candidate / "cache").is_dir():
            return candidate
    return _candidate_bases()[0]


def app_dir() -> Path:
    """Directory holding backend/, frontend_dist/, desktop/."""
    return base_dir() / DESKTOP_DIR_NAME


def backend_dir() -> Path:
    """Backend source: bundled app/backend, the repo's app/backend, then a
    sibling geolens/backend checkout."""
    candidates = (
        app_dir() / "backend",
        base_dir().parent / "app" / "backend",
        base_dir().parent / "geolens" / "backend",
    )
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[-1]


def frontend_dist_dir() -> Path:
    """Built SPA: bundled app/frontend_dist, or app/frontend/dist in a repo."""
    candidates = (
        app_dir() / "frontend_dist",
        base_dir().parent / "app" / "frontend" / "dist",
        base_dir().parent / "geolens" / "frontend" / "dist",
    )
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate
    return candidates[-1]


def runtime_dir() -> Path:
    return base_dir() / "runtime"


def python_exe() -> Path:
    candidate = runtime_dir() / "py" / "python.exe"
    if candidate.is_file():
        return candidate
    return runtime_dir() / "py" / "bin" / "python"


def pgsql_dir() -> Path:
    return runtime_dir() / "pgsql"


def data_dir() -> Path:
    env = os.environ.get("GEOLENS_DESKTOP_DATA")
    if env:
        return Path(env).resolve()
    return base_dir() / "data"


def pgdata_dir() -> Path:
    return data_dir() / "pgdata"


def storage_dir() -> Path:
    return data_dir() / "storage"


def logs_dir() -> Path:
    return data_dir() / "logs"


def library_default_dir() -> Path:
    return data_dir() / "library"


def config_path() -> Path:
    return data_dir() / "config.json"


def journal_path() -> Path:
    return data_dir() / "library-index.sqlite3"


def control_assets_dir() -> Path:
    frozen = app_dir() / "control"
    if frozen.is_dir():
        return frozen
    return Path(__file__).resolve().parent / "control"


def server_entry() -> Path:
    """The Python entry script children are launched with."""
    frozen = app_dir() / "server_entry.py"
    if frozen.is_file():
        return frozen
    return Path(__file__).resolve().parents[1] / "server_entry.py"


def ensure_layout() -> None:
    for path in (data_dir(), storage_dir(), logs_dir(), library_default_dir()):
        path.mkdir(parents=True, exist_ok=True)
