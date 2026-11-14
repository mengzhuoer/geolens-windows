"""Accept the nginx-style public raster URL when the API is served directly.

The frontend mints ``/raster-tiles/<dataset>/tiles/<z>/<x>/<y>.<fmt>`` and the
Docker deployment's nginx rewrites it to the API's
``/tiles/raster-proxy/...`` route. A standalone frontend talks to the API
directly, so the alias has to live in the backend.
"""
from __future__ import annotations

import re

from starlette.types import ASGIApp, Receive, Scope, Send

_PATTERN = re.compile(
    r"^/raster-tiles/([0-9a-fA-F-]{36})/tiles/(\d+)/(\d+)/(\d+)\.([a-zA-Z]+)$"
)


class RasterAliasMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            match = _PATTERN.match(scope.get("path", ""))
            if match:
                dataset, z, x, y, fmt = match.groups()
                path = f"/tiles/raster-proxy/{dataset}/{z}/{x}/{y}.{fmt}"
                scope = dict(scope)
                scope["path"] = path
                scope["raw_path"] = path.encode()
        await self.app(scope, receive, send)
