""" Node 资源接口。

启动时发现 Pod，之后每 5 秒刷新一次 Pod 列表；资源指标按 1 秒读取本机
/proc、/sys、cgroup 和 Pod 网络命名空间。接口：

- GET /resource/status?node_id=<hostname>&request_id=<id>
- POST /resource/config：按模态写入 CPU、内存 cgroup 配额和 TC 带宽上限。
"""

from __future__ import annotations

import json
import math
import os
import re
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


# 新节点只需要在这里增加 hostname、IP、Kubernetes 节点名和业务网卡。
NODE_TABLE = {
    # 网口容量单位为 Mbps；10000 表示 10 Gbps。
    "IPL238": {
        "ip": "192.168.104.238",
        "kube_node": "ipl238",
        "interface": "enp60s0f1",
        "interface_capacity_mbps": 10000,
    },
    "SDN234": {
        "ip": "192.168.104.234",
        "kube_node": "sdn234",
        "interface": "ens41f0",
        "interface_capacity_mbps": 10000,
    },
    "IPL235": {
        "ip": "192.168.104.235",
        "kube_node": "ipl235",
        "interface": "",
        "interface_capacity_mbps": 0,
    },
}

# 带宽限速：
# 必须与 multimodal/script/before-sys-work.py 中的 TC_CLASS_IDS 保持一致。
# before-sys-work.py 在启动期创建这些 class 并绑定 EtherType/MAC；本 Agent
# 在运行期只对已存在的 class 执行 "tc class change"，不会重建 qdisc 或 filter。
TC_CLASS_IDS = {
    "ndn": "1:10",
    "geo": "1:11",
    "mf": "1:12",
    "ipv4": "1:13",
    "ipv6": "1:14",
    "srv6": "1:15",
    "powerlink": "1:16",
}

HOSTNAME = socket.gethostname()
NODE_ID = HOSTNAME
PROFILE = NODE_TABLE.get(HOSTNAME.upper(), {})
POD_NAMESPACE = "default"
SAMPLE_SECONDS = 1.0
DISCOVERY_SECONDS = 5.0
SERVER_CPU_COUNT = os.cpu_count() or 1
CPU_QUOTA_MIN_US = 1_000
RESOURCE_CONFIG_LOCK = threading.Lock()
# False 时仅确认收到 /resource/config 请求，不修改 cgroup 或 TC 配置。
ENABLE_CONFIG = False


@dataclass
class PodTarget:
    pod: str
    modality: str
    container_id: str
    pid: int
    cpu_cgroup: str | None
    memory_cgroup: str | None
    last_cpu_ns: int | None = None
    last_net_bytes: int | None = None


class ResourceConfigError(ValueError):
    """node 资源配置请求无法应用。"""


def command(args: list[str]) -> str:
    result = subprocess.run(
        args,
        check=True,
        capture_output=True,
        text=True,
        timeout=8,
    )
    return result.stdout.strip()


def modality_of(pod: dict[str, Any]) -> str:
    labels = pod.get("metadata", {}).get("labels", {})
    if isinstance(labels, dict) and labels.get("modality"):
        return str(labels["modality"])
    name = str(pod.get("metadata", {}).get("name", ""))
    return re.split(r"[-_]", name, maxsplit=1)[0]


def cgroups(pid: int) -> tuple[str | None, str | None]:
    """返回 Pod 的 cpu、memory cgroup 路径；优先 v1，再回退 v2。"""

    try:
        lines = Path(f"/proc/{pid}/cgroup").read_text().splitlines()
    except OSError:
        return None, None

    def locate_v1(controller: str, relative: str) -> str | None:
        if relative in {"", "/"}:
            return None
        roots = {
            "cpu": (
                "/sys/fs/cgroup/cpu,cpuacct",
                "/sys/fs/cgroup/cpuacct",
                "/sys/fs/cgroup/cpu",
            ),
            "memory": ("/sys/fs/cgroup/memory",),
        }[controller]
        files = {
            "cpu": ("cpuacct.usage", "cpu.stat"),
            "memory": ("memory.usage_in_bytes", "memory.current"),
        }[controller]
        for root in roots:
            candidate = Path(root, relative.lstrip("/"))
            if candidate.is_dir() and any((candidate / name).is_file() for name in files):
                return str(candidate)
        return None

    cpu = memory = None
    unified_relative: str | None = None
    for line in lines:
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        controllers, relative = parts[1], parts[2]
        if not controllers:
            # 混合环境中的 0::/ 可能只是 Calico 的 v2 层级，先记录但
            # 不能覆盖已经找到的 v1 CPU/内存路径。
            unified_relative = relative
            continue
        names = controllers.split(",")
        if cpu is None and ("cpu" in names or "cpuacct" in names):
            cpu = locate_v1("cpu", relative)
        if memory is None and "memory" in names:
            memory = locate_v1("memory", relative)

    # 纯 v2 主机没有 v1 controller。只有路径不是根目录且对应资源文件
    # 确实存在时才采用，避免把 /sys/fs/cgroup 根目录当成任意 Pod。
    if unified_relative not in {None, "", "/"}:
        unified = Path("/sys/fs/cgroup", unified_relative.lstrip("/"))
        if cpu is None and (unified / "cpu.stat").is_file():
            cpu = str(unified)
        if memory is None and (unified / "memory.current").is_file():
            memory = str(unified)
    return cpu, memory


def container_pid(container_id: str) -> int | None:
    try:
        # 直接读取 JSON，避免不同 crictl 版本对 go-template 参数的差异。
        payload = json.loads(command(["crictl", "inspect", container_id.split("://")[-1]]))
        pid = int(payload.get("info", {}).get("pid", 0))
        return pid if pid > 0 else None
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or str(error)).strip()
        print(
            f"[resource-agent] crictl inspect failed container={container_id} {detail}",
            flush=True,
        )
        return None
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(
            f"[resource-agent] crictl inspect parse failed container={container_id} {error}",
            flush=True,
        )
        return None


def discover_pods(previous: dict[str, PodTarget] | None = None) -> list[PodTarget]:
    """发现当前节点的 Pod；同一容器复用已有目标，避免重复 inspect。"""

    initial_discovery = previous is None
    kube_node = PROFILE.get("kube_node", HOSTNAME.lower())
    previous = previous or {}
    try:
        payload = json.loads(
            command(
                [
                    "kubectl",
                    "get",
                    "pods",
                    "-n",
                    POD_NAMESPACE,
                    "--field-selector",
                    f"spec.nodeName={kube_node}",
                    "-o",
                    "json",
                ]
            )
        )
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"[resource-agent] pod discovery failed: {error}", flush=True)
        # kubectl 临时失败时保留上一轮，避免接口瞬间丢失全部 Pod。
        return list(previous.values())

    targets: list[PodTarget] = []
    for pod in payload.get("items", []):
        if pod.get("status", {}).get("phase") != "Running":
            continue
        statuses = pod.get("status", {}).get("containerStatuses", [])
        running = next(
            (
                item
                for item in statuses
                if item.get("state", {}).get("running") and item.get("containerID")
            ),
            None,
        )
        if not running:
            continue
        container_id = str(running["containerID"])
        old = previous.get(container_id)
        if old is not None:
            targets.append(old)
            continue
        pid = container_pid(container_id)
        if pid is None:
            continue
        cpu, memory = cgroups(pid)
        targets.append(
            PodTarget(
                pod=str(pod.get("metadata", {}).get("name", "")),
                modality=modality_of(pod),
                container_id=container_id,
                pid=pid,
                cpu_cgroup=cpu,
                memory_cgroup=memory,
            )
        )

    # 相同模态每 5 秒会被重新确认，但不重复刷屏；Pod 仅重启或实例数变化
    # 而模态集合未变化时也不输出。
    previous_modalities = {item.modality for item in previous.values()}
    current_modalities = {item.modality for item in targets}
    if initial_discovery or current_modalities != previous_modalities:
        print(
            f"[resource-agent] discovered node={kube_node} namespace={POD_NAMESPACE} "
            f"pods={len(targets)} interface={PROFILE.get('interface') or 'auto'}",
            flush=True,
        )
    return targets


def host_cpu() -> tuple[int, int] | None:
    try:
        fields = Path("/proc/stat").read_text().splitlines()[0].split()
        values = [int(value) for value in fields[1:]]
        return sum(values), values[3] + (values[4] if len(values) > 4 else 0)
    except (OSError, ValueError, IndexError):
        return None


def host_memory_bytes() -> int:
    values: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, value = line.partition(":")
            if key in {"MemTotal", "MemAvailable"}:
                values[key] = int(value.split()[0]) * 1024
    except (OSError, ValueError):
        return 0
    return max(0, values.get("MemTotal", 0) - values.get("MemAvailable", 0))


def host_memory_capacity_bytes() -> int:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def interface_bytes(interface: str) -> int:
    if not interface:
        return 0
    try:
        rx = int(Path(f"/sys/class/net/{interface}/statistics/rx_bytes").read_text())
        tx = int(Path(f"/sys/class/net/{interface}/statistics/tx_bytes").read_text())
        return rx + tx
    except (OSError, ValueError):
        return 0


@lru_cache(maxsize=16)
def interface_capacity_mbps(interface: str) -> float:
    if not interface:
        return float(PROFILE.get("interface_capacity_mbps", 0) or 0)
    fallback = float(PROFILE.get("interface_capacity_mbps", 0) or 0)
    try:
        speed = float(Path(f"/sys/class/net/{interface}/speed").read_text().strip())
        if speed > 0:
            return speed
    except (OSError, ValueError):
        pass

    # sysfs 返回 -1/0 时，优先使用节点表中配置的固定网口容量。
    if fallback > 0:
        return fallback

    # 未配置固定值时，才尝试用 ethtool 作为最后回退。
    try:
        output = command(["ethtool", interface])
    except (OSError, subprocess.SubprocessError):
        return fallback
    match = re.search(
        r"^\s*Speed:\s*([0-9]+(?:\.[0-9]+)?)\s*(Mb/s|Gb/s|Kb/s)",
        output,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    if not match:
        return fallback
    value = float(match.group(1))
    unit = match.group(2).lower()
    if unit == "gb/s":
        value *= 1000.0
    elif unit == "kb/s":
        value /= 1000.0
    return value if value > 0 else fallback


def pod_net_bytes(pid: int) -> int:
    try:
        lines = Path(f"/proc/{pid}/net/dev").read_text().splitlines()
    except OSError:
        return 0
    total = 0
    for line in lines:
        if ":" not in line:
            continue
        interface, values = line.split(":", 1)
        fields = values.split()
        if interface.strip() == "lo" or len(fields) < 9:
            continue
        try:
            total += int(fields[0]) + int(fields[8])
        except ValueError:
            pass
    return total


def cgroup_cpu_ns(path: str | None) -> int:
    if not path:
        return 0
    for filename in ("cpu.stat", "cpuacct.usage"):
        try:
            text = Path(path, filename).read_text().strip()
        except OSError:
            continue
        if filename == "cpuacct.usage":
            try:
                return int(text)
            except ValueError:
                continue
        for line in text.splitlines():
            fields = line.split()
            if len(fields) == 2 and fields[0] == "usage_usec":
                return int(fields[1]) * 1000
    return 0


def cgroup_memory_bytes(path: str | None) -> int:
    if not path:
        return 0
    for filename in ("memory.current", "memory.usage_in_bytes"):
        try:
            return int(Path(path, filename).read_text().strip())
        except (OSError, ValueError):
            continue
    return 0


def process_cpu_ns(pid: int) -> int:
    """cgroup 不可读时，至少用容器主进程的 CPU 时间作为回退。"""

    try:
        fields = Path(f"/proc/{pid}/stat").read_text().split()
        # /proc/<pid>/stat 的第 14、15 列是 utime、stime。
        ticks = int(fields[13]) + int(fields[14])
        return int(ticks * 1_000_000_000 / os.sysconf("SC_CLK_TCK"))
    except (OSError, ValueError, IndexError):
        return 0


def process_memory_bytes(pid: int) -> int:
    """cgroup 不可读时，回退到主进程 RSS，避免 Pod 永远显示 0。"""

    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def target_cpu_ns(target: PodTarget) -> int:
    value = cgroup_cpu_ns(target.cpu_cgroup)
    return value if value > 0 else process_cpu_ns(target.pid)


def target_memory_bytes(target: PodTarget) -> int:
    value = cgroup_memory_bytes(target.memory_cgroup)
    return value if value > 0 else process_memory_bytes(target.pid)


def mbps(delta: int, seconds: float) -> float:
    return max(0.0, delta * 8 / seconds / 1_000_000) if seconds > 0 else 0.0


def _config_number(values: dict[str, Any], name: str, *, minimum: float = 0.0) -> float:
    value = values.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResourceConfigError(f"缺少或非法的 {name}")
    number = float(value)
    if not math.isfinite(number) or not number >= minimum:
        raise ResourceConfigError(f"{name} 必须大于等于 {minimum}")
    return number


def _cgroup_file(path: str, names: tuple[str, ...], resource_name: str) -> Path:
    for name in names:
        candidate = Path(path, name)
        if candidate.is_file():
            return candidate
    raise ResourceConfigError(f"cgroup {path} 缺少 {resource_name} 控制文件")


def _write_cgroup_value(path: Path, value: str, resource_name: str) -> None:
    try:
        path.write_text(value, encoding="utf-8")
    except OSError as error:
        raise ResourceConfigError(
            f"写入 {resource_name} 失败：{path}：{error}"
        ) from error


def apply_cpu_limit(cpu_cgroup: str, compute_percent: float) -> None:
    """按节点总 CPU 的百分比写入一个 cgroup 的 CPU 上限。"""

    cpu_max = Path(cpu_cgroup, "cpu.max")
    if cpu_max.is_file():
        try:
            current = cpu_max.read_text(encoding="utf-8").split()
            period_us = int(current[1]) if len(current) == 2 else 100_000
        except (OSError, ValueError):
            period_us = 100_000
        quota_us = max(
            CPU_QUOTA_MIN_US,
            round(compute_percent / 100 * SERVER_CPU_COUNT * period_us),
        )
        _write_cgroup_value(cpu_max, f"{quota_us} {period_us}", "CPU 配额")
        return

    quota_file = _cgroup_file(
        cpu_cgroup,
        ("cpu.cfs_quota_us",),
        "cpu.cfs_quota_us",
    )
    period_file = _cgroup_file(
        cpu_cgroup,
        ("cpu.cfs_period_us",),
        "cpu.cfs_period_us",
    )
    try:
        period_us = int(period_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        period_us = 100_000
    quota_us = max(
        CPU_QUOTA_MIN_US,
        round(compute_percent / 100 * SERVER_CPU_COUNT * period_us),
    )
    _write_cgroup_value(quota_file, str(quota_us), "CPU 配额")


def apply_memory_limit(memory_cgroup: str, storage_mb: float) -> None:
    """按十进制 MB 写入一个 cgroup 的内存上限。"""

    limit_bytes = max(1, round(storage_mb * 1_000_000))
    memory_max = Path(memory_cgroup, "memory.max")
    if memory_max.is_file():
        _write_cgroup_value(memory_max, str(limit_bytes), "内存配额")
        return

    limit_file = _cgroup_file(
        memory_cgroup,
        ("memory.limit_in_bytes",),
        "memory.limit_in_bytes",
    )
    _write_cgroup_value(limit_file, str(limit_bytes), "内存配额")


def apply_tc_limit(modality: str, forwarding_mbps: float) -> dict[str, Any]:
    """修改启动期已创建的 HTB class 上限，不触碰 qdisc 或 filter。"""

    interface = str(PROFILE.get("interface", "")).strip()
    if not interface:
        raise ResourceConfigError(f"节点 {NODE_ID} 没有配置 TC 网卡")

    classid = TC_CLASS_IDS.get(modality)
    if not classid:
        raise ResourceConfigError(f"模态 {modality} 没有配置 TC class")
    if forwarding_mbps <= 0:
        raise ResourceConfigError("forwarding_config_mbps 必须大于 0")

    capacity_mbps = float(PROFILE.get("interface_capacity_mbps", 0) or 0)
    if capacity_mbps > 0 and forwarding_mbps > capacity_mbps:
        raise ResourceConfigError(
            f"模态 {modality} 带宽上限 {forwarding_mbps:g} Mbps 超过网口容量 "
            f"{capacity_mbps:g} Mbps"
        )

    rate = f"{forwarding_mbps:g}mbit"
    tc_args = [
        "tc",
        "class",
        "change",
        "dev",
        interface,
        "parent",
        "1:",
        "classid",
        classid,
        "htb",
        "rate",
        rate,
        "ceil",
        rate,
    ]
    try:
        command(tc_args)
    except (OSError, subprocess.SubprocessError) as error:
        detail = getattr(error, "stderr", "") or str(error)
        raise ResourceConfigError(
            f"tc 配置失败（{interface} {classid}）：{str(detail).strip()}"
        ) from error

    return {
        "state": "applied",
        "interface": interface,
        "classid": classid,
        "requested_mbps": forwarding_mbps,
        "rate": rate,
    }


class ResourceSampler:
    def __init__(self) -> None:
        self.targets = discover_pods()
        # 启动时先取一次基线，这样第一个 HTTP 查询在服务启动约 1 秒后
        # 就能得到 Pod CPU/带宽增量，不再额外丢弃第一轮数据。
        for target in self.targets:
            target.last_cpu_ns = target_cpu_ns(target)
            target.last_net_bytes = pod_net_bytes(target.pid)
        self.last_at = time.monotonic()
        self.last_discovery_at = self.last_at
        self.last_host_cpu = host_cpu()
        self.last_host_net = interface_bytes(PROFILE.get("interface", ""))
        self.storage_capacity_mb = host_memory_capacity_bytes() / 1_000_000
        # 链路容量只在 Agent 启动时读取一次；后续每秒采样只读取流量计数器。
        self.forwarding_capacity_mbps = interface_capacity_mbps(
            PROFILE.get("interface", "")
        )
        self.snapshot: dict[str, Any] = self.empty()
        self.lock = threading.Lock()
        self.stop_event = threading.Event()

    @staticmethod
    def empty() -> dict[str, Any]:
        return {
            "timestamp_ms": int(time.time() * 1000),
            "node_resource": {
                "compute_usage_percent": 0.0,
                "storage_usage_mb": 0.0,
                "forwarding_usage_mbps": 0.0,
                "storage_capacity_mb": 0.0,
                "forwarding_capacity_mbps": 0.0,
            },
            "modalities_resource": [],
        }

    def start(self) -> None:
        threading.Thread(target=self.loop, name="resource-sampler", daemon=True).start()

    def loop(self) -> None:
        while not self.stop_event.wait(SAMPLE_SECONDS):
            now = time.monotonic()
            if now - self.last_discovery_at >= DISCOVERY_SECONDS:
                self.refresh_targets()
                self.last_discovery_at = now
            self.sample()

    def refresh_targets(self) -> None:
        """每 5 秒刷新 Pod；新容器建立基线，旧容器保留累计值。"""

        previous = {target.container_id: target for target in self.targets}
        refreshed = discover_pods(previous)
        for target in refreshed:
            if target.container_id not in previous:
                target.last_cpu_ns = target_cpu_ns(target)
                target.last_net_bytes = pod_net_bytes(target.pid)
        self.targets = refreshed

    def sample(self) -> None:
        current_at = time.monotonic()
        seconds = max(current_at - self.last_at, 0.001)
        current_cpu = host_cpu()
        current_net = interface_bytes(PROFILE.get("interface", ""))

        node_cpu = 0.0
        if current_cpu and self.last_host_cpu:
            total_delta = current_cpu[0] - self.last_host_cpu[0]
            idle_delta = current_cpu[1] - self.last_host_cpu[1]
            if total_delta > 0:
                # 节点：CPU 使用量 / 服务器全部 CPU 总量。
                node_cpu = max(0.0, (total_delta - idle_delta) * 100 / total_delta)

        modalities: dict[str, dict[str, Any]] = {}
        for target in self.targets:
            current_cpu_ns = target_cpu_ns(target)
            current_net_bytes = pod_net_bytes(target.pid)
            item = modalities.setdefault(
                target.modality,
                {
                    "modality": target.modality,
                    "compute_usage_percent": 0.0,
                    "storage_usage_mb": 0.0,
                    "forwarding_usage_mbps": 0.0,
                },
            )
            if target.last_cpu_ns is not None:
                # Pod：Pod 使用 CPU 时间 / 服务器全部 CPU 总时间。
                item["compute_usage_percent"] += max(
                    0.0,
                    (current_cpu_ns - target.last_cpu_ns)
                    / 1_000_000_000
                    / seconds
                    / SERVER_CPU_COUNT
                    * 100,
                )
            item["storage_usage_mb"] += target_memory_bytes(target) / 1_000_000
            if target.last_net_bytes is not None:
                item["forwarding_usage_mbps"] += mbps(
                    current_net_bytes - target.last_net_bytes,
                    seconds,
                )
            target.last_cpu_ns = current_cpu_ns
            target.last_net_bytes = current_net_bytes

        with self.lock:
            self.snapshot = {
                "timestamp_ms": int(time.time() * 1000),
                "node_resource": {
                    # 保留原始浮点值，避免小流量/小 CPU 使用量被截断。
                    "compute_usage_percent": node_cpu,
                    "storage_usage_mb": host_memory_bytes() / 1_000_000,
                    "forwarding_usage_mbps": mbps(
                        current_net - self.last_host_net,
                        seconds,
                    ),
                    "storage_capacity_mb": self.storage_capacity_mb,
                    "forwarding_capacity_mbps": self.forwarding_capacity_mbps,
                },
                "modalities_resource": [
                    item
                    for item in sorted(modalities.values(), key=lambda x: x["modality"])
                ],
            }
        self.last_at = current_at
        self.last_host_cpu = current_cpu
        self.last_host_net = current_net

    def latest(self) -> dict[str, Any]:
        with self.lock:
            return json.loads(json.dumps(self.snapshot))


SAMPLER = ResourceSampler()


def _parse_mode_policies(config: dict[str, Any]) -> list[dict[str, Any]]:
    requested_node_id = config.get("node_id")
    if requested_node_id is not None and str(requested_node_id).casefold() != NODE_ID.casefold():
        raise ResourceConfigError(f"node_id 不匹配：{requested_node_id}")

    raw_modes = config.get("modalities_resource")
    if not isinstance(raw_modes, list) or not raw_modes:
        raise ResourceConfigError("modalities_resource 必须是非空数组")

    policies: list[dict[str, Any]] = []
    seen_modes: set[str] = set()
    for raw_mode in raw_modes:
        if not isinstance(raw_mode, dict):
            raise ResourceConfigError("modalities_resource 元素必须是对象")
        modality = str(raw_mode.get("modality", "")).strip().lower()
        if not modality:
            raise ResourceConfigError("模态配置缺少 modality")
        if modality in seen_modes:
            raise ResourceConfigError(f"模态配置重复：{modality}")

        compute_percent = _config_number(raw_mode, "compute_config_percent")
        storage_mb = _config_number(raw_mode, "storage_config_mb")
        forwarding_mbps = _config_number(raw_mode, "forwarding_config_mbps")
        if not 0 < compute_percent <= 100:
            raise ResourceConfigError("compute_config_percent 必须在 (0, 100] 范围内")
        if storage_mb <= 0:
            raise ResourceConfigError("storage_config_mb 必须大于 0")
        if forwarding_mbps <= 0:
            raise ResourceConfigError("forwarding_config_mbps 必须大于 0")

        seen_modes.add(modality)
        policies.append(
            {
                "modality": modality,
                "compute_config_percent": compute_percent,
                "storage_config_mb": storage_mb,
                "forwarding_config_mbps": forwarding_mbps,
            }
        )
    return policies


def _unique_cgroups(targets: list[PodTarget], attribute: str, modality: str) -> list[str]:
    paths = {str(getattr(target, attribute)) for target in targets if getattr(target, attribute)}
    if len(paths) != len(targets):
        raise ResourceConfigError(f"模态 {modality} 存在无法识别的 {attribute}")
    return sorted(paths)


def apply_resource_config(config: dict[str, Any]) -> list[dict[str, Any]]:
    """对每个模态的 Pod cgroup 应用 CPU、内存总配额。"""

    policies = _parse_mode_policies(config)
    # 配置到来时立即刷新，避免把限额写给已经重启的旧 cgroup。
    SAMPLER.refresh_targets()
    targets = list(SAMPLER.targets)
    by_modality: dict[str, list[PodTarget]] = {}
    for target in targets:
        by_modality.setdefault(target.modality.casefold(), []).append(target)

    results: list[dict[str, Any]] = []
    with RESOURCE_CONFIG_LOCK:
        for policy in policies:
            modality = str(policy["modality"])
            mode_targets = by_modality.get(modality, [])
            if not mode_targets:
                raise ResourceConfigError(f"当前节点没有运行中的 {modality} Pod")

            cpu_cgroups = _unique_cgroups(mode_targets, "cpu_cgroup", modality)
            memory_cgroups = _unique_cgroups(mode_targets, "memory_cgroup", modality)
            cpu_per_cgroup = float(policy["compute_config_percent"]) / len(cpu_cgroups)
            memory_per_cgroup = float(policy["storage_config_mb"]) / len(memory_cgroups)
            for cgroup in cpu_cgroups:
                apply_cpu_limit(cgroup, cpu_per_cgroup)
            for cgroup in memory_cgroups:
                apply_memory_limit(cgroup, memory_per_cgroup)

            results.append(
                {
                    "modality": modality,
                    "cpu": {
                        "state": "applied",
                        "total_percent": policy["compute_config_percent"],
                        "per_cgroup_percent": cpu_per_cgroup,
                        "cgroup_count": len(cpu_cgroups),
                    },
                    "memory": {
                        "state": "applied",
                        "total_mb": policy["storage_config_mb"],
                        "per_cgroup_mb": memory_per_cgroup,
                        "cgroup_count": len(memory_cgroups),
                    },
                    "forwarding": apply_tc_limit(
                        modality, float(policy["forwarding_config_mbps"])
                    ),
                }
            )
    return results


def response(node_id: str, request_id: str) -> dict[str, Any]:
    if node_id != NODE_ID or not request_id.isdecimal():
        return {"code": 1, "msg": "apply resource query failed", "data": {"request_id": request_id}}
    resource = SAMPLER.latest()
    return {
        "code": 0,
        "msg": "ok",
        "data": {
            "request_id": int(request_id),
            "timestamp_ms": resource["timestamp_ms"],
            "node_id": NODE_ID,
            "node_resource": resource["node_resource"],
            "modalities_resource": resource["modalities_resource"],
        },
    }


class Handler(BaseHTTPRequestHandler):
    logged_first_resource_response = False

    def do_GET(self) -> None:
        request = urlparse(self.path)
        if request.path != "/resource/status":
            self.send_error(404)
            return
        query = parse_qs(request.query)
        result = response(
            query.get("node_id", [""])[0],
            query.get("request_id", [""])[0],
        )
        if not Handler.logged_first_resource_response:
            Handler.logged_first_resource_response = True
            print(
                "[resource-agent] first resource response: "
                f"{json.dumps(result, ensure_ascii=False)}",
                flush=True,
            )
        body = json.dumps(result, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
        print(
            f"[resource-agent] config request: {json.dumps(config, ensure_ascii=False)}",
            flush=True,
        )
        if not ENABLE_CONFIG:
            result = {
                "code": 0,
                "msg": "ok",
                "data": {"request_id": request_id},
            }
        else:
            try:
                if not isinstance(config, dict):
                    raise ResourceConfigError("请求体必须是 JSON 对象")
                apply_resource_config(config)
                result = {
                    "code": 0,
                    "msg": "ok",
                    "data": {"request_id": request_id},
                }
            except ResourceConfigError as error:
                result = {
                    "code": 1,
                    "msg": f"apply resource config failed: {error}",
                    "data": {"request_id": request_id, "applied": False},
                }
        print(
            f"[resource-agent] config response: {json.dumps(result, ensure_ascii=False)}",
            flush=True,
        )
        body = json.dumps(result, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: Any) -> None:
        pass


if __name__ == "__main__":
    SAMPLER.start()
    print(
        f"resource agent node={NODE_ID} ip={PROFILE.get('ip', 'unknown')} "
        f"interface={PROFILE.get('interface') or 'auto'} "
        f"forwarding_capacity_mbps={SAMPLER.forwarding_capacity_mbps} "
        f"running at http://0.0.0.0:8000",
        flush=True,
    )
    if SAMPLER.targets:
        print("[resource-agent] deployed pods:", flush=True)
        for target in SAMPLER.targets:
            print(f"  modality={target.modality} pod={target.pod}", flush=True)
    else:
        print("[resource-agent] deployed pods: none discovered", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
