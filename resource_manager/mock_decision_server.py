from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


POLICY_MODES = (
    {"mode_id": "ipv4", "CPU_ratio": 0.5, "mem_ratio": 0.5, "trans_ratio": 0.5},
    {"mode_id": "srv6", "CPU_ratio": 0.5, "mem_ratio": 0.5, "trans_ratio": 0.5},
)

FINISHED = threading.Event()
POLICY_SENT = threading.Event()
POLICY_LOCK = threading.Lock()
IMPL_STATUS_COUNT = 0
ONCE_MODE = False


def read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    content_length = int(handler.headers.get("Content-Length", "0"))
    raw = handler.rfile.read(content_length).decode("utf-8") if content_length else "{}"
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _policy_list() -> list[dict[str, Any]]:
    """为 config.json 中启用的节点生成模拟资源配置策略。"""

    config_path = Path(
        os.getenv("RESOURCE_MANAGER_CONFIG", str(Path(__file__).with_name("config.json")))
    )
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    nodes = config.get("nodes", []) if isinstance(config, dict) else []
    return [
        {
            "node_id": str(node["node_id"]),
            "mode_list": [dict(mode) for mode in POLICY_MODES],
        }
        for node in nodes
        if isinstance(node, dict)
        and node.get("enabled") is True
        and str(node.get("node_id", "")).strip()
    ]


def write_json(handler: BaseHTTPRequestHandler, response: dict[str, Any]) -> None:
    body = json.dumps(response, ensure_ascii=False).encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def write_no_content(handler: BaseHTTPRequestHandler) -> None:
    handler.send_response(204)
    handler.end_headers()


def _node_result_summary(body: dict[str, Any]) -> tuple[int, int]:
    """返回 manager 回传的节点配置成功、失败数量。"""

    detail = body.get("data", {}).get("detail", {})
    node_results = detail.get("node_results", []) if isinstance(detail, dict) else []
    if not isinstance(node_results, list):
        return 0, 0
    success_count = sum(
        isinstance(item, dict) and item.get("success") is True
        for item in node_results
    )
    return success_count, len(node_results) - success_count


class MockDecisionHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        body = read_json(self)
        if self.path == "/getNextResCfg":
            request_id = str(body.get("data", {}).get("request_id", ""))
            policy_list = _policy_list()
            with POLICY_LOCK:
                already_sent = ONCE_MODE and POLICY_SENT.is_set()
                if not already_sent:
                    POLICY_SENT.set()
            if already_sent:
                response = {
                    "code": 1,
                    "msg": "once mode already sent a policy",
                    "data": {"request_id": request_id, "policy_list": []},
                }
            else:
                response = {
                    "code": 0,
                    "msg": "ok",
                    "data": {
                        "request_id": request_id,
                        "policy_list": policy_list,
                    },
                }
            if already_sent:
                print(
                    f"[mock-decision] 1/4 单次策略已发送，拒绝重复请求：request_id={request_id}",
                    flush=True,
                )
            else:
                node_ids = ",".join(item["node_id"] for item in policy_list)
                print(
                    f"[mock-decision] 1/4 收到策略请求：request_id={request_id}",
                    flush=True,
                )
                print(
                    f"[mock-decision] 1/4 返回资源配置策略：nodes={node_ids}",
                    flush=True,
                )
            write_json(self, response)
            return

        if self.path == "/postImplStatus":
            global IMPL_STATUS_COUNT
            IMPL_STATUS_COUNT += 1
            request_id = str(body.get("data", {}).get("request_id", ""))
            exec_status = body.get("data", {}).get("exec_status", "unknown")
            success_count, failed_count = _node_result_summary(body)
            print(
                "[mock-decision] 4/4 收到资源配置结果："
                f"request_id={request_id} status={exec_status} "
                f"success={success_count} failed={failed_count}",
                flush=True,
            )
            write_no_content(self)
            FINISHED.set()
            return

        self.send_error(404)

    def log_message(self, *_: Any) -> None:
        pass


def run(*, once: bool = False) -> None:
    """启动 mock 决策服务；单次模式收到执行结果后自动退出。"""

    global IMPL_STATUS_COUNT, ONCE_MODE
    FINISHED.clear()
    POLICY_SENT.clear()
    IMPL_STATUS_COUNT = 0
    ONCE_MODE = once
    host = os.getenv("MOCK_DECISION_HOST", "0.0.0.0")
    port = int(os.getenv("MOCK_DECISION_PORT", "9000"))
    server = ThreadingHTTPServer((host, port), MockDecisionHandler)
    server.daemon_threads = True

    print(
        f"[mock-decision] running at http://{host}:{port}; "
        "waiting for manager workflow...",
        flush=True,
    )
    try:
        if once:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            FINISHED.wait()
            print(
                "[mock-decision] 单次资源配置流程结束",
                flush=True,
            )
        else:
            print("[mock-decision] press Ctrl+C to stop", flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        print("[mock-decision] stopped", flush=True)
    server.shutdown()
    server.server_close()


def main() -> None:
    once = os.getenv("MOCK_DECISION_ONCE", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    run(once=once)


if __name__ == "__main__":
    main()
