"""Desktop orchestrator: database, services, proxy and window."""
from __future__ import annotations

import atexit
import json
import os
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

from . import config as cfgmod
from . import db, paths, processes, proxy
from .librarian import Librarian
from .control import ControlApi

APP_TITLE = "GeoLens 本地版"
DROP_FOLDER_NAME = "影像导入"
DROP_NOTE = """把遥感影像直接拖进这个文件夹即可。

支持：.tif / .tiff（含 BigTIFF）、.img（ENVI）、.jp2、.vrt、.shp（需同名 .dbf/.shx/.prj）、
.gpkg、.geojson、.fgb、.kml/.kmz、.csv 等。
程序每 15 秒检查一次；新文件会被复制到 data\\library 并自动入库。
入库完成后可以删除这里的原文件（复制模式不动原文件，重复文件会自动跳过）。
"""


def _lower_current_thread_priority() -> None:
    """Keep the boot thread from starving the window's message pump."""
    try:
        import ctypes

        ctypes.windll.kernel32.SetThreadPriority(
            ctypes.windll.kernel32.GetCurrentThread(), -1  # BELOW_NORMAL
        )
    except Exception:
        pass


class DesktopApp:
    def __init__(self, owns_services: bool = True) -> None:
        paths.ensure_layout()
        self.cfg = cfgmod.load()
        self._apply_drop_folder()
        self.owns_services = owns_services
        self.boot: dict[str, Any] = {"phase": "starting", "message": "正在启动…", "ready": False, "error": None}
        self.started_at = time.time()
        self.children: dict[str, processes.Child] = {}
        self.proxy_server = None
        self.librarian: Librarian | None = None
        self._stop = threading.Event()
        self._shutdown_done = threading.Event()
        self._shutdown_lock = threading.Lock()
        self._shut_down = False
        self.main_window = None
        self.control_window = None
        self.js_api = None

    # ---- helpers ------------------------------------------------------
    def _apply_drop_folder(self) -> None:
        """Keep one fixed drop folder beside the launcher, per this build's UX.

        The folder is created on every start and becomes the ONLY scan source;
        the librarian watches it and ingests files dropped there. Set
        library.single_folder=false in config.json to manage multiple sources.
        """
        if not self.cfg["library"].get("single_folder", True):
            return
        drop = paths.base_dir() / DROP_FOLDER_NAME
        try:
            drop.mkdir(parents=True, exist_ok=True)
            note = drop / "说明.txt"
            if not note.is_file():
                note.write_text(DROP_NOTE, encoding="utf-8")
        except OSError:
            return
        lib = self.cfg["library"]
        changed = False
        if lib.get("source_dirs") != [str(drop)]:
            lib["source_dirs"] = [str(drop)]
            changed = True
        if int(lib.get("scan_interval_seconds", 120)) != 15:
            lib["scan_interval_seconds"] = 15
            changed = True
        if not lib.get("scan_on_startup"):
            lib["scan_on_startup"] = True
            changed = True
        if changed:
            cfgmod.save(self.cfg)

    @property
    def api_url(self) -> str:
        return f"http://127.0.0.1:{self.cfg['ports']['api']}"

    @property
    def proxy_url(self) -> str:
        return f"http://127.0.0.1:{self.cfg['ports']['proxy']}"

    def set_boot(self, phase: str, message: str, error: str | None = None) -> None:
        self.boot.update({"phase": phase, "message": message, "error": error,
                          "ready": phase == "ready"})
        try:
            with open(paths.logs_dir() / "desktop.log", "a", encoding="utf-8") as handle:
                handle.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {phase}: {message}\n")
        except OSError:
            pass

    def _existing_instance(self) -> bool:
        url = f"{self.proxy_url}/__desktop/api/status"
        try:
            with urllib.request.urlopen(url, timeout=1.5) as resp:
                return resp.status == 200
        except Exception:
            return False

    def _assign_ports(self) -> None:
        if self._existing_instance():
            return
        ports = self.cfg["ports"]
        # Only a fresh install may move ports: an existing cluster already has
        # its server port baked into pgdata/pg_hba + postmaster.pid.
        if not db.is_initialized():
            taken: set[int] = set()
            for key in ("postgres", "api", "titiler", "worker_health"):
                port = int(ports[key])
                if port in taken or not processes.port_free(port):
                    port = processes.pick_port(port + 1)
                    while port in taken:
                        port = processes.pick_port(port + 1)
                ports[key] = port
                taken.add(port)
            cfgmod.save(self.cfg)

    # ---- boot ---------------------------------------------------------
    def boot_services(self) -> None:
        try:
            _lower_current_thread_priority()
            self.set_boot("database", "准备数据库…")
            if not db.is_initialized():
                db.init_cluster(self.cfg, lambda m: self.set_boot("database", m))
            db.start(self.cfg, lambda m: self.set_boot("database", m))
            db.bootstrap(self.cfg, lambda m: self.set_boot("database", m))
            if db.schema_uptodate(self.cfg, cfgmod.APP_VERSION):
                self.set_boot("database", "数据库结构已是最新，跳过迁移")
            else:
                db.migrate(self.cfg, lambda m: self.set_boot("migrate", m))
                db.mark_schema(cfgmod.APP_VERSION)

            # Spawn all three services first, then wait concurrently: the old
            # serial waits added every service's startup time together.
            self.set_boot("services", "启动 API / 切片 / 任务队列…")
            self.children["api"] = processes.spawn("api", "api", self.cfg)
            self.children["titiler"] = processes.spawn("titiler", "titiler", self.cfg)
            self.children["worker"] = processes.spawn("worker", "worker", self.cfg)

            targets = {
                "api": (f"{self.api_url}/health/live", 180, "api.log"),
                "titiler": (f"http://127.0.0.1:{self.cfg['ports']['titiler']}/healthz", 120, "titiler.log"),
                "worker": (
                    f"http://127.0.0.1:{self.cfg['ports']['worker_health']}/health/live",
                    120, "worker.log",
                ),
            }
            results: dict[str, bool] = {}

            def wait_one(name: str) -> None:
                url, timeout, _log = targets[name]
                results[name] = processes.wait_http(url, timeout=timeout)

            waiters = [threading.Thread(target=wait_one, args=(name,), daemon=True) for name in targets]
            for waiter in waiters:
                waiter.start()
            for waiter in waiters:
                waiter.join()
            failed = [name for name, ok in results.items() if not ok]
            if failed:
                name = failed[0]
                _log = targets[name][2]
                raise RuntimeError(
                    f"{name} 启动超时，详见 data/logs/{_log}:\n" + _tail(_log)
                )

            self.librarian = Librarian(lambda: self.cfg, lambda: self.api_url)
            self.librarian.start()

            self.set_boot("ready", "全部服务已就绪")
        except Exception as exc:
            self.set_boot("error", f"启动失败：{exc}", error=str(exc))
            traceback.print_exc()

    def start_proxy(self) -> None:
        app = proxy.ProxyApp(
            api_port=self.cfg["ports"]["api"],
            static_dir=paths.frontend_dist_dir(),
            control_dir=paths.control_assets_dir(),
            control_api=ControlApi(self).handle,
            env_js='window.__ENV__ = { API_BASE_URL: "/api", TILE_BASE_URL: "/api", AUTO_LOGIN: "1" };\n',
        )
        self.proxy_server = proxy.make_server(
            app, self.cfg["ports"]["proxy"], host=cfgmod.bind_host(self.cfg)
        )
        thread = threading.Thread(target=self.proxy_server.serve_forever,
                                  name="geolens-proxy", daemon=True)
        thread.start()
        self.set_boot("proxy", "本地服务已启动")

    # ---- network ------------------------------------------------------
    def network_status(self) -> dict[str, Any]:
        cfg = self.cfg
        enabled = bool(cfg.get("network", {}).get("lan_access"))
        info = {
            "lan_access": enabled,
            "bind_host": cfgmod.bind_host(cfg),
            "port": int(cfg["ports"]["proxy"]),
            "lan_ip": cfgmod.lan_ip(),
            "url": cfgmod.network_base(cfg),
            "public_host": cfg.get("network", {}).get("public_host", ""),
        }
        if enabled:
            cached = getattr(self, "_firewall_cache", None)
            if cached and time.time() - cached[1] < 60:
                info["firewall"] = cached[0]
            else:
                state = firewall_rule_state(int(cfg["ports"]["proxy"]))
                self._firewall_cache = (state, time.time())
                info["firewall"] = state
        return info

    def apply_network(self) -> None:
        """Rebind the proxy and restart services after a network change."""
        self._firewall_cache = None
        try:
            self.set_boot("network", "正在应用局域网设置…")
            if self.proxy_server is not None:
                try:
                    self.proxy_server.shutdown()
                    self.proxy_server.server_close()
                except Exception:
                    pass
                self.proxy_server = None
            self.start_proxy()
            if self.cfg.get("network", {}).get("lan_access"):
                firewall_apply(True, int(self.cfg["ports"]["proxy"]))
            else:
                firewall_apply(False, int(self.cfg["ports"]["proxy"]))
            self.restart_services()
            self.set_boot("ready", "全部服务已就绪")
        except Exception as exc:
            self.set_boot("error", f"应用网络设置失败：{exc}", error=str(exc))

    def restart_services(self) -> None:
        if self.librarian:
            try:
                self.librarian.stop()
            except Exception:
                pass
            self.librarian = None
        for name in ("worker", "api", "titiler"):
            child = self.children.pop(name, None)
            if child:
                try:
                    child.stop()
                except Exception:
                    pass
        self.children.clear()
        self.boot_services()

    # ---- UI -----------------------------------------------------------
    def open_main(self) -> bool:
        """Switch the single window to the catalog view."""
        try:
            if self.control_window is not None:
                self.control_window.evaluate_js("switchView('catalog');")
                return True
        except Exception:
            pass
        webbrowser.open(f"{self.proxy_url}/")
        return True

    def create_control_window(self) -> None:
        import webview

        self.js_api = DesktopJsApi(self)
        self.control_window = webview.create_window(
            APP_TITLE, f"{self.proxy_url}/__desktop/",
            js_api=self.js_api,
            width=int(self.cfg["ui"].get("window_width", 1180)),
            height=int(self.cfg["ui"].get("window_height", 760)),
            min_size=(900, 620),
        )
        self.js_api.window = self.control_window

    # ---- shutdown -----------------------------------------------------
    def shutdown(self, quit_app: bool = False) -> None:
        if not self.owns_services:
            # UI-only process: ask the background service to stop, then exit.
            try:
                req = urllib.request.Request(
                    f"{self.proxy_url}/__desktop/api/quit", data=b"{}",
                    headers={"Content-Type": "application/json"}, method="POST",
                )
                urllib.request.urlopen(req, timeout=5).read()
            except Exception:
                pass
            os._exit(0)
        with self._shutdown_lock:
            if self._shut_down:
                return
            self._shut_down = True
        self.set_boot("stopping", "正在停止服务…")
        if self.librarian:
            try:
                self.librarian.stop()
            except Exception:
                pass
        for name in ("worker", "api", "titiler"):
            child = self.children.get(name)
            if child:
                try:
                    child.stop()
                except Exception:
                    pass
        db.stop()
        self.set_boot("stopped", "服务已停止")
        # Set last: the main thread waits on this before leaving the process.
        self._stop.set()
        self._shutdown_done.set()
        if quit_app:
            os._exit(0)

    def wait_for_shutdown(self, timeout: float = 60) -> None:
        self._shutdown_done.wait(timeout)


def _tail(name: str) -> str:
    path = paths.logs_dir() / name
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-30:])
    except OSError:
        return "(无日志)"


FIREWALL_RULE = "GeoLens Desktop (LAN 8080)"


def _netsh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["netsh", "advfirewall", "firewall", *args],
        capture_output=True, text=True, timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def firewall_rule_state(port: int) -> dict[str, Any]:
    result = _netsh("show", "rule", f"name={FIREWALL_RULE}")
    present = result.returncode == 0 and str(port) in (result.stdout or "")
    return {
        "ok": present,
        "message": "已添加防火墙入站规则" if present else "尚未添加（局域网设备可能无法连接）",
        "manual": (
            f'netsh advfirewall firewall add rule name="{FIREWALL_RULE}" '
            f"dir=in action=allow protocol=TCP localport={port}"
        ),
    }


def firewall_apply(enabled: bool, port: int) -> dict[str, Any]:
    _netsh("delete", "rule", f"name={FIREWALL_RULE}")
    if not enabled:
        return {"ok": True, "message": "已移除防火墙规则（如存在）"}
    result = _netsh(
        "add", "rule", f"name={FIREWALL_RULE}", "dir=in", "action=allow",
        "protocol=TCP", f"localport={port}",
    )
    state = firewall_rule_state(port)
    if result.returncode != 0 and not state["ok"]:
        state["ok"] = False
        state["message"] = (
            "添加防火墙规则失败（通常需要管理员权限）。请以管理员身份运行："
            + state["manual"]
        )
    return state


def firewall_apply_elevated(port: int) -> dict[str, Any]:
    """Ask UAC to add the inbound rule (the app itself runs unprivileged)."""
    args = [
        "advfirewall", "firewall", "add", "rule",
        f'name="{FIREWALL_RULE}"', "dir=in", "action=allow",
        "protocol=TCP", f"localport={port}",
    ]
    ps_args = ",".join("'" + a.replace("'", "''") + "'" for a in args)
    script = (
        f"$argList = @({ps_args});"
        "Start-Process -FilePath netsh.exe -ArgumentList $argList -Verb RunAs"
    )
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-Command", script],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        return {"ok": False, "message": f"无法发起管理员授权：{exc}"}
    return {
        "ok": True,
        "message": "已请求管理员权限，请在 UAC 弹窗中点“是”；随后点“添加防火墙规则”可复查状态。",
    }


def _powershell_pick_folder(title: str = "选择文件夹") -> str:
    """Windows folder picker for the browser fallback."""
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
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()


class DesktopJsApi:
    """Bridge exposed to the control page as ``window.pywebview.api``."""

    def __init__(self, app: DesktopApp) -> None:
        self._app = app
        self.window = None

    def choose_directory(self, title: str = "选择文件夹") -> str:
        """Native folder picker.

        Prefer pywebview's own dialog (runs on the GUI thread and owns the
        window); fall back to a Windows FolderBrowserDialog for browser use.
        Failures are returned as an empty string and surfaced by the page.
        """
        try:
            import webview

            window = self.window
            if window is None and webview.windows:
                window = webview.windows[0]
            if window is not None:
                result = window.create_file_dialog(
                    webview.FOLDER_DIALOG, allow_multiple=False,
                    directory=self._app.cfg["library"].get("library_dir", ""),
                )
                if result:
                    return result[0] if isinstance(result, (list, tuple)) else str(result)
                return ""
        except Exception:
            pass
        try:
            selected = _powershell_pick_folder(title)
            return selected
        except Exception:
            return ""

    def open_main_window(self) -> bool:
        return self._app.open_main()

    def open_folder(self, kind: str = "library") -> bool:
        cfg = self._app.cfg
        target = Path(cfg["library"]["library_dir"]) if kind == "library" else paths.data_dir()
        try:
            os.startfile(str(target))  # type: ignore[attr-defined]
            return True
        except Exception:
            return False

    def quit_app(self) -> bool:
        threading.Thread(target=lambda: self._app.shutdown(quit_app=True), daemon=True).start()
        return True


def _message_box(text: str, title: str = APP_TITLE) -> None:
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, text, title, 0x10)
    except Exception:
        print(text)


def service_main() -> None:
    """Background service host: proxy + database + API/worker/TiTiler."""
    app = DesktopApp(owns_services=True)
    atexit.register(lambda: app.shutdown(quit_app=False))

    if app._existing_instance():
        print("GeoLens service already running.")
        return

    app._assign_ports()
    try:
        app.start_proxy()
    except OSError as exc:
        app.set_boot("error", f"端口 {app.cfg['ports']['proxy']} 被占用，无法启动：{exc}", error=str(exc))
        return

    threading.Thread(target=app.boot_services, name="geolens-boot", daemon=True).start()
    try:
        while not app._stop.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        app.shutdown()
        app.wait_for_shutdown()


def _spawn_service() -> "subprocess.Popen[bytes]":
    if getattr(sys, "frozen", False):
        args = [sys.executable, "--service"]
    else:
        args = [sys.executable, str(Path(__file__).resolve().parents[1] / "run_desktop.py"), "--service"]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if sys.platform == "win32":
        # Detach so closing the launcher (or a browser UI) never stops the
        # background service.
        flags |= 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(
        args, cwd=str(paths.base_dir()), creationflags=flags,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        close_fds=True,
    )


def _wait_proxy(proc: "subprocess.Popen[bytes] | None", timeout: float = 90.0) -> str | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc is not None and proc.poll() is not None:
            return None
        cfg = cfgmod.load()
        port = int(cfg["ports"]["proxy"])
        url = f"http://127.0.0.1:{port}"
        try:
            with urllib.request.urlopen(f"{url}/__desktop/api/status", timeout=1.0) as resp:
                if resp.status == 200:
                    return url
        except Exception:
            pass
        time.sleep(0.3)
    return None


def _run_webview_ui(url: str) -> None:
    app = DesktopApp(owns_services=False)
    atexit.register(lambda: app.shutdown(quit_app=False))
    try:
        import webview
    except Exception:
        webbrowser.open(f"{url}/__desktop/")
        return
    app.create_control_window()
    webview.start(debug=False)
    app.shutdown(quit_app=False)


def _open_ui(url: str, *, boot: bool = False) -> None:
    """Open the app in a standalone Edge/Chrome app window.

    ``boot=True`` starts on the progress screen, which redirects to the
    catalog once every service reports ready; the catalog is the product and
    the control panel is reachable from the shell bar inside it. A browser app
    window is a separate process, so WebView2/pythonnet problems in the UI can
    never freeze the background service.
    """
    page = f"{url}/__boot/" if boot else f"{url}/"
    if sys.platform == "win32":
        roots = [
            os.environ.get("ProgramFiles(x86)"),
            os.environ.get("ProgramFiles"),
            os.environ.get("LocalAppData"),
        ]
        candidates = []
        for root in roots:
            if not root:
                continue
            base = Path(root)
            candidates.append(base / "Microsoft" / "Edge" / "Application" / "msedge.exe")
            candidates.append(base / "Google" / "Chrome" / "Application" / "chrome.exe")
        for exe in candidates:
            if exe.is_file():
                try:
                    subprocess.Popen(
                        [str(exe), f"--app={page}", "--window-size=1280,820"],
                        close_fds=True,
                    )
                    return
                except OSError:
                    continue
    webbrowser.open(page)


def launcher_main() -> None:
    app = DesktopApp()
    mode = str(app.cfg["ui"].get("mode", "browser")).lower()
    if app._existing_instance():
        if mode == "webview":
            _run_webview_ui(app.proxy_url)
        else:
            _open_ui(app.proxy_url)
        return

    proc = _spawn_service()
    url = _wait_proxy(proc)
    if url is None:
        _message_box(
            "GeoLens 后台服务启动失败。\n\n请查看：\n"
            f"{paths.logs_dir() / 'desktop.log'}"
        )
        return

    cfg = cfgmod.load()
    if str(cfg["ui"].get("mode", "browser")).lower() == "webview":
        _run_webview_ui(url)
    else:
        # Show the progress screen immediately; it lands on the catalog when
        # every service is ready, so one double-click is the whole experience.
        _open_ui(url, boot=True)


def main() -> None:
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    if os.environ.get("GEOLENS_DESKTOP_HEADLESS") == "1" or "--service" in sys.argv[1:]:
        service_main()
        return
    launcher_main()


if __name__ == "__main__":
    main()
