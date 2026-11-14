"""REST client for the local GeoLens API (stdlib only)."""
from __future__ import annotations

import json
import mimetypes
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path


class ApiError(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body[:500]}")
        self.status = status
        self.body = body


def _multipart(field: str, path: Path, filename: str) -> tuple[bytes, str]:
    boundary = f"----geolens{uuid.uuid4().hex}"
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
        f"Content-Type: {ctype}\r\n\r\n"
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    return head + path.read_bytes() + tail, f"multipart/form-data; boundary={boundary}"


@dataclass
class JobState:
    id: str
    status: str
    dataset_id: str | None = None
    error: str | None = None


TERMINAL = {"complete", "completed", "success", "succeeded", "failed", "error", "cancelled", "fatal"}


class GeoLensClient:
    def __init__(self, base_url: str, username: str, password: str, timeout: int = 1800) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self.access_token: str | None = None

    def _request(self, method: str, path: str, *, data: bytes | None = None,
                 headers: dict[str, str] | None = None) -> tuple[int, bytes]:
        url = f"{self.base_url}{path}"
        hdrs = {"Accept": "application/json"}
        if self.access_token:
            hdrs["Authorization"] = f"Bearer {self.access_token}"
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def _json(self, method: str, path: str, payload: dict | None = None) -> dict:
        data = None
        headers: dict[str, str] = {}
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        status, body = self._request(method, path, data=data, headers=headers)
        if status >= 400:
            raise ApiError(status, body.decode("utf-8", "replace"))
        return json.loads(body) if body else {}

    def login(self) -> None:
        form = urllib.parse.urlencode({"username": self.username, "password": self.password}).encode()
        status, body = self._request(
            "POST", "/auth/login/", data=form,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if status >= 400:
            raise ApiError(status, body.decode("utf-8", "replace"))
        self.access_token = json.loads(body)["access_token"]

    def upload(self, path: Path, *, kind: str | None = None) -> str:
        data, ctype = _multipart("file", path, path.name)
        status, body = self._request("POST", "/ingest/upload", data=data,
                                     headers={"Content-Type": ctype})
        if status >= 400:
            raise ApiError(status, body.decode("utf-8", "replace"))
        return json.loads(body)["job_id"]

    def preview(self, job_id: str, layer_name: str | None = None) -> dict:
        query = f"?layer_name={urllib.parse.quote(layer_name)}" if layer_name else ""
        return self._json("POST", f"/ingest/preview/{job_id}{query}")

    def commit(self, job_id: str, payload: dict) -> dict:
        return self._json("POST", f"/ingest/commit/{job_id}", payload)

    def job(self, job_id: str) -> JobState:
        data = self._json("GET", f"/jobs/{job_id}")
        return JobState(
            id=str(data.get("id", job_id)),
            status=str(data.get("status", "")),
            dataset_id=(str(data["dataset_id"]) if data.get("dataset_id") else None),
            error=data.get("error") or data.get("error_message"),
        )

    def wait_for_job(self, job_id: str, timeout: int = 1800, interval: float = 2.0) -> JobState:
        deadline = time.time() + timeout
        state = JobState(id=job_id, status="pending")
        while time.time() < deadline:
            state = self.job(job_id)
            if state.status in TERMINAL:
                return state
            time.sleep(interval)
        return state

    def ingest_file(self, path: Path, *, title: str, summary: str | None = None,
                    wait: bool = True, timeout: int = 1800) -> dict:
        job_id = self.upload(path)
        self.preview(job_id)
        payload: dict = {"title": title, "visibility": "private"}
        if summary:
            payload["summary"] = summary
        commit = self.commit(job_id, payload)
        if wait:
            state = self.wait_for_job(job_id, timeout=timeout)
            return {"job_id": job_id, "status": state.status,
                    "dataset_id": state.dataset_id, "error": state.error}
        return {**commit, "job_id": job_id, "status": "pending"}
