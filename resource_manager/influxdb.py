from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from threading import Lock
from typing import Any

from influxdb import InfluxDBClient

logger = logging.getLogger(__name__)

_RESOURCE_VALUE_NAMES = {
    "compute": ("compute", "compute_usage_percent", "compute_config_percent"),
    "storage": ("storage", "storage_usage_mb", "storage_config_mb"),
    "forwarding": (
        "forwarding",
        "forwarding_usage_mbps",
        "forwarding_config_mbps",
    ),
    "storage_capacity_mb": ("storage_capacity_mb", "storage_capacity"),
    "forwarding_capacity_mbps": (
        "forwarding_capacity_mbps",
        "forwarding_capacity",
    ),
}


def _resource_values(values: Any) -> dict[str, float]:
    if not isinstance(values, dict):
        return {}

    result: dict[str, float] = {}
    for target, names in _RESOURCE_VALUE_NAMES.items():
        for name in names:
            value = values.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                result[target] = float(value)
                break
    return result


def _mode_resources(values: Any) -> dict[str, dict[str, float]]:
    if not isinstance(values, list):
        return {}

    result: dict[str, dict[str, float]] = {}
    for item in values:
        if not isinstance(item, dict):
            continue
        mode_id = str(item.get("modality", "")).strip().lower()
        resources = _resource_values(item)
        if mode_id and resources:
            result[mode_id] = resources
    return result


def _parse_snapshot(response: dict[str, Any]) -> dict[str, Any] | None:
    """从 node 响应提取两个存储后端共用的资源快照。"""

    if response.get("code") not in (None, 0):
        return None

    data = response.get("data", response)
    if not isinstance(data, dict):
        logger.warning("资源快照 data 不是对象，跳过写入")
        return None

    timestamp_ms = data.get("timestamp_ms")
    node_id = data.get("node_id")
    if not isinstance(timestamp_ms, (int, float)) or timestamp_ms <= 0:
        logger.warning("资源快照缺少有效 timestamp_ms，跳过写入")
        return None
    if node_id is None or not str(node_id):
        logger.warning("资源快照缺少 node_id，跳过写入")
        return None

    node_resource = _resource_values(data.get("node_resource"))
    if not node_resource:
        logger.warning("资源快照缺少 node_resource，跳过写入")
        return None

    return {
        "timestamp_ms": int(timestamp_ms),
        "node_id": str(node_id),
        "node_resource": node_resource,
        "modalities_resource": _mode_resources(data.get("modalities_resource")),
    }


def _influx_mode_resources(
    resources_by_mode: dict[str, dict[str, float]],
) -> dict[str, dict[str, float]]:
    """转换为既有 resource.mode_resource_list 的字段结构。"""

    return {
        mode_id: {
            "cpu_ratio": round(float(resources.get("compute", 0.0)) / 100.0, 6),
            "mem_util_ratio": float(resources.get("storage", 0.0)),
            "trans_util_ratio": float(resources.get("forwarding", 0.0)),
        }
        for mode_id, resources in resources_by_mode.items()
    }


def _ensure_finite_numbers(fields: dict[str, Any]) -> None:
    for key, value in fields.items():
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"InfluxDB field {key} 不是有限数值")


def _legacy_resource_fields(snapshot: dict[str, Any]) -> dict[str, Any]:
    """将 node 通用资源名转换为既有 resource measurement 字段。"""

    node_resource = snapshot["node_resource"]
    fields = {
        "node_id": snapshot["node_id"],
        "cpu_ratio": float(node_resource.get("compute", 0.0)) / 100.0,
        "mem_max": float(node_resource.get("storage_capacity_mb", 0.0)),
        "mem_util_ratio": float(node_resource.get("storage", 0.0)),
        "trans_max": float(node_resource.get("forwarding_capacity_mbps", 0.0)),
        "trans_util_ratio": float(node_resource.get("forwarding", 0.0)),
        "mode_resource_list": json.dumps(
            _influx_mode_resources(snapshot["modalities_resource"]),
            ensure_ascii=False,
        ),
    }
    _ensure_finite_numbers(fields)
    return fields


class ResourceRepository:
    """资源状态存储接口。"""

    def write_status(self, response: dict[str, Any]) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class Influx1Repository(ResourceRepository):
    """将课题二 node 资源转换为 JSON point 后写入 InfluxDB 1.x。"""

    MEASUREMENT = "resource"

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        database: str,
        measurement: str = MEASUREMENT,
        timeout_seconds: float = 5.0,
    ) -> None:
        self.measurement = measurement
        # 保持与 collector/data_receive.py 一致，使用 influxdb Python Client
        # 的 JSON point 写入方式，而不是手工拼接 line protocol。
        self._client = InfluxDBClient(
            host=host,
            port=port,
            username=username,
            password=password,
            database=database,
            timeout=timeout_seconds,
        )

    def write_status(self, response: dict[str, Any]) -> None:
        snapshot = _parse_snapshot(response)
        if snapshot is None:
            return

        fields = _legacy_resource_fields(snapshot)
        point = {
            "measurement": self.measurement,
            "time": snapshot["timestamp_ms"],
            "fields": fields,
        }
        try:
            written = self._client.write_points([point], time_precision="ms")
        except Exception as error:  # noqa: BLE001
            raise RuntimeError(f"InfluxDB 请求失败: {error}") from error
        if not written:
            raise RuntimeError("InfluxDB 拒绝写入资源数据")

    def close(self) -> None:
        self._client.close()


class FileInfluxRepository(ResourceRepository):
    """测试模式下将资源状态保存为本地 JSON 文件。"""

    def __init__(self, file_path: str | Path) -> None:
        self.file_path = Path(file_path)
        self._lock = Lock()

    def write_status(self, response: dict[str, Any]) -> None:
        snapshot = _parse_snapshot(response)
        if snapshot is None:
            return

        entry_key = (
            f"node:{snapshot['node_id']}@timestamp:{snapshot['timestamp_ms']}"
        )
        entry = {
            # 测试文件与真实 InfluxDB 使用同一字段结构；时间值保持不变。
            "time": snapshot["timestamp_ms"],
            **_legacy_resource_fields(snapshot),
        }
        with self._lock:
            entries = self._read_entries()
            entries[entry_key] = entry
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            self.file_path.write_text(
                json.dumps({"entries": entries}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def _read_entries(self) -> dict[str, Any]:
        if not self.file_path.exists():
            return {}
        try:
            content = json.loads(self.file_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("模拟数据库文件不是合法 JSON，重新初始化：%s", self.file_path)
            return {}
        if not isinstance(content, dict) or not isinstance(content.get("entries"), dict):
            logger.warning("模拟数据库文件格式错误，重新初始化：%s", self.file_path)
            return {}
        return content["entries"]

    def close(self) -> None:
        return
