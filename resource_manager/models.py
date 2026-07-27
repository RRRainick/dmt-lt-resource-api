from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from typing import Any


@dataclass(frozen=True)
class NodeConfig:
    node_id: str
    base_url: str


@dataclass
class NodeResult:
    node_id: str
    success: bool
    data: dict[str, Any] | None = None
    error: str | None = None


class RequestIdCounter:
    """为一个接口维护独立的递增 request_id。"""

    def __init__(self, start: int = 1) -> None:
        self._next_id = start
        self._lock = Lock()

    def next(self) -> int:
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            return request_id


def now_ms() -> int:
    """返回 Unix 毫秒时间戳。"""

    import time

    return int(time.time() * 1000)
