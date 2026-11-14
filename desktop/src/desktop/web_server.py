"""Static frontend server for the separated deployment.

Serves the Vite build on its own port. By default it also acts as the
single-origin front for the backend, exactly like nginx in the Docker stack:

* ``/api/*`` is forwarded to the FastAPI app with the prefix stripped;
* ``/raster-tiles/<id>/tiles/...`` is rewritten to ``/tiles/raster-proxy/<id>/...``;
* everything else is served from the build with an SPA fallback.

Keeping the browser on one origin means the httpOnly refresh cookie works, so
a signed-in session survives a page reload. ``--direct`` turns the proxy off
for split-origin deployments (frontend on one host, API on another); those
must then allow this origin through ``--frontend-origin`` on the backend.

Usage:
    geolens_web.py --port 8081 --api http://127.0.0.1:8000
"""
from __future__ import annotations

import argparse
import http.client
import json
import mimetypes
import re
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from desktop import paths

RASTER_TILES_RE = re.compile(
    r"^/raster-tiles/([0-9a-fA-F-]+)/tiles/(\d+)/(\d+)/(\d+)\.([a-zA-Z]+)$"
)

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}


class Handler(BaseHTTPRequestHandler):
    dist: Path
    api_upstream: str  # http://host:port, empty in direct mode
    env_js: str
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        return

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

    def _dispatch(self, method: str) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        try:
            if path == "/env-config.js":
                body = self.env_js.encode()
                self._bytes(method, 200, {"Content-Type": "application/javascript; charset=utf-8",
                                          "Cache-Control": "no-store"}, body)
                return
            if self.api_upstream:
                if path == "/api" or path.startswith("/api/"):
                    upstream = (path[4:] or "/") if path.startswith("/api/") else "/"
                    if parsed.query:
                        upstream += "?" + parsed.query
                    self._proxy(method, upstream)
                    return
                match = RASTER_TILES_RE.match(path)
                if match:
                    upstream = (
                        f"/tiles/raster-proxy/{match.group(1)}/{match.group(2)}"
                        f"/{match.group(3)}/{match.group(4)}.{match.group(5)}"
                    )
                    if parsed.query:
                        upstream += "?" + parsed.query
                    self._proxy(method, upstream)
                    return
            self._static(method, path)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # never take the server down for one request
            try:
                self._bytes(method, 500, {"Content-Type": "text/plain; charset=utf-8"},
                            f"geolens web error: {exc}".encode())
            except Exception:
                pass

    def _static(self, method: str, path: str) -> None:
        rel = urllib.parse.unquote(path).lstrip("/") or "index.html"
        base = self.dist.resolve()
        candidate = (base / rel).resolve()
        if not candidate.is_relative_to(base) or not candidate.is_file():
            candidate = base / "index.html"
        if not candidate.is_file():
            self._bytes(method, 404, {"Content-Type": "text/plain; charset=utf-8"}, b"not found")
            return
        ctype = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        if candidate.suffix == ".js":
            ctype = "application/javascript"
        elif candidate.suffix == ".css":
            ctype = "text/css"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        headers = {
            "Content-Type": ctype,
            "Content-Length": str(candidate.stat().st_size),
            "Cache-Control": "no-cache" if candidate.parent.name != "assets"
            else "public, max-age=31536000, immutable",
        }
        self.send_response(200)
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        if method != "HEAD":
            with candidate.open("rb") as handle:
                while True:
                    chunk = handle.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)

    def _bytes(self, method: str, status: int, headers: dict[str, str], body: bytes) -> None:
        headers.setdefault("Content-Length", str(len(body)))
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        if method != "HEAD" and body:
            self.wfile.write(body)

    def _read_body(self, length: int):
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk

    def _proxy(self, method: str, upstream_path: str) -> None:
        target = urllib.parse.urlsplit(self.api_upstream)
        host = target.hostname or "127.0.0.1"
        port = target.port or (443 if target.scheme == "https" else 80)
        length = int(self.headers.get("Content-Length") or 0)
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in HOP_BY_HOP and key.lower() != "host"
        }
        prefix = target.path.rstrip("/")
        if prefix:
            upstream_path = prefix + upstream_path
        headers["Host"] = target.netloc
        connection_cls = (
            http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
        )
        connection = connection_cls(host, port, timeout=900)
        try:
            if length:
                connection.request(method, upstream_path,
                                   body=self._read_body(length), headers=headers)
            else:
                connection.request(method, upstream_path, headers=headers)
            response = connection.getresponse()
            self.send_response(response.status, response.reason)
            for key, value in response.getheaders():
                if key.lower() in HOP_BY_HOP or key.lower() == "content-length":
                    continue
                self.send_header(key, value)
            content_length = response.getheader("Content-Length")
            if content_length is not None:
                self.send_header("Content-Length", content_length)
            elif method == "HEAD":
                self.send_header("Content-Length", "0")
            self.end_headers()
            if method == "HEAD":
                return
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
        finally:
            connection.close()


def build_server(host: str, port: int, api: str, *, direct: bool = False,
                 public_api: str = "", tile: str = "") -> ThreadingHTTPServer:
    """Create (but do not start) the web server; shared by the standalone CLI
    and the combined ``geolens_server.py --web-port`` mode."""
    dist = paths.frontend_dist_dir()
    if not (dist / "index.html").is_file():
        raise SystemExit(f"找不到前端构建产物：{dist}")

    if direct:
        api_base = (public_api or api).rstrip("/")
        tile_base = (tile or api_base).rstrip("/")
        upstream = ""
        env_js = "window.__ENV__ = " + json.dumps(
            {"API_BASE_URL": api_base, "TILE_BASE_URL": tile_base, "AUTO_LOGIN": "1"}
        ) + ";\n"
    else:
        upstream = api.rstrip("/")
        # Same-origin proxy: relative paths keep cookies on this origin, so a
        # signed-in session survives a reload.
        env_js = (
            'window.__ENV__ = { API_BASE_URL: "/api", TILE_BASE_URL: "/api", '
            'AUTO_LOGIN: "1" };\n'
        )

    handler = type("GeoLensWebHandler", (Handler,), {
        "dist": dist,
        "api_upstream": upstream,
        "env_js": env_js,
    })
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    server.geolens_api = upstream  # type: ignore[attr-defined]
    return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="GeoLens 独立前端（网页）")
    parser.add_argument("--host", default="127.0.0.1", help="绑定地址（局域网用 0.0.0.0）")
    parser.add_argument("--port", type=int, default=8081, help="网页端口")
    parser.add_argument("--api", default="http://127.0.0.1:8000", help="后端 API 地址")
    parser.add_argument("--direct", action="store_true",
                        help="不代理，浏览器直连 --api（跨域，需后端允许本页来源）")
    parser.add_argument("--public-api", default="", help="direct 模式下浏览器使用的 API 地址")
    parser.add_argument("--tile", default="", help="direct 模式下的切片地址，默认同 API")
    args = parser.parse_args(argv)

    server = build_server(args.host, args.port, args.api, direct=args.direct,
                          public_api=args.public_api, tile=args.tile)
    print(f"GeoLens 前端：http://{args.host}:{args.port}")
    upstream = getattr(server, "geolens_api", "")
    if upstream:
        print(f"  反向代理 : {upstream}（同源，登录状态刷新后保留）")
    else:
        print(f"  直连 API : {args.public_api or args.api}（跨域）")
    print("  按 Ctrl+C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
