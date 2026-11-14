"""Single-origin HTTP front: SPA static files + API reverse proxy.

Port 8080 in the desktop bundle mirrors what nginx does in the Docker stack:
* ``/api/*`` is forwarded to the FastAPI app with the prefix stripped;
* ``/raster-tiles/<id>/tiles/...`` is rewritten to ``/tiles/raster-proxy/<id>/...``;
* everything else is served from the built Vite bundle with an SPA fallback.

The desktop control page lives under ``/__desktop`` and is handled in-process.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import mimetypes
import re
import socketserver
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler as _BaseHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from .shell import BOOT_HTML, SHELL_JS, SHELL_PATH, SHELL_TAG

RASTER_TILES_RE = re.compile(
    r"^/raster-tiles/([0-9a-fA-F-]+)/tiles/(\d+)/(\d+)/(\d+)\.([a-zA-Z]+)$"
)

ControlHandler = Callable[[str, str, dict[str, list[str]], bytes], tuple[int, dict[str, str], bytes]]

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}


class ProxyApp:
    def __init__(self, *, api_port: int, static_dir: Path, control_dir: Path,
                 control_api: ControlHandler, env_js: str) -> None:
        self.api_port = api_port
        self.static_dir = static_dir
        self.control_dir = control_dir
        self.control_api = control_api
        self.env_js = env_js


class Handler(_BaseHandler):
    app: ProxyApp = None  # type: ignore[assignment]
    protocol_version = "HTTP/1.1"
    server_version = "GeoLensDesktop/1.0"
    sys_version = ""

    # ---- entry points -------------------------------------------------
    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_HEAD(self) -> None:
        self._dispatch("HEAD")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def do_OPTIONS(self) -> None:
        self._dispatch("OPTIONS")

    def log_message(self, fmt: str, *args: Any) -> None:
        # Keep the launcher log quiet; API access logs are in api.log.
        return

    # ---- routing ------------------------------------------------------
    def _is_local_client(self) -> bool:
        try:
            addr = self.client_address[0].split("%")[0]
            if addr.startswith("::ffff:"):
                addr = addr[7:]
            return ipaddress.ip_address(addr).is_loopback
        except (ValueError, IndexError):
            return False

    def _dispatch(self, method: str) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        try:
            if path.startswith("/__desktop") and not self._is_local_client():
                # The control panel exposes the admin password and destructive
                # actions; it must never be reachable from the LAN.
                self._error(403, "control panel is loopback-only")
                return
            if path.startswith("/__desktop/api/"):
                self._control_api(method, path, query)
            elif path in ("/__desktop", "/__desktop/"):
                self._static(self.app.control_dir, "index.html", method)
            elif path.startswith("/__desktop/"):
                self._static(self.app.control_dir, path[len("/__desktop/"):], method)
            elif path == "/env-config.js":
                self._bytes(200, {"Content-Type": "application/javascript; charset=utf-8",
                                  "Cache-Control": "no-store"}, self.app.env_js.encode(), method)
            elif path == SHELL_PATH:
                self._bytes(200, {"Content-Type": "application/javascript; charset=utf-8",
                                  "Cache-Control": "no-cache"}, SHELL_JS.encode(), method)
            elif path in ("/__boot", "/__boot/"):
                self._bytes(200, {"Content-Type": "text/html; charset=utf-8",
                                  "Cache-Control": "no-store"}, BOOT_HTML.encode(), method)
            elif path == "/api" or path.startswith("/api/"):
                upstream = (path[4:] or "/") if path.startswith("/api/") else "/"
                self._proxy(method, upstream + (("?" + parsed.query) if parsed.query else ""))
            elif RASTER_TILES_RE.match(path):
                match = RASTER_TILES_RE.match(path)
                assert match
                upstream = f"/tiles/raster-proxy/{match.group(1)}/{match.group(2)}/{match.group(3)}/{match.group(4)}.{match.group(5)}"
                if parsed.query:
                    upstream += "?" + parsed.query
                self._proxy(method, upstream)
            else:
                rel = path.lstrip("/") or "index.html"
                if rel.startswith("__desktop"):
                    self._error(404, "not found")
                    return
                self._static(self.app.static_dir, rel, method)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # last-resort: never take the process down
            try:
                self._error(500, f"desktop proxy error: {exc}")
            except Exception:
                pass

    # ---- response helpers ---------------------------------------------
    def _bytes(self, status: int, headers: dict[str, str], body: bytes,
               method: str = "GET") -> None:
        headers.setdefault("Content-Length", str(len(body)))
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        if method != "HEAD" and body:
            self.wfile.write(body)

    def _error(self, status: int, text: str) -> None:
        self._bytes(status, {"Content-Type": "text/plain; charset=utf-8"}, text.encode())

    def _control_api(self, method: str, path: str, query: dict[str, list[str]]) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        status, headers, payload = self.app.control_api(method, path, query, body)
        headers = dict(headers)
        headers.setdefault("Cache-Control", "no-store")
        self._bytes(status, headers, payload, method)

    def _static(self, root: Path, rel: str, method: str) -> None:
        rel = urllib.parse.unquote(rel)
        if rel.startswith("/"):
            rel = rel[1:]
        if ".." in Path(rel).parts:
            self._error(404, "not found")
            return
        base = root.resolve()
        candidate = (base / rel).resolve()
        if not candidate.is_relative_to(base):
            self._error(404, "not found")
            return
        if not candidate.is_file():
            candidate = base / "index.html"
        if not candidate.is_file():
            self._error(404, "not found")
            return
        body = candidate.read_bytes()
        # The bundled SPA gets the small shell bar (control panel / quit) so
        # the whole product feels like one app. Served for index.html only.
        if root == self.app.static_dir and candidate.name == "index.html":
            body = body.replace(b"</body>", SHELL_TAG + b"</body>", 1)
        ctype = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        if candidate.suffix.lower() in (".js", ".mjs"):
            ctype = "application/javascript"
        elif candidate.suffix.lower() == ".css":
            ctype = "text/css"
        headers = {"Content-Type": f"{ctype}; charset=utf-8" if ctype.startswith(("text/", "application/javascript", "application/json", "image/svg")) else ctype}
        if candidate.parent.name == "assets":
            headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            headers["Cache-Control"] = "no-cache"
        self._bytes(200, headers, body, method)

    # ---- upstream -----------------------------------------------------
    def _read_body(self, length: int):
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk

    def _proxy(self, method: str, upstream_path: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in HOP_BY_HOP and key.lower() != "host"
        }
        headers["Host"] = f"127.0.0.1:{self.app.api_port}"
        connection = http.client.HTTPConnection("127.0.0.1", self.app.api_port, timeout=900)
        try:
            if length:
                connection.request(method, upstream_path, body=self._read_body(length), headers=headers)
            else:
                connection.request(method, upstream_path, headers=headers)
            response = connection.getresponse()
            out_headers: dict[str, str] = {}
            for key, value in response.getheaders():
                if key.lower() in HOP_BY_HOP:
                    continue
                if key.lower() == "content-length":
                    continue
                out_headers[key] = value
            data = response.read()
            out_headers["Content-Length"] = str(len(data))
            self.send_response(response.status, response.reason)
            for key, value in out_headers.items():
                self.send_header(key, value)
            self.end_headers()
            if method != "HEAD" and data:
                self.wfile.write(data)
        finally:
            connection.close()


def make_server(app: ProxyApp, port: int, host: str = "127.0.0.1"):
    handler = type("GeoLensHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def start(app: ProxyApp, port: int):
    server = make_server(app, port)
    thread = threading.Thread(target=server.serve_forever, name="geolens-proxy", daemon=True)
    thread.start()
    return server
