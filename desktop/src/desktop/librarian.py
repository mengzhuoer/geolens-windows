"""Local imagery librarian: discover → organize → ingest.

The librarian watches one or more source directories, copies (or moves) every
supported geospatial file into a single managed library folder, de-duplicates
by content fingerprint, and submits each new file to the local GeoLens API
(upload → preview → commit). A SQLite journal makes the whole pass resumable
and idempotent.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import sqlite3
import threading
import time
import traceback
import zipfile
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import paths
from .client import ApiError, GeoLensClient

RASTER_EXTS = {
    ".tif", ".tiff", ".img", ".jp2", ".dat",
    ".bmp", ".grd", ".pix", ".ecw", ".sid",
}
VECTOR_EXTS = {".geojson", ".gpkg", ".shp", ".fgb", ".kml", ".kmz", ".parquet"}
CONVERT_EXTS = RASTER_EXTS - {".tif", ".tiff"}
# Formats that need the user to choose a sub-dataset (or unpack) first.
MULTI_CONTAINER_EXTS = {".hdf", ".h4", ".hdf4", ".hdf5", ".nc", ".nc4", ".grib", ".grib2"}
ARCHIVE_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz", ".rar", ".7z")
SKIP_DIRS = {
    ".git", "__pycache__", ".venv", "venv", "node_modules", ".idea", ".vscode",
    ".pytest_cache", ".ruff_cache", "dist", "build", "site-packages",
}
# Whole-drive scans must not crawl the OS itself; those trees never hold
# user imagery and make a full-disk pass take hours.
SYSTEM_DIRS = {
    "windows", "winsxs", "system32", "syswow64", "servicing", "recovery",
    "program files", "program files (x86)", "programdata", "appdata",
    "$recycle.bin", "system volume information", "perflogs",
    "msocache", "config.msi",
}
SHAPEFILE_SIDECARS = (".shp", ".dbf", ".shx", ".prj", ".cpg", ".qix", ".sbn", ".sbx")
RASTER_SIDECARS = (".aux.xml", ".ovr", ".tfw", ".xml", ".enp", ".hdr")

STATUS_NEW = "new"
STATUS_ORGANIZED = "organized"
STATUS_INGESTED = "ingested"
STATUS_DUPLICATE = "duplicate"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    source_path  TEXT PRIMARY KEY,
    size         INTEGER NOT NULL,
    mtime        REAL NOT NULL,
    fingerprint  TEXT NOT NULL,
    ingest_fingerprint TEXT,
    library_path TEXT,
    status       TEXT NOT NULL,
    dataset_id   TEXT,
    attempts     INTEGER NOT NULL DEFAULT 0,
    message      TEXT,
    updated_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fingerprints (
    fingerprint  TEXT PRIMARY KEY,
    library_path TEXT,
    dataset_id   TEXT,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_items_status ON items(status);
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _rel_safe(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return cleaned or "_"


def _label(root: Path) -> str:
    drive = root.drive.replace(":", "") or "root"
    tail = root.name or "root"
    return _rel_safe(f"{drive}_{tail}")


def fingerprint(path: Path, mode: str = "quick") -> str:
    st = path.stat()
    digest = hashlib.sha256()
    digest.update(str(st.st_size).encode())
    if mode == "full":
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    window = 512 * 1024
    with open(path, "rb") as handle:
        digest.update(handle.read(window))
        if st.st_size > window:
            handle.seek(max(0, st.st_size - window))
            digest.update(handle.read(window))
    return digest.hexdigest()


def pretty_title(path: Path) -> str:
    stem = path.stem
    stem = re.sub(r"[_]+", " ", stem)
    stem = re.sub(r"\s+", " ", stem).strip()
    stamp = ""
    match = re.search(r"(19|20)\d{2}[-_]?\d{2}[-_]?\d{2}", stem)
    if match and match.start() > 0:
        stamp = match.group(0) + " "
        stem = (stem[: match.start()] + stem[match.end():]).strip()
    title = f"{stem} {stamp}".strip()
    return (title or path.stem)[:500]


class Journal:
    def __init__(self, path: Path) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(items)")}
            if "ingest_fingerprint" not in columns:
                self._conn.execute("ALTER TABLE items ADD COLUMN ingest_fingerprint TEXT")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def get(self, source_path: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM items WHERE source_path = ?",
                                     (source_path,)).fetchone()
        return dict(row) if row else None

    def put(self, **fields: Any) -> None:
        allowed = {"source_path", "size", "mtime", "fingerprint", "ingest_fingerprint",
                   "library_path", "status", "dataset_id", "attempts", "message"}
        values = {k: fields.get(k) for k in allowed if k in fields}
        with self._lock:
            existing = self._conn.execute(
                "SELECT source_path FROM items WHERE source_path = ?",
                (values["source_path"],),
            ).fetchone()
            values["updated_at"] = _now()
            if existing:
                assignments = ", ".join(f"{k} = ?" for k in values)
                self._conn.execute(f"UPDATE items SET {assignments} WHERE source_path = ?",
                                   [*values.values(), values["source_path"]])
            else:
                columns = ", ".join(values)
                marks = ", ".join("?" for _ in values)
                self._conn.execute(f"INSERT INTO items ({columns}) VALUES ({marks})",
                                   list(values.values()))
            self._conn.commit()

    def seen(self, fp: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM fingerprints WHERE fingerprint = ?",
                                     (fp,)).fetchone()
        return dict(row) if row else None

    def mark_seen(self, fp: str, library_path: str | None, dataset_id: str | None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO fingerprints (fingerprint, library_path, dataset_id, created_at)"
                " VALUES (?, ?, ?, ?)",
                (fp, library_path, dataset_id, _now()),
            )
            self._conn.commit()

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute("SELECT status, COUNT(*) AS n FROM items GROUP BY status").fetchall()
        return {row["status"]: row["n"] for row in rows}

    def recent(self, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM items ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]


class Librarian:
    def __init__(self, get_config: Callable[[], dict[str, Any]], api_url: Callable[[], str]) -> None:
        self._get_config = get_config
        self._api_url = api_url
        self.journal = Journal(paths.journal_path())
        self._scan_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._log_lines: deque[str] = deque(maxlen=400)
        self._log_lock = threading.Lock()
        self._pending_extra: list[Path] = []
        self._cancel_scan = threading.Event()
        self._state: dict[str, Any] = {
            "mode": "idle", "last_scan": None, "last_result": None,
            "current": None, "progress": {"done": 0, "total": 0},
        }
        self._state_lock = threading.Lock()
        self._client: GeoLensClient | None = None

    # ---- logging/state -------------------------------------------------
    def log(self, message: str) -> None:
        line = f"[{_now()}] {message}"
        with self._log_lock:
            self._log_lines.append(line)
            try:
                with open(paths.logs_dir() / "librarian.log", "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                pass

    def logs(self, limit: int = 200) -> list[str]:
        return list(self._log_lines)[-limit:]

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            state = dict(self._state)
        cfg = self._get_config()["library"]
        state["counts"] = self.journal.counts()
        state["config"] = {
            "source_dirs": cfg["source_dirs"],
            "library_dir": cfg["library_dir"],
            "auto_ingest": cfg["auto_ingest"],
            "mode": cfg["mode"],
            "scan_interval_seconds": cfg["scan_interval_seconds"],
        }
        return state

    def _set_state(self, **changes: Any) -> None:
        with self._state_lock:
            self._state.update(changes)

    # ---- lifecycle -----------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="geolens-librarian", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=10)
        self.journal.close()

    def trigger(self, reason: str = "manual", extra_dirs: list[Path] | None = None) -> bool:
        if self._scan_lock.locked():
            return False
        if extra_dirs:
            self._pending_extra = [Path(p) for p in extra_dirs]
        self._wake.set()
        self.log(f"收到扫描请求（{reason}）")
        return True

    def cancel_scan(self) -> bool:
        if not self._scan_lock.locked():
            return False
        self._cancel_scan.set()
        self._set_state(mode="cancelling")
        self.log("收到停止扫描请求，正在停止…")
        return True

    def _loop(self) -> None:
        # Give the API a head start on first boot.
        time.sleep(3)
        first = True
        while not self._stop.is_set():
            extra = list(self._pending_extra)
            self._pending_extra = []
            cfg = self._get_config()["library"]
            if first and not extra and not cfg.get("scan_on_startup", False):
                self.log("已跳过启动扫描（可在影像库设置里开启“启动后立即扫描”）")
            else:
                reason = "指定文件夹扫描" if extra else (
                    "启动扫描" if first else "定时扫描"
                )
                try:
                    self.scan_once(reason, extra_dirs=extra)
                except Exception:
                    self.log("扫描异常:\n" + traceback.format_exc())
            first = False
            interval = max(15, int(cfg.get("scan_interval_seconds", 120)))
            self._wake.wait(timeout=interval)
            self._wake.clear()

    # ---- client ------------------------------------------------------
    def _get_client(self) -> GeoLensClient:
        admin = self._get_config()["admin"]
        if not self._client or not self._client.access_token:
            self._client = GeoLensClient(self._api_url(), admin["username"], admin["password"])
            self._client.login()
        return self._client

    # ---- scanning ----------------------------------------------------
    def scan_once(self, reason: str = "manual", extra_dirs: list[Path] | None = None) -> dict[str, Any]:
        if not self._scan_lock.acquire(blocking=False):
            return {"busy": True}
        try:
            self._cancel_scan.clear()
            self._set_state(
                mode="scanning", running_reason=reason, started_at=_now(),
                scanned=0, processed=0, walked_dirs=0, current_root=None,
                current=None, progress={"done": 0, "total": 0},
            )
            self.log(f"开始扫描（{reason}）")
            result = self._scan(extra_dirs or [])
            cancelled = self._cancel_scan.is_set()
            self._set_state(
                mode="idle", last_scan=_now(), last_result=result,
                cancelled=cancelled, current=None, current_root=None,
                progress={"done": result["processed"], "total": result["found"]},
            )
            self.log(
                ("扫描已停止：" if cancelled else "扫描完成：") +
                "发现 {found}，整理 {organized}，入库 {ingested}，"
                "重复 {duplicate}，跳过 {skipped}，失败 {failed}".format(**result)
            )
            if result["found"] == 0 and not cancelled:
                self.log(
                    "没有发现可入库的文件。请确认：文件夹路径正确；扩展名在允许列表内"
                    "（.tif/.tiff/.img/.jp2/.dat/…）；文件大小未超上限；压缩包需先解压；"
                    "HDF/NetCDF 请先导出 GeoTIFF。"
                )
            return result
        finally:
            self._scan_lock.release()

    @staticmethod
    def clean_path(raw: Any) -> Path | None:
        text = str(raw or "").strip().strip('"').strip("'")
        if not text:
            return None
        text = os.path.expandvars(os.path.expanduser(text))
        path = Path(text)
        try:
            resolved = path.resolve()
        except OSError:
            return None
        return resolved if resolved.is_dir() else None

    def _source_roots(self, cfg: dict[str, Any], extra_dirs: list[Path]) -> list[Path]:
        roots: list[Path] = []
        seen: set[str] = set()

        def add(path: Path | None, origin: str) -> None:
            if path is None:
                return
            key = str(path).lower()
            if key in seen:
                return
            seen.add(key)
            roots.append(path)

        for raw in cfg.get("source_dirs", []):
            cleaned = self.clean_path(raw)
            if cleaned is None:
                self.log(f"源目录不存在，已忽略：{raw}")
            else:
                add(cleaned, "配置")
        for path in extra_dirs:
            cleaned = self.clean_path(path)
            if cleaned is None:
                self.log(f"指定目录不存在：{path}")
            else:
                add(cleaned, "一次性")

        library = Path(cfg["library_dir"]).expanduser()
        try:
            library = library.resolve()
        except OSError:
            library = None
        if library is not None and library.is_dir():
            add(library, "影像库")
        return roots

    def _walk(self, root: Path, cfg: dict[str, Any], exclude: Path | None):
        """Yield candidate files incrementally, pruning system trees."""
        skip_hidden = bool(cfg.get("skip_hidden", True))
        skip_system = bool(cfg.get("skip_system_dirs", True))
        recursive = bool(cfg.get("recursive", True))
        own_data = paths.data_dir().resolve()
        walked = 0
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            if self._cancel_scan.is_set() or self._stop.is_set():
                return
            current = Path(dirpath)
            if exclude is not None:
                try:
                    if current.resolve() == exclude:
                        dirnames[:] = []
                        continue
                except OSError:
                    pass
            if skip_hidden:
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            blocked = set(SKIP_DIRS)
            if skip_system:
                blocked |= SYSTEM_DIRS
            dirnames[:] = [d for d in dirnames if d.lower() not in blocked]
            if current == own_data or own_data in current.parents:
                dirnames[:] = []
                continue
            if not recursive:
                dirnames[:] = []
            walked += 1
            if walked % 50 == 0:
                self._set_state(scanned=self._state.get("scanned", 0), walked_dirs=walked,
                                current_dir=str(current))
            for name in filenames:
                if self._cancel_scan.is_set() or self._stop.is_set():
                    return
                if skip_hidden and name.startswith("."):
                    continue
                yield current / name
        self._set_state(walked_dirs=walked)

    def _scan(self, extra_dirs: list[Path]) -> dict[str, int]:
        cfg = self._get_config()["library"]
        result = {"found": 0, "processed": 0, "organized": 0, "ingested": 0,
                  "duplicate": 0, "skipped": 0, "failed": 0}
        roots = self._source_roots(cfg, extra_dirs)
        if not roots:
            self.log("尚未配置任何源目录。请在「本地影像库」页添加，或用「指定文件夹立即扫描」。")
            return result

        library = Path(cfg["library_dir"]).expanduser().resolve()
        library.mkdir(parents=True, exist_ok=True)
        include = {str(e).strip().lower() for e in cfg.get("include_extensions", []) if str(e).strip()}
        min_bytes = int(float(cfg.get("min_size_mb", 0)) * 1024 * 1024)
        max_bytes = int(float(cfg.get("max_size_mb", 4096)) * 1024 * 1024)
        known_exts = include | RASTER_EXTS | VECTOR_EXTS | MULTI_CONTAINER_EXTS

        for root in roots:
            if self._cancel_scan.is_set() or self._stop.is_set():
                break
            self._set_state(current_root=str(root))
            self.log(f"扫描根目录：{root}")
            exclude = library if (root != library and library.is_relative_to(root)) else None

            for file in self._walk(root, cfg, exclude):
                if self._cancel_scan.is_set() or self._stop.is_set():
                    break
                name = file.name.lower()
                suffix = file.suffix.lower()
                # Archives are not auto-ingested, but they must not fill the
                # journal with rows on every whole-drive pass either.
                if name.endswith(ARCHIVE_SUFFIXES):
                    continue
                if suffix not in known_exts:
                    continue
                if suffix in RASTER_SIDECARS or name.endswith(".aux.xml"):
                    continue
                if name.endswith((".tmp", ".part", ".crdownload")):
                    continue
                if suffix == ".dat" and not file.with_suffix(".hdr").is_file():
                    result["skipped"] += 1
                    result["processed"] += 1
                    self._skip(file, "ENVI .dat 缺少同名 .hdr 头文件")
                    continue
                if suffix in MULTI_CONTAINER_EXTS:
                    result["skipped"] += 1
                    result["processed"] += 1
                    self._skip(file, "多子数据集容器（HDF/NetCDF/GRIB）请先导出为 GeoTIFF")
                    continue

                result["found"] += 1
                try:
                    size = file.stat().st_size
                except OSError:
                    result["processed"] += 1
                    continue
                if size < min_bytes or size > max_bytes:
                    result["skipped"] += 1
                    result["processed"] += 1
                    self._skip(file, f"文件大小 {size/1048576:.1f} MB 超出允许范围")
                    self._update_progress(result)
                    continue

                if root == library:
                    dest = file
                else:
                    rel = file.relative_to(root)
                    dest = library / _label(root) / rel

                self._set_state(current=str(file), scanned=result["found"],
                                progress={"done": result["processed"], "total": result["found"]})
                try:
                    outcome = self._process(file, root, dest, library, cfg)
                    result[outcome] = result.get(outcome, 0) + 1
                except Exception as exc:
                    result["failed"] += 1
                    self._record_failure(file, dest, str(exc))
                    self.log(f"失败 {file}: {exc}")
                result["processed"] += 1
                if result["processed"] % 50 == 0:
                    self.log(
                        f"扫描中：已发现 {result['found']}，已处理 {result['processed']}，"
                        f"入库 {result['ingested']}，跳过 {result['skipped']}，失败 {result['failed']}"
                    )
                self._update_progress(result)
        return result

    def _update_progress(self, result: dict[str, int]) -> None:
        self._set_state(
            scanned=result["found"],
            processed=result["processed"],
            progress={"done": result["processed"], "total": result["found"]},
        )

    # ---- per-file pipeline -------------------------------------------
    def _record_failure(self, file: Path, dest: Path, message: str) -> None:
        previous = self.journal.get(str(file))
        attempts = (previous["attempts"] if previous else 0) + 1
        fp = previous["fingerprint"] if previous else ""
        try:
            st = file.stat()
        except OSError:
            st = None
        self.journal.put(
            source_path=str(file), size=(st.st_size if st else 0),
            mtime=(st.st_mtime if st else 0.0), fingerprint=fp,
            library_path=str(dest), status=STATUS_FAILED,
            dataset_id=(previous["dataset_id"] if previous else None),
            attempts=attempts, message=message[:2000],
        )

    def _skip(self, file: Path, message: str) -> None:
        try:
            st = file.stat()
        except OSError:
            return
        self.journal.put(
            source_path=str(file), size=st.st_size, mtime=st.st_mtime,
            fingerprint="", library_path=None, status=STATUS_SKIPPED,
            attempts=0, message=message[:2000],
        )

    def _process(self, file: Path, root: Path, dest: Path, library: Path,
                 cfg: dict[str, Any]) -> str:
        st = file.stat()
        source_fp = fingerprint(file, cfg.get("hash_mode", "quick"))
        previous = self.journal.get(str(file))

        if (previous and previous["status"] == STATUS_INGESTED
                and previous["fingerprint"] == source_fp and previous["size"] == st.st_size):
            return "skipped"

        seen = self.journal.seen(source_fp)
        if seen and seen.get("dataset_id"):
            self._mark_duplicate(file, st, source_fp, seen.get("library_path"),
                                 seen.get("dataset_id"), "内容与已有数据集重复")
            return "duplicate"

        # Already in the library: no organization step.
        if root == library:
            organized = file
        else:
            organized, _ = self._organize(file, dest, cfg)

        ingest_path = organized
        ingest_fp = source_fp
        if organized.suffix.lower() == ".shp":
            # The API's upload allowlist takes shapefiles only as a zip bundle.
            ingest_path = self._zip_shapefile(organized)
            ingest_fp = fingerprint(ingest_path, cfg.get("hash_mode", "quick"))
            seen = self.journal.seen(ingest_fp)
            if seen and seen.get("dataset_id"):
                self._mark_duplicate(file, st, source_fp, str(organized),
                                     seen.get("dataset_id"), "边界数据与已有数据集重复",
                                     ingest_fp=ingest_fp)
                return "duplicate"
        elif organized.suffix.lower() in CONVERT_EXTS and cfg.get("convert_unsupported", True):
            ingest_path = self._convert_to_geotiff(organized)
            ingest_fp = fingerprint(ingest_path, cfg.get("hash_mode", "quick"))
            seen = self.journal.seen(ingest_fp)
            if seen and seen.get("dataset_id"):
                self._mark_duplicate(file, st, source_fp, str(organized),
                                     seen.get("dataset_id"), "转换后与已有数据集重复",
                                     ingest_fp=ingest_fp)
                return "duplicate"
        try:
            ingest_size = ingest_path.stat().st_size
        except OSError:
            ingest_size = st.st_size

        self.journal.put(
            source_path=str(file), size=st.st_size, mtime=st.st_mtime,
            fingerprint=source_fp, ingest_fingerprint=ingest_fp,
            library_path=str(organized),
            status=STATUS_ORGANIZED, dataset_id=None, attempts=0,
            message="已整理，等待入库",
        )
        self._write_sidecar(organized, file, ingest_fp, None)

        if not cfg.get("auto_ingest", True):
            if root != library:
                self._move_source_if_requested(file, cfg)
            return "organized"

        # Make sure the API is up (first scan often fires during startup).
        client = self._get_client()
        title = pretty_title(organized)
        summary = (
            f"来源文件：{file}\n"
            f"影像库路径：{organized}\n"
            f"自动整理时间：{_now()}"
        )
        self.log(f"上传入库：{organized.name}（{ingest_size / 1048576:.1f} MB）")
        try:
            outcome = client.ingest_file(ingest_path, title=title, summary=summary,
                                         wait=True, timeout=3600)
        except ApiError as exc:
            if exc.status in (401, 403):
                self._client = None
                client = self._get_client()
                outcome = client.ingest_file(ingest_path, title=title, summary=summary,
                                             wait=True, timeout=3600)
            else:
                raise
        if outcome.get("status") != "complete":
            raise RuntimeError(f"入库任务未完成：{outcome.get('status')} {outcome.get('error') or ''}")

        dataset_id = outcome.get("dataset_id")
        self.journal.put(
            source_path=str(file), size=st.st_size, mtime=st.st_mtime,
            fingerprint=source_fp, ingest_fingerprint=ingest_fp,
            library_path=str(organized),
            status=STATUS_INGESTED, dataset_id=dataset_id, attempts=0,
            message="入库成功",
        )
        self.journal.mark_seen(source_fp, str(organized), dataset_id)
        if ingest_fp != source_fp:
            self.journal.mark_seen(ingest_fp, str(organized), dataset_id)
        self._write_sidecar(organized, file, ingest_fp, dataset_id)
        if root != library:
            self._move_source_if_requested(file, cfg)
        self.log(f"入库成功：{title} → {dataset_id}")
        return "ingested"

    def _mark_duplicate(self, file: Path, st: os.stat_result, source_fp: str,
                        library_path: str | None, dataset_id: str | None,
                        message: str, ingest_fp: str | None = None) -> None:
        self.journal.put(
            source_path=str(file), size=st.st_size, mtime=st.st_mtime,
            fingerprint=source_fp, ingest_fingerprint=ingest_fp or source_fp,
            library_path=library_path, status=STATUS_DUPLICATE,
            dataset_id=dataset_id, attempts=0, message=message,
        )
        self.log(f"重复跳过：{file}")

    def _organize(self, file: Path, dest: Path, cfg: dict[str, Any]) -> tuple[Path, bool]:
        mode = cfg.get("mode", "copy")
        dest.parent.mkdir(parents=True, exist_ok=True)
        target = dest
        if target.exists():
            same = target.stat().st_size == file.stat().st_size
            if same:
                return target, False
            base = target.stem
            suffix = target.suffix
            for n in range(1, 1000):
                candidate = target.with_name(f"{base}_{n}{suffix}")
                if not candidate.exists() or candidate.stat().st_size == file.stat().st_size:
                    target = candidate
                    break

        if mode == "link":
            try:
                os.link(file, target)
            except OSError:
                shutil.copy2(file, target)
        elif mode == "move":
            shutil.move(str(file), str(target))
        else:
            if not target.exists():
                shutil.copy2(file, target)

        # Shapefile/raster companions travel with the primary.
        for companion, companion_dest in self._companion_pairs(file, target):
            if mode == "move":
                shutil.move(str(companion), str(companion_dest))
            elif not companion_dest.exists():
                shutil.copy2(companion, companion_dest)
        return target, True

    @staticmethod
    def _companion_pairs(file: Path, target: Path) -> list[tuple[Path, Path]]:
        pairs: list[tuple[Path, Path]] = []
        if file.suffix.lower() == ".shp":
            for ext in SHAPEFILE_SIDECARS:
                if ext == ".shp":
                    continue
                candidate = file.with_suffix(ext)
                if candidate.is_file():
                    pairs.append((candidate, target.with_suffix(ext)))
        else:
            for ext in (".aux.xml", ".ovr", ".tfw", ".enp", ".hdr"):
                candidate = Path(str(file) + ext) if ext == ".aux.xml" else file.with_suffix(ext)
                if candidate.is_file():
                    dest = Path(str(target) + ext) if ext == ".aux.xml" else target.with_suffix(ext)
                    pairs.append((candidate, dest))
        return pairs

    def _convert_to_geotiff(self, source: Path) -> Path:
        """Convert an ENVI/JP2 source to a plain GeoTIFF for the ingest pipeline.

        The server does its own COG conversion; handing it an already-COG file
        trips GDAL 3.13's layout guard, so this deliberately produces a plain
        tiled GeoTIFF.
        """
        target = source.with_suffix(".converted.tif")
        if target.is_file() and target.stat().st_mtime >= source.stat().st_mtime:
            return target
        self.log(f"转换为 GeoTIFF：{source.name}")
        import subprocess

        from . import processes

        gdal_translate = paths.runtime_dir() / "py" / "Library" / "bin" / "gdal_translate.exe"
        env = processes.build_env(self._get_config())
        result = subprocess.run(
            [
                str(gdal_translate), "-q", "-of", "GTiff",
                "-co", "TILED=YES", "-co", "COMPRESS=DEFLATE",
                str(source), str(target),
            ],
            env=env, capture_output=True, text=True, timeout=3600,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0 or not target.is_file():
            raise RuntimeError(
                "GeoTIFF 转换失败：" + ((result.stderr or result.stdout or "unknown error").strip()[-1500:])
            )
        return target

    def _zip_shapefile(self, source: Path) -> Path:
        """Bundle a shapefile set into one zip for the upload allowlist."""
        target = source.with_suffix(".zip")
        members = [source]
        for ext in SHAPEFILE_SIDECARS:
            if ext == ".shp":
                continue
            candidate = source.with_suffix(ext)
            if candidate.is_file():
                members.append(candidate)
        try:
            newest = max(member.stat().st_mtime for member in members)
            if target.is_file() and target.stat().st_mtime >= newest:
                return target
        except OSError:
            pass
        tmp = target.with_name(target.name + ".tmp")
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as archive:
            for member in members:
                # One folder inside the zip: GDAL's /vsizip/ root points at it.
                archive.write(member, arcname=f"{source.stem}/{member.name}")
        tmp.replace(target)
        return target

    def _write_sidecar(self, organized: Path, source: Path, fp: str, dataset_id: str | None) -> None:
        import json

        payload = {
            "source_path": str(source),
            "library_path": str(organized),
            "fingerprint": fp,
            "dataset_id": dataset_id,
            "updated_at": _now(),
        }
        try:
            Path(str(organized) + ".geolens.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

    def _move_source_if_requested(self, source: Path, cfg: dict[str, Any]) -> None:
        if cfg.get("mode") != "move":
            return
        if source.is_file():
            try:
                source.unlink()
            except OSError:
                pass
