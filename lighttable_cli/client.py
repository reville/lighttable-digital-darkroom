"""Small urllib client for the local LightTable server."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
import uuid


class ClientError(RuntimeError):
    def __init__(self, status: int, payload: dict | str) -> None:
        self.status = status
        self.payload = payload
        self.code = payload.get("code") if isinstance(payload, dict) else None
        self.field = payload.get("field") if isinstance(payload, dict) else None
        message = payload.get("error") if isinstance(payload, dict) else str(payload)
        super().__init__(message or f"HTTP {status}")


class Client:
    def __init__(self, url: str, *, token: str = "", strict: bool = True,
                 origin: str = "cli", timeout: float = 30.0) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.strict = strict
        self.origin = origin
        self.timeout = timeout
        self.client_id = f"cli-{uuid.uuid4().hex[:12]}"

    def _request(self, method: str, path: str, body: dict | None = None,
                 *, accept: str = "application/json"):
        headers = {"Accept": accept, "X-LightTable-Client": self.client_id}
        data = None
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
            if self.strict:
                headers["X-LightTable-Strict"] = "1"
            if self.token:
                headers["X-LightTable-Token"] = self.token
        request = urllib.request.Request(
            self.url + (path if path.startswith("/") else "/" + path),
            data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read(), response.headers.get_content_type()
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                payload = json.loads(raw)
            except (ValueError, TypeError):
                payload = raw.decode("utf-8", "replace")
            raise ClientError(error.code, payload) from error
        except urllib.error.URLError as error:
            raise ClientError(0, {"error": str(error.reason),
                                  "code": "no-server"}) from error

    def get(self, path: str) -> dict:
        raw, _ = self._request("GET", path)
        value = json.loads(raw)
        return value

    def post(self, path: str, body: dict | None = None) -> dict:
        raw, _ = self._request("POST", path, body or {})
        return json.loads(raw)

    def bytes(self, path: str, body: dict | None = None) -> tuple[bytes, str]:
        return self._request("POST" if body is not None else "GET", path, body,
                             accept="image/*,application/octet-stream")


def query(endpoint: str, **values) -> str:
    present = {key: value for key, value in values.items() if value is not None}
    return endpoint + ("?" + urllib.parse.urlencode(present) if present else "")
