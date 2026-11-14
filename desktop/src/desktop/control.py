"""JSON control API backing the desktop control page."""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import config as cfgmod
from . import db, paths, processes

if TYPE_CHECKING:  # pragma: no cover
    from .app import DesktopApp


def _json_bytes(payload: Any, status: int = 200) -> tuple[int, dict[str, str], bytes]:
    return status, {"Content-Type": "application/json; charset=utf-8"}, \
        json.dumps(payload, ensure_ascii=False).encode("utf-8")


class ControlApi:
    def __init__(self, app: "DesktopApp") -> None:
        self.app = app

    def handle(self, method: str, path: str, query: dict[str, list[str]],
               body: bytes) -> tuple[int, dict[str, str], bytes]:
        name = path.rsplit("/", 1)[-1]
        try:
            if method == "GET" and name == "status":
                return _json_bytes(self._status())
            if method == "GET" and name == "config":
                return _json_bytes(cfgmod.redacted(self.app.cfg))
            if method == "POST" and name == "config":
                return _json_bytes(self._update_config(body))
            if method == "POST" and name == "scan":
                return _json_bytes(self._scan())
            if method == "POST" and name == "scan-folder":
                return _json_bytes(self._scan_folder(body))
            if method == "POST" and name == "cancel-scan":
                return _json_bytes(self._cancel_scan())
            if method == "POST" and name == "pick-folder":
                return _json_bytes(self._pick_folder(body))
            if method == "POST" and name == "network":
                return _json_bytes(self._network(body))
            if method == "POST" and name == "firewall":
                return _json_bytes(self._firewall(body))
            if method == "GET" and name == "items":
                limit = int((query.get("limit") or ["200"])[0])
                lib = self.app.librarian
                rows = lib.journal.recent(min(max(limit, 1), 1000)) if lib else []
                return _json_bytes({"items": rows})
            if method == "GET" and name == "logs":
                return _json_bytes({"librarian": self.app.librarian.logs(300) if self.app.librarian else [],
                                    "api": _tail("api.log", 120), "worker": _tail("worker.log", 120)})
            if method == "POST" and name == "open-folder":
                return _json_bytes(self._open_folder(body))
            if method == "POST" and name == "restart-services":
                return _json_bytes(self._restart_services())
            if method == "POST" and name == "quit":
                import threading
                threading.Thread(target=lambda: self.app.shutdown(quit_app=True), daemon=True).start()
                return _json_bytes({"ok": True})
            return _json_bytes({"error": "unknown endpoint"}, 404)
        except Exception as exc:
            return _json_bytes({"error": str(exc)}, 500)

    # ---- status -------------------------------------------------------
    def _status(self) -> dict[str, Any]:
        cfg = self.app.cfg
        ports = cfg["ports"]
        now = time.time()
        cached = getattr(self, "_svc_cache", None)
        if cached and now - cached[0] < 1.0:
            services = dict(cached[1])
        else:
            results: dict[str, bool] = {}

            def check(name: str, url: str) -> None:
                # Short timeout: during boot these are expected to fail, and
                # the page polls every 2s.
                results[name] = processes.http_ok(url, 0.5)

            checks = [
                ("api", f"http://127.0.0.1:{ports['api']}/health/live"),
                ("worker", f"http://127.0.0.1:{ports['worker_health']}/health/live"),
                ("titiler", f"http://127.0.0.1:{ports['titiler']}/healthz"),
            ]
            waiters = [threading.Thread(target=check, args=(n, u), daemon=True) for n, u in checks]
            for waiter in waiters:
                waiter.start()
            for waiter in waiters:
                waiter.join()
            services = {
                **results,
                "database": processes.port_free(int(ports["postgres"])) is False,
            }
            self._svc_cache = (now, services)
        librarian = self.app.librarian.status() if self.app.librarian else {
            "mode": "idle", "counts": {}, "last_scan": None, "last_result": None,
            "progress": {"done": 0, "total": 0}, "current": None,
            "config": cfg["library"],
        }
        revision = ""
        if services["api"] and self.app.boot.get("phase") == "ready":
            cached = getattr(self, "_revision_cache", None)
            if cached and time.time() - cached[1] < 60:
                revision = cached[0]
            else:
                try:
                    revision = db.current_revision(cfg)
                except Exception:
                    revision = ""
                self._revision_cache = (revision, time.time())
        return {
            "app": "geolens-desktop",
            "version": "1.0.0-geolens-1.29.2",
            "boot": self.app.boot,
            "services": services,
            "ports": ports,
            "urls": {"app": self.app.proxy_url, "api": f"{self.app.proxy_url}/api"},
            "admin": cfg["admin"],
            "database": {"revision": revision, **cfg["database"]},
            "paths": {
                "data": str(paths.data_dir()),
                "library": str(Path(cfg["library"]["library_dir"])),
                "storage": str(paths.storage_dir()),
                "logs": str(paths.logs_dir()),
            },
            "library": librarian,
            "network": self.app.network_status(),
        }

    # ---- actions ------------------------------------------------------
    def _update_config(self, body: bytes) -> dict[str, Any]:
        incoming = json.loads(body.decode("utf-8") or "{}")
        cfg = self.app.cfg
        library = incoming.get("library") or {}
        allowed = set(cfg["library"].keys())
        for key, value in library.items():
            if key in allowed:
                cfg["library"][key] = value
        if "library_dir" in library:
            target = Path(str(library["library_dir"]).strip().strip('"')).expanduser()
            if not str(target).strip():
                return {"error": "影像库目录不能为空"}
            target.mkdir(parents=True, exist_ok=True)
            cfg["library"]["library_dir"] = str(target.resolve())
        if "source_dirs" in library:
            cleaned: list[str] = []
            seen: set[str] = set()
            for raw in library["source_dirs"] or []:
                text = str(raw).strip().strip('"').strip("'")
                if not text:
                    continue
                text = os.path.expandvars(os.path.expanduser(text))
                key = text.rstrip("\\/").lower()
                if key in seen:
                    continue
                seen.add(key)
                cleaned.append(str(Path(text)))
            cfg["library"]["source_dirs"] = cleaned
        ui = incoming.get("ui") or {}
        for key, value in ui.items():
            if key in cfg["ui"]:
                cfg["ui"][key] = value
        cfgmod.save(cfg)
        return {"ok": True, "config": cfgmod.redacted(cfg)}

    def _scan(self) -> dict[str, Any]:
        if not self.app.librarian:
            return {"error": "服务尚未就绪，请稍候再试"}
        started = self.app.librarian.trigger("控制台手动扫描")
        return {"ok": True, "started": started}

    def _scan_folder(self, body: bytes) -> dict[str, Any]:
        if not self.app.librarian:
            return {"error": "服务尚未就绪，请稍候再试"}
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            payload = {}
        from .librarian import Librarian

        raw = payload.get("path") or ""
        if not str(raw).strip():
            return {"error": "请先选择或输入要扫描的文件夹"}
        candidates = [str(raw)]
        # "D:\a;D:\b" style input for a one-off multi-folder pass.
        for sep in ("\n", ";"):
            if sep in str(raw):
                candidates = [p for p in str(raw).split(sep) if p.strip()]
        cleaned = []
        for candidate in candidates:
            resolved = Librarian.clean_path(candidate)
            if resolved is None:
                return {"error": f"文件夹不存在或不可读：{candidate.strip()}"}
            cleaned.append(resolved)
        started = self.app.librarian.trigger("指定文件夹扫描", extra_dirs=cleaned)
        if not started:
            return {"ok": False, "error": "已有扫描正在进行，请稍后再试或先停止当前扫描"}
        return {"ok": True, "paths": [str(p) for p in cleaned]}

    def _cancel_scan(self) -> dict[str, Any]:
        if not self.app.librarian:
            return {"error": "服务尚未就绪"}
        return {"ok": self.app.librarian.cancel_scan()}

    def _pick_folder(self, body: bytes) -> dict[str, Any]:
        """Native folder picker for the browser fallback (no pywebview)."""
        import subprocess

        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            payload = {}
        title = str(payload.get("title") or "选择文件夹").replace("'", "''")
        script = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$owner = New-Object System.Windows.Forms.Form;"
            "$owner.TopMost = $true; $owner.ShowInTaskbar = $false;"
            "$owner.Opacity = 0; $owner.Width = 1; $owner.Height = 1;"
            "$owner.Show();"
            f"$d = New-Object System.Windows.Forms.FolderBrowserDialog; $d.Description = '{title}';"
            "$d.ShowNewFolderButton = $true;"
            "if ($d.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {"
            "[Console]::Out.Write($d.SelectedPath) };"
            "$owner.Close()"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", script],
            capture_output=True, text=True, timeout=300,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        path = (result.stdout or "").strip()
        if result.returncode != 0:
            return {"error": (result.stderr or "无法打开文件夹选择对话框").strip()[-800:]}
        return {"path": path} if path else {"path": "", "cancelled": True}

    def _network(self, body: bytes) -> dict[str, Any]:
        import threading

        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            payload = {}
        cfg = self.app.cfg
        network = cfg.setdefault("network", {"lan_access": False, "public_host": ""})
        if "lan_access" in payload:
            network["lan_access"] = bool(payload["lan_access"])
        if "public_host" in payload:
            network["public_host"] = str(payload["public_host"] or "").strip()
        cfgmod.save(cfg)
        # Rebind/restart after the response reaches the page.
        threading.Timer(0.6, self.app.apply_network).start()
        return {"ok": True, "restarting": True, "network": self.app.network_status()}

    def _firewall(self, body: bytes) -> dict[str, Any]:
        from .app import firewall_apply, firewall_apply_elevated

        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            payload = {}
        action = str(payload.get("action") or "add")
        if action == "elevate":
            return firewall_apply_elevated(int(self.app.cfg["ports"]["proxy"]))
        enable = action == "add"
        state = firewall_apply(enable, int(self.app.cfg["ports"]["proxy"]))
        self.app._firewall_cache = None
        return state

    def _open_folder(self, body: bytes) -> dict[str, Any]:
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            payload = {}
        kind = payload.get("kind", "library")
        if kind == "library":
            target = Path(self.app.cfg["library"]["library_dir"])
        elif kind == "data":
            target = paths.data_dir()
        elif kind == "logs":
            target = paths.logs_dir()
        else:
            return {"error": "unknown folder"}
        try:
            os.startfile(str(target))  # type: ignore[attr-defined]
            return {"ok": True, "path": str(target)}
        except Exception as exc:
            return {"error": str(exc)}

    def _restart_services(self) -> dict[str, Any]:
        # The API/worker read most settings at import time; re-spawning is
        # enough for the ones exposed in the control page.
        for name in ("worker", "api", "titiler"):
            child = self.app.children.pop(name, None)
            if child:
                child.stop()
        self.app.children.clear()
        try:
            self.app.boot_services()
            return {"ok": True}
        except Exception as exc:
            return {"error": str(exc)}


def _tail(name: str, lines: int) -> str:
    path = paths.logs_dir() / name
    try:
        data = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(data[-lines:])
    except OSError:
        return ""
