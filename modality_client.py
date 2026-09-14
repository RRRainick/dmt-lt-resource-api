"""Standard-library HTTP client for resource and modality operations.

Methods return HttpResult even for HTTP/business errors; callers decide policy.
"""
from __future__ import annotations

import ipaddress
import json
import socket
import time
from dataclasses import dataclass
from http.client import HTTPException
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

JSON_MISSING = object()


class TransportFailure(RuntimeError):
    """The target could not be reached or its response could not be read."""


@dataclass(frozen=True)
class HttpResult:
    status: int
    content_type: str
    body: str
    payload: Any
    json_error: str | None
    elapsed_ms: float


class RequestIds:
    """Generate unique, process-local integer request IDs."""

    def __init__(self) -> None:
        self._next = int(time.time() * 1000) % 2_000_000_000

    def next(self) -> int:
        value = self._next
        self._next += 1
        return value


class ApiClient:
    def __init__(self, base_url: str, timeout: float = 3.0, verbose: bool = False) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.verbose = verbose
        # Do not let HTTP(S)_PROXY redirect node-local integration requests.
        self.opener = build_opener(ProxyHandler({}))

    def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        json_body: Any = JSON_MISSING,
        raw_body: bytes | None = None,
    ) -> HttpResult:
        if json_body is not JSON_MISSING and raw_body is not None:
            raise ValueError("json_body 和 raw_body 不能同时设置")

        url = f"{self.base_url}{path}"
        if query is not None:
            url = f"{url}?{urlencode(query)}"

        headers = {"Accept": "application/json"}
        data = raw_body
        printable_body: Any = None
        if json_body is not JSON_MISSING:
            printable_body = json_body
            data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        elif raw_body is not None:
            printable_body = raw_body.decode("utf-8", errors="replace")
        if data is not None:
            headers["Content-Type"] = "application/json"

        if self.verbose:
            print(f"       -> {method} {url}")
            if data is not None:
                print(_pretty_json("          request: ", printable_body))

        request = Request(url, data=data, method=method, headers=headers)
        started = time.perf_counter()
        try:
            try:
                response = self.opener.open(request, timeout=self.timeout)
            except HTTPError as error:
                response = error
            with response:
                status = response.status
                content_type = response.headers.get("Content-Type", "")
                body = response.read().decode("utf-8", errors="replace")
        except (URLError, TimeoutError, socket.timeout, OSError, HTTPException) as error:
            elapsed = (time.perf_counter() - started) * 1000
            raise TransportFailure(
                f"{method} {url} 请求失败（{elapsed:.1f} ms）：{error}"
            ) from error

        elapsed = (time.perf_counter() - started) * 1000
        payload: Any = None
        json_error: str | None = None
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as error:
            json_error = str(error)

        result = HttpResult(
            status=status,
            content_type=content_type,
            body=body,
            payload=payload,
            json_error=json_error,
            elapsed_ms=elapsed,
        )
        if self.verbose:
            print(
                f"       <- HTTP {status} ({elapsed:.1f} ms) "
                f"Content-Type={content_type or '<missing>'}"
            )
            print(_pretty_json("          response: ", payload if json_error is None else body))
        return result


def _pretty_json(prefix: str, value: Any) -> str:
    if isinstance(value, str):
        rendered = value
    else:
        rendered = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    return prefix + rendered.replace("\n", f"\n{' ' * len(prefix)}")


def deploy_body(
    request_id: int,
    node_id: str,
    modality: str,
    compute: float,
    storage: float,
    forwarding: float,
) -> dict[str, Any]:
    return {
        "timestamp_ms": int(time.time() * 1000),
        "request_id": request_id,
        "node_id": node_id,
        "modality": modality,
        "compute_config_percent": compute,
        "storage_config_mb": storage,
        "forwarding_config_mbps": forwarding,
    }


def delete_body(request_id: int, node_id: str, modality: str) -> dict[str, Any]:
    return {
        "timestamp_ms": int(time.time() * 1000),
        "request_id": request_id,
        "node_id": node_id,
        "modality": modality,
    }


def build_base_url(scheme: str, host: str, port: int) -> str:
    clean_host = host.strip()
    if clean_host.startswith("[") and clean_host.endswith("]"):
        clean_host = clean_host[1:-1]
    if not clean_host:
        raise ValueError("IP/主机名不能为空")
    if any(character in clean_host for character in "/?#"):
        raise ValueError("--ip 只能是 IP 地址或主机名，不能包含 URL 路径")

    try:
        address = ipaddress.ip_address(clean_host)
    except ValueError:
        rendered_host = clean_host
    else:
        rendered_host = f"[{clean_host}]" if address.version == 6 else clean_host
    return f"{scheme}://{rendered_host}:{port}"


class ModalityClient(ApiClient):
    """Execute one operation per call, without cleanup or automatic retries."""

    def __init__(self, base_url: str, timeout: float = 3.0, verbose: bool = False) -> None:
        super().__init__(base_url, timeout, verbose)
        self._ids = RequestIds()

    def deploy(
        self, node_id: str, modality: str, *,
        compute_config_percent: float = 10.0,
        storage_config_mb: float = 128.0,
        forwarding_config_mbps: float = 10.0,
        request_id: int | None = None,
    ) -> HttpResult:
        body = deploy_body(
            self._ids.next() if request_id is None else request_id,
            node_id, modality, compute_config_percent,
            storage_config_mb, forwarding_config_mbps,
        )
        return self.request("POST", "/modality/deploy", json_body=body)

    def delete(
        self, node_id: str, modality: str, *, request_id: int | None = None,
    ) -> HttpResult:
        body = delete_body(
            self._ids.next() if request_id is None else request_id, node_id, modality,
        )
        return self.request("POST", "/modality/delete", json_body=body)
