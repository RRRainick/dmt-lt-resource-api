from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

from .models import NodeConfig, NodeResult, RequestIdCounter, now_ms

logger = logging.getLogger(__name__)


class NodeSystemError(RuntimeError):
    """课题二 node 接口调用失败。"""


class NodeSystem:
    """负责调用 node 的资源接口。接口保持同步，调用在线程中执行。"""

    def __init__(
        self,
        nodes: list[NodeConfig],
        timeout_seconds: float = 3.0,
        capacities: dict[str, dict[str, float]] | None = None,
        request_id_start: int = 1,
    ) -> None:
        self.nodes = {node.node_id: node for node in nodes}
        self.timeout_seconds = timeout_seconds
        self.capacities = capacities or {}
        self._reported_capacities: dict[str, dict[str, float]] = {}
        self._capacity_lock = Lock()
        self._opener = build_opener(ProxyHandler({}))
        self._status_request_ids = RequestIdCounter(request_id_start)
        self._config_request_ids = RequestIdCounter(request_id_start)

    def read_status(self, node_id: str) -> dict[str, Any]:
        node = self._get_node(node_id)
        request_id = self._status_request_ids.next()
        query = urlencode({"node_id": node_id, "request_id": request_id})
        request = Request(
            f"{node.base_url.rstrip('/')}/resource/status?{query}",
            method="GET",
            headers={"Accept": "application/json"},
        )
        response = self._send_json(request)
        self._check_response(response, "资源读取")
        self._check_request_id(response, request_id, "资源读取")
        self._remember_reported_capacity(node_id, response)
        return response

    def read_all_status(self) -> list[NodeResult]:
        """并行读取所有 node，单个 node 失败不影响其他 node。"""

        results: list[NodeResult] = []
        with ThreadPoolExecutor(max_workers=max(1, len(self.nodes))) as pool:
            futures = {
                pool.submit(self.read_status, node_id): node_id
                for node_id in self.nodes
            }
            for future, node_id in futures.items():
                try:
                    results.append(NodeResult(node_id, True, future.result()))
                except Exception as error:  # noqa: BLE001
                    results.append(NodeResult(node_id, False, error=str(error)))
        return results

    def configure_all(self, policy: dict[str, Any]) -> list[NodeResult]:
        """根据课题四返回的 policy_list 并行配置 node。"""

        policy_list = policy.get("policy_list", [])
        if not isinstance(policy_list, list):
            raise NodeSystemError("policy_list 不是数组")

        results: list[NodeResult] = []
        with ThreadPoolExecutor(max_workers=max(1, len(policy_list))) as pool:
            futures = {}
            for item in policy_list:
                if not isinstance(item, dict):
                    logger.warning("跳过格式错误的 node 配置策略：%s", item)
                    continue
                node_id = str(item.get("node_id", ""))
                if node_id not in self.nodes:
                    continue
                futures[pool.submit(self.configure, node_id, item)] = node_id

            for future, node_id in futures.items():
                try:
                    results.append(NodeResult(node_id, True, future.result()))
                except Exception as error:  # noqa: BLE001
                    logger.warning("配置 %s 失败：%s", node_id, error)
                    results.append(NodeResult(node_id, False, error=str(error)))
        return results

    def configure(self, node_id: str, node_policy: dict[str, Any]) -> dict[str, Any]:
        node = self._get_node(node_id)
        body = self._build_config_body(node_id, node_policy)
        modalities = body["modalities_resource"]
        first_mode = modalities[0] if modalities else {}
        mode_name = str(first_mode.get("modality", "none"))
        mode_config = {
            key: first_mode[key]
            for key in (
                "compute_config_percent",
                "storage_config_mb",
                "forwarding_config_mbps",
            )
            if key in first_mode
        }
        logger.debug(
            "2/4 下发资源配置：node_id=%s request_id=%s %s=%s",
            node_id,
            body["request_id"],
            mode_name,
            json.dumps(mode_config, ensure_ascii=False),
        )
        request = Request(
            f"{node.base_url.rstrip('/')}/resource/config",
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        response = self._send_json(request)
        logger.debug(
            "3/4 收到 node 响应：node_id=%s request_id=%s code=%s msg=%s data=%s",
            node_id,
            body["request_id"],
            response.get("code"),
            response.get("msg"),
            json.dumps(response.get("data"), ensure_ascii=False),
        )
        self._check_response(response, "资源配置")
        self._check_request_id(response, body["request_id"], "资源配置")
        return response

    def _build_config_body(
        self,
        node_id: str,
        node_policy: dict[str, Any],
    ) -> dict[str, Any]:
        modes = node_policy.get("mode_list", [])
        capacity = self._capacity_for(node_id)
        needs_storage = any(mode.get("storage_config_mb") is None for mode in modes)
        needs_forwarding = any(
            mode.get("forwarding_config_mbps") is None for mode in modes
        )
        if (
            (needs_storage and capacity.get("storage_mb") is None)
            or (needs_forwarding and capacity.get("forwarding_mbps") is None)
        ):
            # 配置线程可能早于首轮监测运行，按需读取一次Agent容量。
            self.read_status(node_id)
            capacity = self._capacity_for(node_id)

        modalities: list[dict[str, Any]] = []
        for mode in modes:
            mode_id = mode.get("mode_id") or mode.get("modality")
            if not mode_id:
                raise NodeSystemError(f"{node_id} 的策略缺少 mode_id")

            storage_mb = mode.get("storage_config_mb")
            forwarding_mbps = mode.get("forwarding_config_mbps")
            if storage_mb is None:
                storage_mb = self._ratio_to_value(
                    mode.get("mem_ratio"), capacity.get("storage_mb"), "存储"
                )
            if forwarding_mbps is None:
                forwarding_mbps = self._ratio_to_value(
                    mode.get("trans_ratio"),
                    capacity.get("forwarding_mbps"),
                    "转发",
                )

            cpu_percent = mode.get("compute_config_percent")
            if cpu_percent is None:
                cpu_ratio = mode.get("CPU_ratio", mode.get("cpu_ratio"))
                cpu_percent = self._ratio_to_value(cpu_ratio, 100.0, "计算")

            modalities.append(
                {
                    "modality": mode_id,
                    "compute_config_percent": float(cpu_percent),
                    "storage_config_mb": float(storage_mb),
                    "forwarding_config_mbps": float(forwarding_mbps),
                }
            )

        return {
            "timestamp_ms": now_ms(),
            "request_id": self._config_request_ids.next(),
            "node_id": node_id,
            "modalities_resource": modalities,
        }

    def _remember_reported_capacity(
        self,
        node_id: str,
        response: dict[str, Any],
    ) -> None:
        data = response.get("data")
        node_resource = data.get("node_resource") if isinstance(data, dict) else None
        if not isinstance(node_resource, dict):
            return

        reported: dict[str, float] = {}
        storage = node_resource.get("storage_capacity_mb")
        forwarding = node_resource.get("forwarding_capacity_mbps")
        if (
            isinstance(storage, (int, float))
            and not isinstance(storage, bool)
            and storage > 0
        ):
            reported["storage_mb"] = float(storage)
        if (
            isinstance(forwarding, (int, float))
            and not isinstance(forwarding, bool)
            and forwarding > 0
        ):
            reported["forwarding_mbps"] = float(forwarding)
        if reported:
            with self._capacity_lock:
                self._reported_capacities[node_id] = reported

    def _capacity_for(self, node_id: str) -> dict[str, float]:
        capacity = {
            key: float(value)
            for key, value in self.capacities.get(node_id, {}).items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }
        with self._capacity_lock:
            capacity.update(self._reported_capacities.get(node_id, {}))
        return capacity

    @staticmethod
    def _ratio_to_value(
        ratio: Any,
        capacity: Any,
        resource_name: str,
    ) -> float:
        if ratio is None:
            raise NodeSystemError(f"缺少{resource_name}资源配置值")
        if capacity is None:
            raise NodeSystemError(f"缺少{resource_name}总容量，无法换算比例")
        return float(ratio) * float(capacity) if float(ratio) <= 1 else float(ratio)

    def _get_node(self, node_id: str) -> NodeConfig:
        if node_id not in self.nodes:
            raise NodeSystemError(f"未配置节点：{node_id}")
        return self.nodes[node_id]

    def _send_json(self, request: Request) -> dict[str, Any]:
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            raise NodeSystemError(f"HTTP {error.code}：{body}") from error
        except (URLError, TimeoutError) as error:
            raise NodeSystemError(f"node 请求失败：{error}") from error

        try:
            data = json.loads(body)
        except json.JSONDecodeError as error:
            raise NodeSystemError(f"node 返回的不是合法 JSON：{body}") from error
        if not isinstance(data, dict):
            raise NodeSystemError("node 返回的 JSON 不是对象")
        return data

    @staticmethod
    def _check_response(response: dict[str, Any], action: str) -> None:
        if response.get("code") != 0:
            raise NodeSystemError(
                f"{action}失败：code={response.get('code')}，msg={response.get('msg', '')}"
            )

    @staticmethod
    def _check_request_id(
        response: dict[str, Any],
        expected: int,
        action: str,
    ) -> None:
        data = response.get("data")
        actual = data.get("request_id") if isinstance(data, dict) else None
        if str(actual) != str(expected):
            raise NodeSystemError(
                f"{action}返回 request_id 不一致：expected={expected}，actual={actual}"
            )
