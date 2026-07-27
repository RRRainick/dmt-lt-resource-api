"""用于接口联调的轻量资源节点 Agent。

仅实现资源协议：资源读取返回固定的资源快照，资源配置直接确认成功，
不会修改 cgroup、TC 或其他系统资源。
"""

from __future__ import annotations

import json
import os
import socket
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _enabled_node() -> dict[str, Any]:
    """读取 config.json 中唯一启用的节点配置。"""

    config_path = Path(
        os.getenv("RESOURCE_MANAGER_CONFIG", str(Path(__file__).with_name("config.json")))
    )
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    nodes = config.get("nodes", []) if isinstance(config, dict) else []
    enabled_nodes = [
        item
        for item in nodes
        if isinstance(item, dict) and item.get("enabled") is True
    ]
    hostname = socket.gethostname()
    matched_nodes = [
        item
        for item in enabled_nodes
        if str(item.get("node_id", "")).casefold() == hostname.casefold()
    ]
    if matched_nodes:
        return matched_nodes[0]
    return enabled_nodes[0] if len(enabled_nodes) == 1 else {}


NODE_CONFIG = _enabled_node()
_base_url = urlparse(str(NODE_CONFIG.get("base_url", "")))
HOST = os.getenv(
    "RESOURCE_AGENT_NODE_IP",
    _base_url.hostname or os.getenv("RESOURCE_AGENT_HOST", "0.0.0.0"),
)
PORT = int(os.getenv("RESOURCE_AGENT_PORT", str(_base_url.port or 8000)))
NODE_ID = os.getenv("RESOURCE_AGENT_NODE_ID", str(NODE_CONFIG.get("node_id") or socket.gethostname()))
COMPUTE_USAGE_PERCENT = _float_env("RESOURCE_AGENT_COMPUTE_USAGE_PERCENT", 0.0)
STORAGE_USAGE_MB = _float_env("RESOURCE_AGENT_STORAGE_USAGE_MB", 0.0)
FORWARDING_USAGE_MBPS = _float_env("RESOURCE_AGENT_FORWARDING_USAGE_MBPS", 0.0)
STORAGE_CAPACITY_MB = _float_env("RESOURCE_AGENT_STORAGE_CAPACITY_MB", 67015.192576)
FORWARDING_CAPACITY_MBPS = _float_env("RESOURCE_AGENT_FORWARDING_CAPACITY_MBPS", 10000.0)
MODALITIES = tuple(
    mode.strip().lower()
    for mode in os.getenv("RESOURCE_AGENT_MODALITIES", "ipv4,srv6").split(",")
    if mode.strip()
)


def _write_json(handler: BaseHTTPRequestHandler, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _status_response(node_id: str, request_id: str) -> dict[str, Any]:
    if node_id != NODE_ID or not request_id.isdecimal():
        return {
            "code": 1,
            "msg": "apply resource query failed",
            "data": {"request_id": request_id},
        }

    return {
        "code": 0,
        "msg": "ok",
        "data": {
            "request_id": int(request_id),
            "timestamp_ms": int(time.time() * 1000),
            "node_id": NODE_ID,
            "node_resource": {
                "compute_usage_percent": COMPUTE_USAGE_PERCENT,
                "storage_usage_mb": STORAGE_USAGE_MB,
                "forwarding_usage_mbps": FORWARDING_USAGE_MBPS,
                # 资源管理器用这两个扩展字段把策略比例换算为绝对配置值。
                "storage_capacity_mb": STORAGE_CAPACITY_MB,
                "forwarding_capacity_mbps": FORWARDING_CAPACITY_MBPS,
            },
            "modalities_resource": [
                {
                    "modality": modality,
                    "compute_usage_percent": 0.0,
                    "storage_usage_mb": 0.0,
                    "forwarding_usage_mbps": 0.0,
                }
                for modality in MODALITIES
            ],
        },
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        request = urlparse(self.path)
        if request.path != "/resource/status":
            self.send_error(404)
            return

        query = parse_qs(request.query)
        _write_json(
            self,
            _status_response(
                query.get("node_id", [""])[0],
                query.get("request_id", [""])[0],
            ),
        )

    def do_POST(self) -> None:
        request = urlparse(self.path)
        if request.path != "/resource/config":
            self.send_error(404)
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(content_length).decode("utf-8") if content_length else "{}"
        try:
            config = json.loads(raw)
        except json.JSONDecodeError:
            config = {}
        request_id = config.get("request_id") if isinstance(config, dict) else None

        # 仅用于接口联调：确认收到配置，但不执行任何资源变更。
        _write_json(
            self,
            {"code": 0, "msg": "ok", "data": {"request_id": request_id}},
        )

    def log_message(self, *_: Any) -> None:
        pass


def main() -> None:
    print(f"[resource-agent] running at http://{HOST}:{PORT}; node_id={NODE_ID}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
