"""Parallel ranged HTTP downloader.

The network here throttles single-connection streaming to ~8 KB/s but allows
many concurrent range requests, so large artifacts are fetched in chunks.
"""
from __future__ import annotations

import concurrent.futures as cf
import os
import sys
import time
import urllib.request

CHUNK = 4 * 1024 * 1024
CONNECTIONS = 16


def _size(url: str) -> int:
    req = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(req, timeout=60) as r:
        length = r.headers.get("Content-Length")
        return int(length) if length else 0


def _fetch(url: str, start: int, end: int, path: str) -> int:
    for attempt in range(5):
        try:
            req = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}"})
            with urllib.request.urlopen(req, timeout=120) as r:
                data = r.read()
            if len(data) != end - start + 1:
                raise IOError(f"short read {len(data)} != {end - start + 1}")
            with open(path, "wb") as f:
                f.write(data)
            return len(data)
        except Exception:
            if attempt == 4:
                raise
            time.sleep(2 * (attempt + 1))
    return 0


def main(url: str, dest: str) -> None:
    total = _size(url)
    if total <= 0:
        raise SystemExit("server did not report a size")
    tmpdir = dest + ".parts"
    os.makedirs(tmpdir, exist_ok=True)
    ranges = [(i * CHUNK, min(total - 1, (i + 1) * CHUNK - 1)) for i in range((total + CHUNK - 1) // CHUNK)]
    done = 0
    t0 = time.time()
    with cf.ThreadPoolExecutor(CONNECTIONS) as ex:
        futs = {
            ex.submit(_fetch, url, s, e, os.path.join(tmpdir, f"{i:05d}")): i
            for i, (s, e) in enumerate(ranges)
        }
        for fut in cf.as_completed(futs):
            done += fut.result()
            if done % (32 * 1024 * 1024) < CHUNK:
                rate = done / max(1e-9, time.time() - t0) / 1024
                print(f"  {done / 1048576:.1f}/{total / 1048576:.1f} MB  {rate:.0f} KB/s", flush=True)
    with open(dest, "wb") as out:
        for i in range(len(ranges)):
            with open(os.path.join(tmpdir, f"{i:05d}"), "rb") as part:
                out.write(part.read())
    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)
    got = os.path.getsize(dest)
    print(f"downloaded {dest}: {got} bytes (expected {total})")
    if got != total:
        raise SystemExit(1)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
