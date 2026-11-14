"""Windows entry points that force the selector event loop.

psycopg's async pool refuses the default ProactorEventLoop on Windows, so the
API, worker and TiTiler are all launched through this module.

The selector loop does not implement asyncio subprocesses at all, while the
ingest pipeline shells out to ogrinfo/gdal. ``_install_subprocess_shim`` adds a
thread-backed ``asyncio.create_subprocess_exec`` with the small Process
interface those call sites use (communicate/wait/returncode/kill).
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from typing import Any


class _ThreadedProcess:
    """Minimal asyncio.subprocess.Process look-alike over subprocess.Popen."""

    def __init__(self, popen: subprocess.Popen) -> None:
        self._popen = popen

    @property
    def pid(self) -> int:
        return self._popen.pid

    @property
    def returncode(self) -> int | None:
        return self._popen.returncode

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        return await asyncio.to_thread(self._popen.communicate, input)

    async def wait(self) -> int:
        return await asyncio.to_thread(self._popen.wait)

    def kill(self) -> None:
        try:
            self._popen.kill()
        except OSError:
            pass

    def terminate(self) -> None:
        try:
            self._popen.terminate()
        except OSError:
            pass

    def send_signal(self, sig: int) -> None:
        self.terminate()


def _install_subprocess_shim() -> None:
    if sys.platform != "win32":
        return
    if getattr(asyncio.create_subprocess_exec, "_geolens_shim", False):
        return

    async def create_subprocess_exec(program: str, *args: Any, **kwargs: Any) -> _ThreadedProcess:
        popen_kwargs: dict[str, Any] = {}
        for key in ("stdin", "stdout", "stderr", "env", "cwd"):
            value = kwargs.get(key)
            if value is not None:
                popen_kwargs[key] = value
        # Selector-loop-only options with no Windows subprocess meaning.
        kwargs.pop("limit", None)
        kwargs.pop("start_new_session", None)
        pass_fds = kwargs.get("pass_fds")
        if pass_fds:
            raise NotImplementedError("pass_fds is not supported on Windows")
        creationflags = kwargs.get("creationflags") or 0
        popen_kwargs["creationflags"] = creationflags | getattr(
            subprocess, "CREATE_NO_WINDOW", 0
        )
        if kwargs.get("universal_newlines") or kwargs.get("text"):
            popen_kwargs["text"] = True
        popen = subprocess.Popen([program, *args], **popen_kwargs)
        return _ThreadedProcess(popen)

    create_subprocess_exec._geolens_shim = True  # type: ignore[attr-defined]
    asyncio.create_subprocess_exec = create_subprocess_exec  # type: ignore[assignment]


def _watch_launcher(role: str) -> None:
    """Exit when the desktop launcher exits, so no orphan services remain."""
    try:
        pid = int(os.environ.get("GEOLENS_LAUNCHER_PID", "0"))
    except ValueError:
        return
    if pid <= 0 or sys.platform != "win32":
        return

    def wait() -> None:
        import ctypes

        SYNCHRONIZE = 0x00100000
        INFINITE = 0xFFFFFFFF
        handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            return
        ctypes.windll.kernel32.WaitForSingleObject(handle, INFINITE)
        # The launcher died (crash / Task Manager). Stop the database once,
        # from the API process, so no services are left holding ports.
        if role == "api":
            pgctl = os.environ.get("GEOLENS_PGCTL", "")
            pgdata = os.environ.get("GEOLENS_PGDATA", "")
            if pgctl and pgdata:
                try:
                    subprocess.run(
                        [pgctl, "-D", pgdata, "-m", "fast", "-w", "-t", "10", "stop"],
                        capture_output=True, timeout=20,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                except Exception:
                    pass
        os._exit(0)

    import threading

    threading.Thread(target=wait, name="launcher-watchdog", daemon=True).start()


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        raise SystemExit("usage: server_entry.py api|worker|titiler")
    role = argv[0]
    _install_subprocess_shim()
    _watch_launcher(role)

    if role == "api":
        import uvicorn

        config = uvicorn.Config(
            "app.api.main:app",
            host=os.environ.get("GEOLENS_API_HOST", "127.0.0.1"),
            port=int(os.environ.get("GEOLENS_API_PORT", "8000")),
            log_level=os.environ.get("GEOLENS_LOG_LEVEL", "info"),
        )
        server = uvicorn.Server(config)
        # Uvicorn's own win32 factory picks ProactorEventLoop; psycopg's async
        # pool refuses it, so hand asyncio.run the selector loop explicitly.
        asyncio.run(server.serve(), loop_factory=asyncio.SelectorEventLoop)
    elif role == "worker":
        from app.platform.jobs.worker import main as worker_main

        asyncio.run(worker_main(), loop_factory=asyncio.SelectorEventLoop)
    elif role == "titiler":
        import uvicorn

        config = uvicorn.Config(
            "titiler.application.main:app",
            host=os.environ.get("GEOLENS_HOST", "127.0.0.1"),
            port=int(os.environ.get("GEOLENS_TITILER_PORT", "8001")),
            log_level=os.environ.get("GEOLENS_LOG_LEVEL", "info"),
        )
        server = uvicorn.Server(config)
        asyncio.run(server.serve(), loop_factory=asyncio.SelectorEventLoop)
    else:
        raise SystemExit(f"unknown role: {role}")


if __name__ == "__main__":
    main()
