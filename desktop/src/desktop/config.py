"""Persisted desktop configuration with first-run secret generation."""
from __future__ import annotations

import base64
import ipaddress
import json
import os
import secrets
import socket
import string
import tempfile
from pathlib import Path
from typing import Any

from . import paths

APP_VERSION = "1.2.8"

DEFAULTS: dict[str, Any] = {
    "version": 1,
    "ports": {
        "proxy": 8080,
        "api": 8000,
        "titiler": 8002,
        "worker_health": 8003,
        "postgres": 54329,
    },
    "database": {
        "host": "127.0.0.1",
        "db": "geolens",
        "user": "geolens",
    },
    "server": {
        "api_host": "127.0.0.1",
        "auto_login": True,          # bundled build opens straight into admin
    },
    "ui": {
        "language": "zh",
        "mode": "browser",          # browser | webview
        "auto_open_main": False,
        "window_width": 1180,
        "window_height": 760,
    },
    "network": {
        "lan_access": False,
        "public_host": "",
    },
    "library": {
        "source_dirs": [],
        "library_dir": "",
        "single_folder": True,       # one fixed drop folder beside the launcher
        "mode": "copy",              # copy | move | link
        "auto_ingest": True,
        "scan_on_startup": False,    # avoid a heavy backlog scan before the window is usable
        "scan_interval_seconds": 120,
        "recursive": True,
        "skip_hidden": True,
        "skip_system_dirs": True,    # Windows/WinSxS/AppData/… during whole-drive scans
        "hash_mode": "quick",        # quick | full
        "convert_unsupported": True, # ENVI/JP2/VRT/… -> GeoTIFF
        "include_extensions": [
            ".tif", ".tiff", ".img", ".jp2", ".dat",
            ".bmp", ".grd", ".pix", ".ecw", ".sid",
            ".hdf", ".h4", ".hdf4", ".hdf5", ".nc",
            ".geojson", ".gpkg", ".shp", ".fgb", ".kml", ".kmz", ".parquet",
        ],
        "min_size_mb": 0,
        "max_size_mb": 4096,
        "retry_limit": 3,
    },
    "advanced": {
        "api_workers": 1,
        "log_level": "INFO",
        "gdal_cache_mb": 200,
    },
}


def _merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def _fern_key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


def _password() -> str:
    alphabet = string.ascii_letters + string.digits
    chunk = lambda: "".join(secrets.choice(alphabet) for _ in range(6))
    return f"GeoLens-{chunk()}-{chunk()}"


def load() -> dict[str, Any]:
    paths.ensure_layout()
    cfg: dict[str, Any] = {}
    path = paths.config_path()
    if path.is_file():
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cfg = {}
    cfg = _merge(DEFAULTS, cfg)

    changed = False
    cfg.setdefault("secrets", {})
    if not cfg["secrets"].get("jwt_secret"):
        cfg["secrets"]["jwt_secret"] = secrets.token_hex(48)
        changed = True
    if not cfg["secrets"].get("secret_encryption_key"):
        cfg["secrets"]["secret_encryption_key"] = _fern_key()
        changed = True
    if not cfg["secrets"].get("db_password"):
        cfg["secrets"]["db_password"] = secrets.token_urlsafe(24)
        changed = True
    cfg.setdefault("admin", {})
    if not cfg["admin"].get("username"):
        cfg["admin"]["username"] = "admin"
        changed = True
    if not cfg["admin"].get("password"):
        cfg["admin"]["password"] = _password()
        changed = True
    if not cfg["library"].get("library_dir"):
        cfg["library"]["library_dir"] = str(paths.library_default_dir())
        changed = True

    if changed or not path.is_file():
        save(cfg)
    return cfg


def save(cfg: dict[str, Any]) -> None:
    paths.ensure_layout()
    path = paths.config_path()
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(cfg, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def redacted(cfg: dict[str, Any]) -> dict[str, Any]:
    """Config safe for the control page (keeps the admin password visible)."""
    return json.loads(json.dumps(cfg))


def library_dir(cfg: dict[str, Any]) -> Path:
    return Path(cfg["library"]["library_dir"]).expanduser().resolve()


def _is_private(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.is_private and not addr.is_loopback


def lan_ip() -> str:
    """Best-effort primary LAN IPv4 address of this machine."""
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.settimeout(0.3)
        probe.connect(("8.8.8.8", 80))
        ip = probe.getsockname()[0]
        probe.close()
        if _is_private(ip):
            return ip
    except OSError:
        pass
    try:
        _, _, ips = socket.gethostbyname_ex(socket.gethostname())
    except OSError:
        return ""
    for ip in ips:
        if _is_private(ip):
            return ip
    return ""


def hostname() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return ""


def network_base(cfg: dict[str, Any]) -> str:
    """Origin the control page advertises, without a trailing slash."""
    port = int(cfg["ports"]["proxy"])
    if not cfg.get("network", {}).get("lan_access"):
        return f"http://127.0.0.1:{port}"
    raw = str(cfg.get("network", {}).get("public_host") or "").strip()
    if raw:
        scheme = "http"
        if "://" in raw:
            scheme, raw = raw.split("://", 1)
            scheme = scheme.lower() or "http"
        raw = raw.split("/", 1)[0].strip()
        host, _, tail = raw.partition(":")
        if host and tail.isdigit():
            return f"{scheme}://{host}:{tail}"
        if raw:
            return f"{scheme}://{raw}:{port}"
    ip = lan_ip()
    return f"http://{ip or '127.0.0.1'}:{port}"


def bind_host(cfg: dict[str, Any]) -> str:
    return "0.0.0.0" if cfg.get("network", {}).get("lan_access") else "127.0.0.1"


def trusted_hosts(cfg: dict[str, Any]) -> str:
    hosts = ["localhost", "127.0.0.1", "::1", "api", "testserver"]
    ip = lan_ip()
    name = hostname()
    for value in (ip, name):
        if value and value not in hosts:
            hosts.append(value)
    return ",".join(hosts)
