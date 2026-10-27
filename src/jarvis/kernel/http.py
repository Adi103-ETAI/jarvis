"""Kernel transport -- stdlib HTTP plumbing for the locked API surface.

No policy and no API semantics live here: this is auth + one-request +
envelope unwrap + SSE line parsing. The operation methods (the stable
ABI) are on jarvis.kernel.adapter.Kernel, which inherits KernelTransport.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from typing import Any, Iterator, Optional

from jarvis.kernel.config import resolve_base, resolve_password
from jarvis.kernel.errors import KernelError


class KernelTransport:
    """Owns base URL + auth; performs one HTTP call per locked operation."""

    def __init__(self, base_url: Optional[str] = None, password: Optional[str] = None):
        self.base_url = resolve_base(base_url)
        pw = resolve_password(password)
        token = base64.b64encode(f"opencode:{pw}".encode()).decode()
        self._auth = f"Basic {token}"

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[dict] = None,
        timeout: float = 30.0,
        stream: bool = False,
    ):
        """One HTTP call to one locked operation. Returns parsed JSON, or None on 204."""
        url = f"{self.base_url}{path}"
        data = None
        headers = {"Authorization": self._auth, "Accept": "text/event-stream" if stream else "application/json"}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            response = urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as e:
            payload = e.read()
            tag, message = "", e.reason
            try:
                parsed = json.loads(payload)
                tag = parsed.get("_tag", "")
                message = parsed.get("message", message)
            except Exception:  # noqa: BLE001
                parsed = {}
            raise KernelError(message or f"HTTP {e.code}", status=e.code, tag=tag) from None
        except urllib.error.URLError as e:
            raise KernelError(f"kernel unreachable at {self.base_url}: {e.reason}", status=0) from None

        if stream:
            return response  # caller owns the file-like SSE stream
        if response.status == 204 or not response.readable():
            return None
        raw = response.read()
        if not raw.strip():
            return None
        return json.loads(raw)

    @staticmethod
    def _data(payload: Any) -> Any:
        """Kernel envelopes success as {data: ...}. Unwrap it once, here, everywhere."""
        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload

    @staticmethod
    def iter_sse(resp) -> Iterator[dict]:
        """Parse an open SSE response into event dicts (caller closes resp)."""
        data_lines: list[str] = []
        for line in resp:
            text = line.decode("utf-8", "replace").rstrip("\r\n")
            if not text:
                if data_lines:
                    blob = "".join(data_lines)
                    data_lines = []
                    try:
                        yield json.loads(blob)
                    except json.JSONDecodeError:
                        continue
                continue
            if text.startswith("data:"):
                data_lines.append(text[len("data:"):].lstrip())
