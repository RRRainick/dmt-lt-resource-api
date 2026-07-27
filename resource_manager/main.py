from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from .decision_system import DecisionSystem
from .influxdb import FileInfluxRepository, Influx1Repository, ResourceRepository
from .models import NodeConfig
from .node_system import NodeSystem
from .resource_controller import ResourceController

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(levelname)s %(message)s",
)


def _load_config() -> dict[str, Any]:
    path = Path(
        os.getenv("RESOURCE_MANAGER_CONFIG", str(Path(__file__).with_name("config.json")))
    )
    if not path.exists():
        return {}
    config = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError(f"配置文件必须是 JSON 对象: {path}")
    return config


def _setting(config: dict[str, Any], section: str, key: str, env: str, default: Any) -> Any:
    value = os.getenv(env)
    if value is not None:
        return value
    section_values = config.get(section, {})
    return section_values.get(key, default) if isinstance(section_values, dict) else default


def _database_setting(
    config: dict[str, Any], section: str, key: str, env: str, default: Any
) -> Any:
    """数据库配置优先读取 config.json，环境变量只补充缺失字段。"""

    section_values = config.get(section, {})
    if isinstance(section_values, dict) and key in section_values:
        return section_values[key]
    return os.getenv(env, default)


def _as_bool(value: Any) -> bool:
    return value if isinstance(value, bool) else str(value).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _database_file_path(config: dict[str, Any]) -> Path:
    """返回测试模式使用的本地 JSON 文件路径。"""

    value = _database_setting(
        config,
        "database",
        "file_path",
        "MOCK_DB_FILE",
        "../mock_influxdb.json",
    )
    path = Path(str(value))
    if path.is_absolute():
        return path
    config_path = Path(
        os.getenv("RESOURCE_MANAGER_CONFIG", str(Path(__file__).with_name("config.json")))
    )
    return config_path.parent / path


def _load_nodes(config: dict[str, Any]) -> list[NodeConfig]:
    raw = os.getenv("NODES_JSON")
    values = json.loads(raw) if raw is not None else config.get("nodes", [])
    if not isinstance(values, list):
        raise ValueError("nodes 必须是数组")

    nodes = []
    for item in values:
        if not isinstance(item, dict):
            continue
        if not _as_bool(item.get("enabled", True)):
            continue
        nodes.append(
            NodeConfig(node_id=str(item["node_id"]), base_url=str(item["base_url"]))
        )
    node_ids = [node.node_id for node in nodes]
    if len(node_ids) != len(set(node_ids)):
        raise ValueError("nodes 中存在重复 node_id")
    return nodes


config = _load_config()
database_enabled = _as_bool(
    _database_setting(config, "database", "enabled", "INFLUX_ENABLED", True)
)
repository: ResourceRepository
if database_enabled:
    repository = Influx1Repository(
        host=str(_database_setting(config, "database", "host", "INFLUX_HOST", "127.0.0.1")),
        port=int(_database_setting(config, "database", "port", "INFLUX_PORT", 8086)),
        username=str(_database_setting(config, "database", "username", "INFLUX_USERNAME", "")),
        password=str(_database_setting(config, "database", "password", "INFLUX_PASSWORD", "")),
        database=str(_database_setting(config, "database", "name", "INFLUX_DATABASE", "MODALITY_RESOURCE")),
        measurement=str(_database_setting(config, "database", "measurement", "INFLUX_MEASUREMENT", "resource")),
        timeout_seconds=float(_database_setting(config, "database", "timeout_seconds", "INFLUX_TIMEOUT_SECONDS", 5)),
    )
else:
    repository = FileInfluxRepository(_database_file_path(config))

nodes = _load_nodes(config)
request_id_start = int(os.getenv("REQUEST_ID_START", "1"))
node_system = NodeSystem(
    nodes=nodes,
    timeout_seconds=float(_setting(config, "monitor", "timeout_seconds", "NODE_TIMEOUT_SECONDS", 2)),
    request_id_start=request_id_start,
)
decision_system = DecisionSystem(
    base_url=str(_setting(config, "decision", "base_url", "DECISION_BASE_URL", "http://127.0.0.1:9000")),
    timeout_seconds=float(_setting(config, "decision", "timeout_seconds", "DECISION_TIMEOUT_SECONDS", 3)),
    request_id_start=request_id_start,
)
controller = ResourceController(
    node_system=node_system,
    decision_system=decision_system,
    repository=repository,
    monitor_interval=float(_setting(config, "monitor", "interval_seconds", "MONITOR_INTERVAL_SECONDS", 1)),
    config_interval=float(_setting(config, "decision", "interval_seconds", "CONFIG_INTERVAL_SECONDS", 10)),
    config_enabled=_as_bool(_setting(config, "decision", "enabled", "DECISION_ENABLED", True)),
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    controller.start()
    yield
    controller.stop()
    repository.close()


app = FastAPI(title="Resource Manager", version="0.2.0", lifespan=lifespan)
