"""PostgreSQL lifecycle for the desktop bundle (native Windows, no Docker)."""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Callable, Any

from . import config as cfgmod
from . import paths

LogFn = Callable[[str], None]


def _noop(_: str) -> None:
    pass


def _pg(binary: str) -> Path:
    return paths.pgsql_dir() / "bin" / f"{binary}.exe"


def _run(args: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
         timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _psql_args(cfg: dict[str, Any], database: str = "postgres", user: str = "postgres") -> list[str]:
    return [
        str(_pg("psql")),
        "-X", "-v", "ON_ERROR_STOP=1",
        "-h", cfg["database"]["host"],
        "-p", str(cfg["ports"]["postgres"]),
        "-U", user,
        "-d", database,
    ]


def _psql_env(cfg: dict[str, Any]) -> dict[str, str]:
    env = os.environ.copy()
    env["PGPASSWORD"] = cfg["secrets"]["db_password"]
    env["PGCLIENTENCODING"] = "UTF8"
    return env


def _pg_hba(pgdata: Path) -> None:
    """Loopback-only, password (SCRAM) for TCP, trust for local admin sockets."""
    content = (
        "# GeoLens Desktop: loopback only\n"
        "host    all             all             127.0.0.1/32            scram-sha-256\n"
        "host    all             all             ::1/128                 scram-sha-256\n"
        "local   all             all                                     trust\n"
    )
    (pgdata / "pg_hba.conf").write_text(content, encoding="utf-8")


def _postgresql_conf(pgdata: Path, cfg: dict[str, Any]) -> None:
    port = cfg["ports"]["postgres"]
    text = f"""
# ---- GeoLens Desktop tuning ----
listen_addresses = '127.0.0.1'
max_connections = 100
shared_buffers = 256MB
work_mem = 16MB
maintenance_work_mem = 128MB
effective_cache_size = 768MB
wal_level = replica
max_wal_size = 1GB
min_wal_size = 128MB
timezone = 'Asia/Shanghai'
log_timezone = 'Asia/Shanghai'
logging_collector = off
log_min_duration_statement = 2000
"""
    with open(pgdata / "postgresql.conf", "a", encoding="utf-8") as handle:
        handle.write(text)


def _pg_isready(cfg: dict[str, Any]) -> bool:
    result = _run([
        str(_pg("pg_isready")),
        "-h", cfg["database"]["host"],
        "-p", str(cfg["ports"]["postgres"]),
        "-q",
    ], timeout=15)
    return result.returncode == 0


def is_initialized() -> bool:
    return (paths.pgdata_dir() / "PG_VERSION").is_file()


def init_cluster(cfg: dict[str, Any], log: LogFn = _noop) -> None:
    pgdata = paths.pgdata_dir()
    pgdata.mkdir(parents=True, exist_ok=True)
    pwfile = paths.logs_dir() / ".initdb-password"
    pwfile.write_text(cfg["secrets"]["db_password"], encoding="utf-8")
    try:
        log("初始化数据库集群（首次启动，约 10 秒）…")
        result = _run([
            str(_pg("initdb")),
            "-D", str(pgdata),
            "-U", "postgres",
            f"--pwfile={pwfile}",
            "--encoding=UTF8",
            "--locale=C",
        ], timeout=300)
        if result.returncode != 0:
            raise RuntimeError(f"initdb failed:\n{result.stdout}\n{result.stderr}")
    finally:
        pwfile.unlink(missing_ok=True)
    _postgresql_conf(pgdata, cfg)
    _pg_hba(pgdata)


def start(cfg: dict[str, Any], log: LogFn = _noop) -> None:
    if _pg_isready(cfg):
        log("PostgreSQL 已在运行")
        return
    log("启动 PostgreSQL…")
    # Do NOT capture output here: postgres.exe inherits the pipe handles and
    # keeps them open, so subprocess.run(capture_output=True) would block until
    # the server stops. pg_ctl already writes the server log via -l.
    ctl_log = paths.logs_dir() / "pg_ctl.log"
    with open(ctl_log, "ab") as handle:
        result = subprocess.run(
            [
                str(_pg("pg_ctl")),
                "-D", str(paths.pgdata_dir()),
                "-l", str(paths.logs_dir() / "postgres.log"),
                "-o", f"-p {cfg['ports']['postgres']} -c listen_addresses=127.0.0.1",
                "start",
            ],
            stdout=handle, stderr=handle,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    deadline = time.time() + 60
    while time.time() < deadline:
        if _pg_isready(cfg):
            log("PostgreSQL 已就绪")
            return
        time.sleep(0.5)
    tail = _tail(paths.logs_dir() / "postgres.log")
    raise RuntimeError(f"PostgreSQL 启动失败（pg_ctl 退出码 {result.returncode}）:\n{tail}")


def stop(log: LogFn = _noop) -> None:
    if not is_initialized():
        return
    log("停止 PostgreSQL…")
    pidfile = paths.pgdata_dir() / "postmaster.pid"
    for mode in ("fast", "immediate"):
        _run([
            str(_pg("pg_ctl")), "-D", str(paths.pgdata_dir()),
            "-m", mode, "-w", "-t", "30", "stop",
        ], timeout=60)
        deadline = time.time() + 15
        while time.time() < deadline:
            if not pidfile.exists():
                return
            time.sleep(0.5)


def _tail(path: Path, lines: int = 25) -> str:
    try:
        data = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(data[-lines:])
    except OSError:
        return ""


def bootstrap(cfg: dict[str, Any], log: LogFn = _noop) -> None:
    """Create the role/database, schemas and extensions (idempotent)."""
    db = cfg["database"]
    pw = cfg["secrets"]["db_password"]
    env = _psql_env(cfg)

    role_sql = (
        "DO $$ BEGIN "
        f"IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{db['user']}') THEN "
        f"CREATE ROLE {db['user']} LOGIN PASSWORD '{pw}' SUPERUSER; "
        f"ELSE ALTER ROLE {db['user']} WITH LOGIN PASSWORD '{pw}' SUPERUSER; "
        "END IF; END $$;"
    )
    result = _run(_psql_args(cfg) + ["-c", role_sql], env=env, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"创建数据库角色失败:\n{result.stderr}")

    exists = _run(_psql_args(cfg) + ["-tAc", f"SELECT 1 FROM pg_database WHERE datname = '{db['db']}'"],
                  env=env, timeout=60)
    if "1" not in exists.stdout:
        log("创建数据库 geolens…")
        result = _run(_psql_args(cfg) + ["-c", f"CREATE DATABASE {db['db']} OWNER {db['user']}"],
                      env=env, timeout=120)
        if result.returncode != 0:
            raise RuntimeError(f"创建数据库失败:\n{result.stderr}")

    ext_sql = (
        "CREATE EXTENSION IF NOT EXISTS postgis;"
        "CREATE EXTENSION IF NOT EXISTS pg_trgm;"
        "CREATE EXTENSION IF NOT EXISTS vector;"
        "CREATE EXTENSION IF NOT EXISTS unaccent;"
        "CREATE SCHEMA IF NOT EXISTS catalog;"
        "CREATE SCHEMA IF NOT EXISTS data;"
    )
    result = _run(
        _psql_args(cfg, database=db["db"], user=db["user"]) + ["-c", ext_sql],
        env=env, timeout=180,
    )
    if result.returncode != 0:
        raise RuntimeError(f"启用扩展失败（PostGIS/pgvector 是否完整？）:\n{result.stderr}")


def alembic_env(cfg: dict[str, Any]) -> dict[str, str]:
    env = os.environ.copy()
    root = paths.base_dir()
    env["PATH"] = ";".join([
        str(root / "runtime" / "py" / "Library" / "bin"),
        str(root / "runtime" / "pgsql" / "bin"),
        env.get("PATH", ""),
    ])
    env["GDAL_DATA"] = str(root / "runtime" / "py" / "Library" / "share" / "gdal")
    env["PROJ_DATA"] = str(root / "runtime" / "py" / "Library" / "share" / "proj")
    env["PYTHONPATH"] = str(paths.backend_dir())
    env["POSTGRES_HOST"] = cfg["database"]["host"]
    env["POSTGRES_PORT"] = str(cfg["ports"]["postgres"])
    env["POSTGRES_DB"] = cfg["database"]["db"]
    env["POSTGRES_USER"] = cfg["database"]["user"]
    env["POSTGRES_PASSWORD"] = cfg["secrets"]["db_password"]
    env["SECRET_ENCRYPTION_KEY"] = cfg["secrets"]["secret_encryption_key"]
    env["JWT_SECRET_KEY"] = cfg["secrets"]["jwt_secret"]
    env["GEOLENS_ADMIN_USERNAME"] = cfg["admin"]["username"]
    env["GEOLENS_ADMIN_PASSWORD"] = cfg["admin"]["password"]
    env["STORAGE_PROVIDER"] = "local"
    env["UPLOAD_STAGING_DIR"] = str(paths.storage_dir())
    env["ENVIRONMENT"] = "development"
    return env


def migrate(cfg: dict[str, Any], log: LogFn = _noop) -> None:
    log("应用数据库迁移…")
    env = alembic_env(cfg)
    result = _run(
        [str(paths.python_exe()), "-m", "alembic", "upgrade", "heads"],
        cwd=paths.backend_dir(), env=env, timeout=900,
    )
    if result.returncode != 0:
        output = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"数据库迁移失败:\n{output[-4000:]}")
    log("数据库迁移完成")


def current_revision(cfg: dict[str, Any]) -> str:
    env = _psql_env(cfg)
    # Alembic is configured with version_table_schema=catalog.
    result = _run(
        _psql_args(cfg, database=cfg["database"]["db"], user=cfg["database"]["user"])
        + ["-tAc", "SELECT version_num FROM catalog.alembic_version"],
        env=env, timeout=30,
    )
    return result.stdout.strip()


def _schema_marker(version: str | None = None) -> Path:
    return paths.data_dir() / "schema-version.txt"


def schema_uptodate(cfg: dict[str, Any], version: str) -> bool:
    """Whether this data dir already has the schema for the running build.

    Upgrades change the version marker; a crash mid-migration leaves no marker,
    so the next boot re-runs alembic.
    """
    marker = _schema_marker()
    try:
        recorded = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    if recorded != version:
        return False
    return bool(current_revision(cfg))


def mark_schema(version: str) -> None:
    try:
        _schema_marker().write_text(version, encoding="utf-8")
    except OSError:
        pass
