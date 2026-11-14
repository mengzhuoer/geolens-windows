"""Child-process supervision for API, worker and TiTiler."""
from __future__ import annotations

import os
import socket
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any

from . import config as cfgmod
from . import paths

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000


def port_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def pick_port(preferred: int, host: str = "127.0.0.1") -> int:
    for port in range(preferred, preferred + 40):
        if port_free(port, host):
            return port
    raise RuntimeError(f"no free port near {preferred}")


def build_env(cfg: dict[str, Any]) -> dict[str, str]:
    env = os.environ.copy()
    root = paths.base_dir()
    py_lib = root / "runtime" / "py" / "Library" / "bin"
    pg_bin = root / "runtime" / "pgsql" / "bin"
    env["PATH"] = ";".join([str(py_lib), str(pg_bin), env.get("PATH", "")])
    env["GDAL_DATA"] = str(root / "runtime" / "py" / "Library" / "share" / "gdal")
    env["PROJ_DATA"] = str(root / "runtime" / "py" / "Library" / "share" / "proj")
    env["PROJ_LIB"] = env["PROJ_DATA"]
    env["PYTHONPATH"] = str(paths.backend_dir())
    env["PYTHONIOENCODING"] = "utf-8"
    # The bundled app has no login screen: the web UI asks the API for an
    # admin session on load. server.auto_login=false restores real auth.
    env["GEOLENS_AUTO_LOGIN"] = "1" if cfg.get("server", {}).get("auto_login", True) else "0"
    # Children exit when the launcher process disappears (crash, Task Manager).
    env["GEOLENS_LAUNCHER_PID"] = str(os.getpid())
    env["GEOLENS_PGCTL"] = str(root / "runtime" / "pgsql" / "bin" / "pg_ctl.exe")
    env["GEOLENS_PGDATA"] = str(paths.pgdata_dir())
    # conda-forge ships OGR drivers as plugins; GDAL only searches its own
    # default plugin dir, so point it at the runtime explicitly.
    env["GDAL_DRIVER_PATH"] = str(root / "runtime" / "py" / "Library" / "lib" / "gdalplugins")

    db = cfg["database"]
    env["POSTGRES_HOST"] = db["host"]
    env["POSTGRES_PORT"] = str(cfg["ports"]["postgres"])
    env["POSTGRES_DB"] = db["db"]
    env["POSTGRES_USER"] = db["user"]
    env["POSTGRES_PASSWORD"] = cfg["secrets"]["db_password"]
    env["JWT_SECRET_KEY"] = cfg["secrets"]["jwt_secret"]
    env["SECRET_ENCRYPTION_KEY"] = cfg["secrets"]["secret_encryption_key"]
    env["GEOLENS_ADMIN_USERNAME"] = cfg["admin"]["username"]
    env["GEOLENS_ADMIN_PASSWORD"] = cfg["admin"]["password"]

    env["STORAGE_PROVIDER"] = "local"
    env["UPLOAD_STAGING_DIR"] = str(paths.storage_dir())
    env["UPLOAD_MAX_SIZE_MB"] = str(cfg["library"]["max_size_mb"])
    env["GEOLENS_API_RUN_MIGRATIONS"] = "false"
    env["ENVIRONMENT"] = "development"
    env["GEOLENS_HOST"] = "127.0.0.1"
    server = cfg.get("server", {})
    env["GEOLENS_API_HOST"] = str(server.get("api_host", "127.0.0.1"))
    env["GEOLENS_API_PORT"] = str(cfg["ports"]["api"])
    env["GEOLENS_TITILER_PORT"] = str(cfg["ports"]["titiler"])
    env["GEOLENS_WORKER_HEALTH_PORT"] = str(cfg["ports"]["worker_health"])
    env["TITILER_BASE_URL"] = f"http://127.0.0.1:{cfg['ports']['titiler']}"
    env["REMOTE_RASTER_RELAY_BASE_URL"] = f"http://127.0.0.1:{cfg['ports']['api']}"
    env["PUBLIC_APP_URL"] = server.get("public_app_url") or cfgmod.network_base(cfg)
    env["PUBLIC_API_URL"] = server.get("public_api_url") or (cfgmod.network_base(cfg) + "/api")
    env["TENANT_TRUSTED_HOSTS"] = cfgmod.trusted_hosts(cfg)
    if server.get("cors_origins"):
        env["CORS_ALLOWED_ORIGINS"] = str(server["cors_origins"])
    env["LOG_LEVEL"] = str(cfg["advanced"].get("log_level", "INFO"))
    env["LOG_JSON"] = "false"
    env["GDAL_CACHEMAX"] = str(cfg["advanced"].get("gdal_cache_mb", 200))
    env["PROCRASTINATE_SCHEMA"] = "catalog"
    return env


class Child:
    def __init__(self, name: str, args: list[str], env: dict[str, str], cwd: Path,
                 logfile: Path, creationflags: int = CREATE_NO_WINDOW) -> None:
        self.name = name
        self.logfile = logfile
        logfile.parent.mkdir(parents=True, exist_ok=True)
        try:
            if logfile.stat().st_size > 10 * 1024 * 1024:
                logfile.write_text("", encoding="utf-8")
        except OSError:
            pass
        self._handle = open(logfile, "ab")
        self.proc = subprocess.Popen(
            args, cwd=str(cwd), env=env,
            stdout=self._handle, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
        )

    def alive(self) -> bool:
        return self.proc.poll() is None

    def stop(self, timeout: float = 8.0) -> None:
        if self.proc.poll() is None:
            taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
            try:
                subprocess.run(
                    [str(taskkill), "/PID", str(self.proc.pid), "/T", "/F"],
                    capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=15,
                )
            except Exception:
                pass
            deadline = time.time() + timeout
            while time.time() < deadline and self.proc.poll() is None:
                time.sleep(0.2)
            if self.proc.poll() is None:
                try:
                    self.proc.terminate()
                    self.proc.wait(timeout=3)
                except Exception:
                    try:
                        self.proc.kill()
                    except Exception:
                        pass
        try:
            self._handle.close()
        except Exception:
            pass


def spawn(name: str, role: str, cfg: dict[str, Any]) -> Child:
    args = [str(paths.python_exe()), str(paths.server_entry()), role]
    flags = CREATE_NO_WINDOW
    if role == "worker":
        # COG conversion and bulk ingests are the heaviest work; keep the
        # window, proxy and API responsive while they run.
        flags |= BELOW_NORMAL_PRIORITY_CLASS
    return Child(name, args, build_env(cfg), paths.backend_dir(),
                 paths.logs_dir() / f"{name}.log", creationflags=flags)


def http_ok(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return 200 <= resp.status < 500
    except Exception:
        return False


def wait_http(url: str, timeout: float = 90.0, interval: float = 0.5) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if http_ok(url):
            return True
        time.sleep(interval)
    return False
