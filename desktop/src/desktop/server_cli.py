"""Command-line backend: database + API + worker + TiTiler, no UI/proxy.

Usage (separated deployment, frontend proxied by web_server.py):
    geolens_server.py start --port 8000
    geolens_web.py --port 8081

The frontend is a separate process. When it runs in ``--direct`` mode (static
site on another origin) pass that page's origin with ``--frontend-origin`` so
CORS admits it; in normal proxy mode no CORS entry is needed.
"""
from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from desktop import config as cfgmod
from desktop import paths
from desktop.app import DesktopApp


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def start(args: argparse.Namespace) -> None:
    app = DesktopApp(owns_services=True)
    cfg = app.cfg
    cfg["ports"]["api"] = int(args.port)

    public_host = args.public_host
    if not public_host:
        public_host = "127.0.0.1" if args.host in ("127.0.0.1", "localhost", "::1") else (cfgmod.lan_ip() or "127.0.0.1")
    api_base = f"http://{public_host}:{args.port}"
    web_port = int(args.web_port or 0)
    app_url = args.frontend_origin or f"http://{public_host}:{web_port or 8081}"
    cfg["server"] = {
        "api_host": args.host,
        "auto_login": bool(cfg.get("server", {}).get("auto_login", True)),
        "public_api_url": api_base,
        "public_app_url": app_url,
        "cors_origins": args.frontend_origin,
    }
    if args.source:
        cfg["library"]["source_dirs"] = [str(Path(p)) for p in args.source]
    if args.library:
        cfg["library"]["library_dir"] = str(Path(args.library))
    if args.scan_on_startup:
        cfg["library"]["scan_on_startup"] = True

    # A second instance next to a running desktop/backend must not collide on
    # PostgreSQL, TiTiler or the worker health port; only a fresh data dir may
    # move them, an existing cluster keeps its baked-in ports.
    app._assign_ports()
    cfg["ports"]["api"] = int(args.port)
    cfgmod.save(cfg)

    if not _port_free(args.host, args.port):
        print(f"API 端口被占用：{args.host}:{args.port}", file=sys.stderr)
        raise SystemExit(2)
    if web_port and not _port_free(args.web_host, web_port):
        print(f"网页端口被占用：{args.web_host}:{web_port}", file=sys.stderr)
        raise SystemExit(2)

    print("GeoLens 启动中…")
    print(f"  API   : {api_base}")
    if args.frontend_origin:
        print(f"  前端  : {args.frontend_origin}")
    if web_port:
        local_web = "127.0.0.1" if args.web_host in ("0.0.0.0", "::") else args.web_host
        print(f"  网页  : http://{local_web}:{web_port}")
        if args.web_host not in ("127.0.0.1", "localhost", "::1", "::"):
            print(f"  局域网: http://{public_host}:{web_port}")
    print(f"  账号  : {cfg['admin']['username']} / {cfg['admin']['password']}")
    print("  按 Ctrl+C 停止")
    app.boot_services()

    if app.boot.get("phase") != "ready":
        print(f"启动失败：{app.boot.get('error') or app.boot.get('message')}", file=sys.stderr)
        app.shutdown(quit_app=False)
        raise SystemExit(1)

    web = None
    if web_port:
        from . import web_server as webmod

        web = webmod.build_server(args.web_host, web_port, f"http://127.0.0.1:{args.port}")
        threading.Thread(target=web.serve_forever, name="geolens-web", daemon=True).start()
        print(f"网页已就绪：http://{local_web}:{web_port}")
        if args.open_browser:
            try:
                webbrowser.open(f"http://{local_web}:{web_port}")
            except Exception:
                pass

    try:
        while not app._stop.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        if web is not None:
            web.shutdown()
            web.server_close()
        app.shutdown()
        app.wait_for_shutdown()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="GeoLens 后端（命令行）")
    sub = parser.add_subparsers(dest="command", required=True)
    p_start = sub.add_parser("start", help="启动后端服务")
    p_start.add_argument("--host", default="127.0.0.1", help="API 绑定地址（局域网用 0.0.0.0）")
    p_start.add_argument("--port", type=int, default=8000, help="API 端口")
    p_start.add_argument("--frontend-origin", default="",
                         help="前端网页地址（仅 direct 跨域模式需要，用于 CORS 与分享链接）")
    p_start.add_argument("--public-host", default="", help="对外主机名/IP（留空自动）")
    p_start.add_argument("--source", action="append", default=[], help="本地影像源目录（可重复）")
    p_start.add_argument("--library", default="", help="影像库目录")
    p_start.add_argument("--scan-on-startup", action="store_true", help="启动后立即扫描本地影像")
    p_start.add_argument("--web-port", type=int, default=0,
                         help="同时启动网页服务并使用该端口（0=不启动）")
    p_start.add_argument("--web-host", default="127.0.0.1",
                         help="网页绑定地址（局域网用 0.0.0.0）")
    p_start.add_argument("--open-browser", action="store_true",
                         help="网页就绪后自动打开浏览器")
    args = parser.parse_args(argv)

    if args.command == "start":
        start(args)


if __name__ == "__main__":
    main()
