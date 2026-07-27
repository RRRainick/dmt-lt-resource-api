from __future__ import annotations

import logging
import threading
import time

from .decision_system import DecisionSystem, DecisionSystemError
from .influxdb import ResourceRepository
from .node_system import NodeSystem

logger = logging.getLogger(__name__)


class ResourceController:
    """课题三资源管理的两个周期任务。"""

    def __init__(
        self,
        node_system: NodeSystem,
        decision_system: DecisionSystem,
        repository: ResourceRepository,
        monitor_interval: float = 1.0,
        config_interval: float = 10.0,
        config_enabled: bool = True,
    ) -> None:
        self.node_system = node_system
        self.decision_system = decision_system
        self.repository = repository
        self.monitor_interval = monitor_interval
        self.config_interval = config_interval
        self.config_enabled = config_enabled
        self.stop_event = threading.Event()
        self.threads: list[threading.Thread] = []

    def start(self) -> None:
        self.threads = [threading.Thread(
            target=self.monitor_loop,
            name="resource-monitor",
            daemon=True,
        )]
        if self.config_enabled:
            self.threads.append(threading.Thread(
                target=self.config_loop,
                name="resource-config",
                daemon=True,
            ))
        for thread in self.threads:
            thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        for thread in self.threads:
            thread.join(timeout=2)

    def monitor_loop(self) -> None:
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                results = self.node_system.read_all_status()
                for result in results:
                    if result.success and result.data:
                        self.repository.write_status(result.data)
            except Exception:  # noqa: BLE001
                logger.exception("资源监测流程失败")

            elapsed = time.monotonic() - started
            self.stop_event.wait(max(0.0, self.monitor_interval - elapsed))

    def config_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                policy = self.decision_system.get_next_res_cfg()
                policy_list = policy.get("policy_list")
                if policy_list:
                    target_node_ids = [
                        str(item.get("node_id", ""))
                        for item in policy_list
                        if isinstance(item, dict)
                        and str(item.get("node_id", "")) in self.node_system.nodes
                    ]
                    logger.debug(
                        "1/4 收到课题四资源配置策略：request_id=%s nodes=%s",
                        policy.get("request_id"),
                        ",".join(target_node_ids),
                    )
                    results = self.node_system.configure_all(policy)
                    success_count = sum(result.success for result in results)
                    logger.debug(
                        "4/4 回传资源配置结果：request_id=%s success=%s failed=%s",
                        policy.get("request_id"),
                        success_count,
                        len(results) - success_count,
                    )
                    self.decision_system.post_impl_status(
                        request_id=policy.get("request_id"),
                        results=results,
                    )
                else:
                    logger.debug("1/4 课题四未返回资源配置策略")
            except DecisionSystemError:
                pass
            except Exception:  # noqa: BLE001
                logger.exception("资源配置流程失败")

            self.stop_event.wait(self.config_interval)
