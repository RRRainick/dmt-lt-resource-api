#!/usr/bin/env python3
"""Test the four synchronous HTTP APIs documented in api.md.

The script intentionally uses only the Python standard library so that it can
be copied with modality_client.py to an integration-test host and run directly.
"""

from __future__ import annotations

import argparse
import copy
import math
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, Sequence, Union


from modality_client import (
    JSON_MISSING, HttpResult, ModalityClient as ApiClient, RequestIds,
    TransportFailure, build_base_url, deploy_body as _deploy_body,
    delete_body as _delete_body,
)

PathPart = Union[str, int]


class TestFailure(AssertionError):
    """An API response does not satisfy the documented contract."""


class SuiteAbort(RuntimeError):
    """Stop unsafe follow-up requests while still running final cleanup."""


@dataclass(frozen=True)
class CaseOutcome:
    ok: bool
    value: Any = None


@dataclass(frozen=True)
class NegativeCase:
    name: str
    method: str
    path: str
    expected_request_id: int | None
    allow_string_request_id: bool = False
    query: dict[str, Any] | None = None
    json_body: Any = JSON_MISSING
    raw_body: bytes | None = None


class Reporter:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.warnings = 0
        self._warning_keys: set[str] = set()

    def run(
        self,
        name: str,
        action: Callable[[], Any],
        *,
        input_summary: str | None = None,
        output_summary: Callable[[Any], str] | None = None,
    ) -> CaseOutcome:
        started = time.perf_counter()
        try:
            value = action()
        except (TestFailure, TransportFailure) as error:
            self.failed += 1
            elapsed = (time.perf_counter() - started) * 1000
            print(f"[FAIL] {name} ({elapsed:.1f} ms): {error}")
            if input_summary:
                print(f"       输入: {input_summary}")
            return CaseOutcome(False)
        except Exception as error:  # noqa: BLE001 - a test runner must keep going
            self.failed += 1
            elapsed = (time.perf_counter() - started) * 1000
            print(
                f"[FAIL] {name} ({elapsed:.1f} ms): "
                f"未预期异常 {type(error).__name__}: {error}"
            )
            if input_summary:
                print(f"       输入: {input_summary}")
            return CaseOutcome(False)

        self.passed += 1
        elapsed = (time.perf_counter() - started) * 1000
        print(f"[PASS] {name} ({elapsed:.1f} ms)")
        if input_summary:
            print(f"       输入: {input_summary}")
        if output_summary:
            print(f"       输出: {output_summary(value)}")
        return CaseOutcome(True, value)

    def fail(self, name: str, message: str) -> None:
        self.failed += 1
        print(f"[FAIL] {name}: {message}")

    def warn(self, message: str) -> None:
        self.warnings += 1
        print(f"[WARN] {message}")

    def warn_once(self, key: str, message: str) -> None:
        if key in self._warning_keys:
            return
        self._warning_keys.add(key)
        self.warn(message)

    def summary(self) -> None:
        total = self.passed + self.failed
        print(
            "\n测试汇总: "
            f"total={total} passed={self.passed} "
            f"failed={self.failed} warnings={self.warnings}"
        )


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise TestFailure(message)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _require_json_response(
    result: HttpResult,
    *,
    allow_client_error: bool,
    reporter: Reporter,
) -> dict[str, Any]:
    if allow_client_error:
        _require(result.status < 500, f"服务器返回 HTTP {result.status}")
    else:
        _require(200 <= result.status < 300, f"期望 HTTP 2xx，实际为 {result.status}")

    media_type = result.content_type.split(";", 1)[0].strip().lower()
    if media_type != "application/json" and not media_type.endswith("+json"):
        reporter.warn_once(
            f"content-type:{media_type}",
            "响应正文是 JSON，但 Content-Type 不是 JSON："
            f"{result.content_type or '<missing>'}",
        )
    _require(
        result.json_error is None,
        f"响应不是合法 JSON：{result.json_error}；body={result.body!r}",
    )
    _require(isinstance(result.payload, dict), "响应 JSON 必须是对象")
    return result.payload


def _validate_request_id(
    data: dict[str, Any],
    expected: int,
    *,
    allow_string: bool,
    reporter: Reporter,
) -> None:
    _require("request_id" in data, "data 缺少 request_id")
    actual = data["request_id"]
    if _is_int(actual):
        _require(actual == expected, f"request_id 不一致：expected={expected}, actual={actual}")
        return
    if allow_string and isinstance(actual, str) and actual.isdecimal():
        _require(
            int(actual) == expected,
            f"request_id 不一致：expected={expected}, actual={actual}",
        )
        reporter.warn_once(
            "status-request-id-string",
            "资源读取响应 request_id 为字符串，已按 api.md 示例兼容",
        )
        return
    expected_type = "整数或同值十进制字符串" if allow_string else "整数"
    raise TestFailure(f"data.request_id 应为{expected_type}，实际为 {actual!r}")


def validate_success(
    result: HttpResult,
    expected_request_id: int,
    *,
    reporter: Reporter,
    allow_string_request_id: bool = False,
) -> dict[str, Any]:
    payload = _require_json_response(
        result,
        allow_client_error=False,
        reporter=reporter,
    )
    _require(_is_int(payload.get("code")), "code 必须是整数且不能是布尔值")
    _require(payload["code"] == 0, f"业务返回失败：code={payload['code']}, msg={payload.get('msg')!r}")
    _require(isinstance(payload.get("msg"), str), "msg 必须是字符串")
    _require(isinstance(payload.get("data"), dict), "data 必须是对象")
    data = payload["data"]
    _validate_request_id(
        data,
        expected_request_id,
        allow_string=allow_string_request_id,
        reporter=reporter,
    )
    return data


def validate_expected_failure(
    result: HttpResult,
    expected_request_id: int | None,
    *,
    reporter: Reporter,
    allow_string_request_id: bool = False,
) -> dict[str, Any]:
    payload = _require_json_response(
        result,
        allow_client_error=True,
        reporter=reporter,
    )
    _require(_is_int(payload.get("code")), "失败响应 code 必须是整数且不能是布尔值")
    _require(payload["code"] != 0, "异常请求未被拒绝：响应 code 为 0")
    _require(isinstance(payload.get("msg"), str), "失败响应 msg 必须是字符串")
    _require(isinstance(payload.get("data"), dict), "失败响应 data 必须是对象")
    if expected_request_id is not None:
        _validate_request_id(
            payload["data"],
            expected_request_id,
            allow_string=allow_string_request_id,
            reporter=reporter,
        )
    return payload


def validate_deploy_data(data: dict[str, Any]) -> None:
    _require("modality_ip" in data, "data 缺少 modality_ip")
    _require(isinstance(data["modality_ip"], str), "data.modality_ip 必须是字符串")
    _require("modality_port" in data, "data 缺少 modality_port")
    _require(
        isinstance(data["modality_port"], str),
        "data.modality_port 必须是字符串",
    )


def _validate_usage_resource(resource: Any, field: str) -> None:
    _require(isinstance(resource, dict), f"{field} 必须是对象")
    numeric_fields = (
        "compute_usage_percent",
        "storage_usage_mb",
        "forwarding_usage_mbps",
    )
    for name in numeric_fields:
        _require(name in resource, f"{field} 缺少 {name}")
        _require(_is_number(resource[name]), f"{field}.{name} 必须是有限数值")
        value = float(resource[name])
        _require(value >= 0, f"{field}.{name} 不能为负数")


def validate_status_data(
    data: dict[str, Any],
    *,
    node_id: str,
    required_modality: str | None,
) -> list[str]:
    _require(_is_int(data.get("timestamp_ms")), "data.timestamp_ms 必须是整数")
    _require(data["timestamp_ms"] >= 0, "data.timestamp_ms 不能为负数")
    _require(isinstance(data.get("node_id"), str), "data.node_id 必须是字符串")
    _require(
        data["node_id"] == node_id,
        f"data.node_id 不一致：expected={node_id!r}, actual={data['node_id']!r}",
    )
    _validate_usage_resource(data.get("node_resource"), "data.node_resource")

    resources = data.get("modalities_resource")
    _require(isinstance(resources, list), "data.modalities_resource 必须是数组")
    modalities: list[str] = []
    for index, resource in enumerate(resources):
        field = f"data.modalities_resource[{index}]"
        _require(isinstance(resource, dict), f"{field} 必须是对象")
        _require(isinstance(resource.get("modality"), str), f"{field}.modality 必须是字符串")
        _require(bool(resource["modality"]), f"{field}.modality 不能为空")
        _validate_usage_resource(resource, field)
        modalities.append(resource["modality"])

    if required_modality is not None:
        _require(
            required_modality in modalities,
            f"资源状态中没有刚部署的模态 {required_modality!r}",
        )
    return modalities


def _config_body(
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
        "modalities_resource": [
            {
                "modality": modality,
                "compute_config_percent": compute,
                "storage_config_mb": storage,
                "forwarding_config_mbps": forwarding,
            }
        ],
    }


def _mutated(source: dict[str, Any], path: Sequence[PathPart], value: Any) -> dict[str, Any]:
    result = copy.deepcopy(source)
    target: Any = result
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    return result


def _omitted(source: dict[str, Any], path: Sequence[PathPart]) -> dict[str, Any]:
    result = copy.deepcopy(source)
    target: Any = result
    for part in path[:-1]:
        target = target[part]
    del target[path[-1]]
    return result


def _request_id_if_valid(body: dict[str, Any]) -> int | None:
    value = body.get("request_id")
    return value if _is_int(value) else None


def build_negative_cases(
    ids: RequestIds,
    *,
    node_id: str,
    modality: str,
    compute: float,
    storage: float,
    forwarding: float,
) -> list[NegativeCase]:
    cases: list[NegativeCase] = []

    status_id = ids.next()
    cases.extend(
        [
            NegativeCase(
                "资源读取/缺少 node_id",
                "GET",
                "/resource/status",
                status_id,
                True,
                query={"request_id": status_id},
            ),
            NegativeCase(
                "资源读取/node_id 为空",
                "GET",
                "/resource/status",
                status_id,
                True,
                query={"node_id": "", "request_id": status_id},
            ),
            NegativeCase(
                "资源读取/缺少 request_id",
                "GET",
                "/resource/status",
                None,
                True,
                query={"node_id": node_id},
            ),
            NegativeCase(
                "资源读取/request_id 类型错误",
                "GET",
                "/resource/status",
                None,
                True,
                query={"node_id": node_id, "request_id": "not-an-int"},
            ),
        ]
    )

    config_omissions: list[tuple[str, tuple[PathPart, ...]]] = [
        ("timestamp_ms", ("timestamp_ms",)),
        ("request_id", ("request_id",)),
        ("node_id", ("node_id",)),
        ("modalities_resource", ("modalities_resource",)),
        ("modality", ("modalities_resource", 0, "modality")),
        (
            "compute_config_percent",
            ("modalities_resource", 0, "compute_config_percent"),
        ),
        ("storage_config_mb", ("modalities_resource", 0, "storage_config_mb")),
        (
            "forwarding_config_mbps",
            ("modalities_resource", 0, "forwarding_config_mbps"),
        ),
    ]
    config_bad_values: list[tuple[str, tuple[PathPart, ...], Any]] = [
        ("timestamp_ms", ("timestamp_ms",), "not-an-int"),
        ("request_id", ("request_id",), "not-an-int"),
        ("node_id", ("node_id",), True),
        ("modalities_resource", ("modalities_resource",), {}),
        ("modality", ("modalities_resource", 0, "modality"), 123),
        (
            "compute_config_percent",
            ("modalities_resource", 0, "compute_config_percent"),
            True,
        ),
        (
            "storage_config_mb",
            ("modalities_resource", 0, "storage_config_mb"),
            True,
        ),
        (
            "forwarding_config_mbps",
            ("modalities_resource", 0, "forwarding_config_mbps"),
            True,
        ),
    ]
    for label, path in config_omissions:
        body = _omitted(
            _config_body(ids.next(), node_id, modality, compute, storage, forwarding),
            path,
        )
        cases.append(
            NegativeCase(
                f"资源配置/缺少 {label}",
                "POST",
                "/resource/config",
                _request_id_if_valid(body),
                json_body=body,
            )
        )
    for label, path, bad_value in config_bad_values:
        body = _mutated(
            _config_body(ids.next(), node_id, modality, compute, storage, forwarding),
            path,
            bad_value,
        )
        cases.append(
            NegativeCase(
                f"资源配置/{label} 类型错误",
                "POST",
                "/resource/config",
                _request_id_if_valid(body),
                json_body=body,
            )
        )
    cases.append(
        NegativeCase(
            "资源配置/非法 JSON",
            "POST",
            "/resource/config",
            None,
            raw_body=b"{",
        )
    )

    deploy_omissions = (
        "timestamp_ms",
        "request_id",
        "node_id",
        "modality",
        "compute_config_percent",
        "storage_config_mb",
        "forwarding_config_mbps",
    )
    deploy_bad_values: dict[str, Any] = {
        "timestamp_ms": "not-an-int",
        "request_id": "not-an-int",
        "node_id": True,
        "modality": 123,
        "compute_config_percent": True,
        "storage_config_mb": True,
        "forwarding_config_mbps": True,
    }
    for field in deploy_omissions:
        body = _deploy_body(ids.next(), node_id, modality, compute, storage, forwarding)
        del body[field]
        cases.append(
            NegativeCase(
                f"模态部署/缺少 {field}",
                "POST",
                "/modality/deploy",
                _request_id_if_valid(body),
                json_body=body,
            )
        )
    for field, bad_value in deploy_bad_values.items():
        body = _deploy_body(ids.next(), node_id, modality, compute, storage, forwarding)
        body[field] = bad_value
        cases.append(
            NegativeCase(
                f"模态部署/{field} 类型错误",
                "POST",
                "/modality/deploy",
                _request_id_if_valid(body),
                json_body=body,
            )
        )
    cases.append(
        NegativeCase(
            "模态部署/非法 JSON",
            "POST",
            "/modality/deploy",
            None,
            raw_body=b"{",
        )
    )

    delete_omissions = ("timestamp_ms", "request_id", "node_id", "modality")
    delete_bad_values: dict[str, Any] = {
        "timestamp_ms": "not-an-int",
        "request_id": "not-an-int",
        "node_id": True,
        "modality": 123,
    }
    for field in delete_omissions:
        body = _delete_body(ids.next(), node_id, modality)
        del body[field]
        cases.append(
            NegativeCase(
                f"模态删除/缺少 {field}",
                "POST",
                "/modality/delete",
                _request_id_if_valid(body),
                json_body=body,
            )
        )
    for field, bad_value in delete_bad_values.items():
        body = _delete_body(ids.next(), node_id, modality)
        body[field] = bad_value
        cases.append(
            NegativeCase(
                f"模态删除/{field} 类型错误",
                "POST",
                "/modality/delete",
                _request_id_if_valid(body),
                json_body=body,
            )
        )
    cases.append(
        NegativeCase(
            "模态删除/非法 JSON",
            "POST",
            "/modality/delete",
            None,
            raw_body=b"{",
        )
    )
    return cases


def _response_code(result: HttpResult) -> int | None:
    payload = result.payload
    if isinstance(payload, dict) and _is_int(payload.get("code")):
        return payload["code"]
    return None


def _status_output(data: dict[str, Any]) -> str:
    modalities = f"[{','.join(data['modalities'])}]"
    resource = data["node_resource"]
    return (
        f"code=0, request_id={data['request_id']}, "
        f"node_id={data['node_id']}, "
        "node_resource={"
        f"compute={resource['compute_usage_percent']}%, "
        f"storage={resource['storage_usage_mb']}MB, "
        f"forwarding={resource['forwarding_usage_mbps']}Mbps"
        f"}}, modalities={modalities}"
    )


def _positive_delete(
    client: ApiClient,
    ids: RequestIds,
    reporter: Reporter,
    node_id: str,
    modality: str,
) -> None:
    request_id = ids.next()
    result = client.delete(node_id, modality, request_id=request_id)
    validate_success(result, request_id, reporter=reporter)


def run_robustness_tests(
    args: argparse.Namespace,
    client: ApiClient,
    ids: RequestIds,
    reporter: Reporter,
) -> None:
    print("\n开始健壮性测试：")
    negative_cases = build_negative_cases(
        ids,
        node_id=args.node_id,
        modality=args.modality,
        compute=args.compute_config_percent,
        storage=args.storage_config_mb,
        forwarding=args.forwarding_config_mbps,
    )
    unexpected_deploy_success = False
    for case in negative_cases:

        def run_negative(current: NegativeCase = case) -> None:
            nonlocal unexpected_deploy_success
            result = client.request(
                current.method,
                current.path,
                query=current.query,
                json_body=current.json_body,
                raw_body=current.raw_body,
            )
            if current.path == "/modality/deploy" and _response_code(result) == 0:
                unexpected_deploy_success = True
            validate_expected_failure(
                result,
                current.expected_request_id,
                reporter=reporter,
                allow_string_request_id=current.allow_string_request_id,
            )

        reporter.run(f"健壮性/{case.name}", run_negative)

    # A broken deploy validator may have accepted an invalid request. Always
    # send one final valid delete; "not found" is harmless when no leak exists.
    cleanup_request_id = ids.next()

    def post_robustness_cleanup() -> None:
        result = client.delete(
            args.node_id, args.modality, request_id=cleanup_request_id,
        )
        code = _response_code(result)
        if code == 0:
            validate_success(result, cleanup_request_id, reporter=reporter)
            return
        if unexpected_deploy_success:
            raise TestFailure(
                "异常部署请求曾返回成功，但最终清理未成功："
                f"code={code}, body={result.body!r}"
            )
        reporter.warn("最终重复清理返回非零 code；测试模态此前已正常删除")

    reporter.run("清理/健壮性测试后重复删除", post_robustness_cleanup)


def run_suite(args: argparse.Namespace) -> int:
    reporter = Reporter()
    ids = RequestIds()
    base_url = build_base_url(args.scheme, args.ip, args.port)
    client = ApiClient(base_url, args.timeout, args.verbose)
    deployed = False
    interrupted = False

    print(f"目标服务: {base_url}")
    print(f"测试节点: {args.node_id}；一次性测试模态: {args.modality}")
    print(f"测试模式: {'正确性 + 健壮性' if args.robustness else '正确性'}")
    print("注意: 本脚本会执行模态部署、资源配置和模态删除写操作。\n")

    try:
        preflight_request_id = ids.next()

        def preflight() -> dict[str, Any]:
            result = client.request(
                "GET",
                "/resource/status",
                query={"node_id": args.node_id, "request_id": preflight_request_id},
            )
            data = validate_success(
                result,
                preflight_request_id,
                reporter=reporter,
                allow_string_request_id=True,
            )
            modalities = validate_status_data(
                data,
                node_id=args.node_id,
                required_modality=None,
            )
            return {
                "request_id": data["request_id"],
                "node_id": data["node_id"],
                "node_resource": data["node_resource"],
                "modalities": modalities,
            }

        preflight_outcome = reporter.run(
            "安全预检/资源读取",
            preflight,
            input_summary=(
                "GET /resource/status | "
                f"node_id={args.node_id}, request_id={preflight_request_id}"
            ),
            output_summary=_status_output,
        )
        if not preflight_outcome.ok:
            reporter.warn("预检失败，为避免误操作，已跳过后续测试")
            raise SuiteAbort

        if args.modality in preflight_outcome.value["modalities"]:
            reporter.fail(
                "安全预检/模态名称",
                f"模态 {args.modality!r} 已存在；请使用可安全创建和删除的一次性名称",
            )
            reporter.warn("为避免删除现有模态，已跳过全部写入和异常用例")
            raise SuiteAbort

        deploy_request_id = ids.next()

        def deploy() -> dict[str, Any]:
            nonlocal deployed
            result = client.deploy(
                args.node_id, args.modality,
                request_id=deploy_request_id,
                compute_config_percent=args.compute_config_percent,
                storage_config_mb=args.storage_config_mb,
                forwarding_config_mbps=args.forwarding_config_mbps,
            )
            if _response_code(result) == 0:
                deployed = True
            data = validate_success(result, deploy_request_id, reporter=reporter)
            validate_deploy_data(data)
            return data

        deploy_outcome = reporter.run(
            "正常流程/模态部署",
            deploy,
            input_summary=(
                "POST /modality/deploy | "
                f"timestamp_ms=自动, request_id={deploy_request_id}, "
                f"node_id={args.node_id}, modality={args.modality}, "
                f"compute={args.compute_config_percent}%, "
                f"storage={args.storage_config_mb}MB, "
                f"forwarding={args.forwarding_config_mbps}Mbps"
            ),
            output_summary=lambda data: (
                f"code=0, request_id={data['request_id']}, "
                f"modality_ip={data['modality_ip']}, "
                f"modality_port={data['modality_port']}"
            ),
        )
        if not deploy_outcome.ok:
            reporter.warn("模态部署未通过，已跳过后续测试")
            raise SuiteAbort

        config_request_id = ids.next()

        def configure() -> dict[str, Any]:
            result = client.request(
                "POST",
                "/resource/config",
                json_body=_config_body(
                    config_request_id,
                    args.node_id,
                    args.modality,
                    args.compute_config_percent,
                    args.storage_config_mb,
                    args.forwarding_config_mbps,
                ),
            )
            return validate_success(result, config_request_id, reporter=reporter)

        reporter.run(
            "正常流程/资源配置",
            configure,
            input_summary=(
                "POST /resource/config | "
                f"timestamp_ms=自动, request_id={config_request_id}, "
                f"node_id={args.node_id}, modalities_resource="
                f"[{args.modality}: compute={args.compute_config_percent}%, "
                f"storage={args.storage_config_mb}MB, "
                f"forwarding={args.forwarding_config_mbps}Mbps]"
            ),
            output_summary=lambda data: (
                f"code=0, request_id={data['request_id']}"
            ),
        )

        status_request_id = ids.next()

        def read_after_config() -> dict[str, Any]:
            result = client.request(
                "GET",
                "/resource/status",
                query={"node_id": args.node_id, "request_id": status_request_id},
            )
            data = validate_success(
                result,
                status_request_id,
                reporter=reporter,
                allow_string_request_id=True,
            )
            modalities = validate_status_data(
                data,
                node_id=args.node_id,
                required_modality=args.modality,
            )
            return {
                "request_id": data["request_id"],
                "node_id": data["node_id"],
                "node_resource": data["node_resource"],
                "modalities": modalities,
            }

        reporter.run(
            "正常流程/资源读取",
            read_after_config,
            input_summary=(
                "GET /resource/status | "
                f"node_id={args.node_id}, request_id={status_request_id}"
            ),
            output_summary=_status_output,
        )

        delete_request_id = ids.next()

        def delete() -> dict[str, Any]:
            nonlocal deployed
            result = client.delete(
                args.node_id, args.modality, request_id=delete_request_id,
            )
            if _response_code(result) == 0:
                deployed = False
            return validate_success(result, delete_request_id, reporter=reporter)

        delete_outcome = reporter.run(
            "正常流程/模态删除",
            delete,
            input_summary=(
                "POST /modality/delete | "
                f"timestamp_ms=自动, request_id={delete_request_id}, "
                f"node_id={args.node_id}, modality={args.modality}"
            ),
            output_summary=lambda data: (
                f"code=0, request_id={data['request_id']}"
            ),
        )
        if not delete_outcome.ok:
            reporter.warn("正常删除未通过，清理阶段将再次尝试删除测试模态")
            raise SuiteAbort

        if args.robustness:
            run_robustness_tests(args, client, ids, reporter)
    except SuiteAbort:
        pass
    except KeyboardInterrupt:
        interrupted = True
        reporter.fail("运行中断", "收到 KeyboardInterrupt")
    finally:
        if deployed:
            print("\n执行 finally 清理：")
            cleanup_outcome = reporter.run(
                "清理/finally 删除测试模态",
                lambda: _positive_delete(
                    client,
                    ids,
                    reporter,
                    args.node_id,
                    args.modality,
                ),
            )
            if cleanup_outcome.ok:
                deployed = False

    reporter.summary()
    if interrupted:
        return 1
    return 0 if reporter.failed == 0 else 1


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("端口必须是整数") from error
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("端口必须在 1..65535 之间")
    return port


def _positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("必须是数值") from error
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("必须是大于 0 的有限数值")
    return number


def _nonnegative_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("必须是数值") from error
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("必须是大于等于 0 的有限数值")
    return number


def _percent(value: str) -> float:
    number = _nonnegative_float(value)
    if number > 100:
        raise argparse.ArgumentTypeError("百分比不能大于 100")
    return number


def _nonempty(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise argparse.ArgumentTypeError("不能为空")
    return cleaned


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="测试 api.md 中定义的资源与模态接口（会执行写操作）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--ip", required=True, type=_nonempty, help="目标 IP 或主机名")
    parser.add_argument("--port", required=True, type=_port, help="目标 HTTP 端口")
    parser.add_argument("--node-id", required=True, type=_nonempty, help="目标节点 ID")
    parser.add_argument(
        "--modality",
        required=True,
        type=_nonempty,
        help="可安全部署和删除的一次性测试模态名称",
    )
    parser.add_argument(
        "--scheme",
        choices=("http", "https"),
        default="http",
        help="URL 协议",
    )
    parser.add_argument(
        "--timeout",
        type=_positive_float,
        default=3.0,
        help="单个 HTTP 请求超时秒数",
    )
    parser.add_argument(
        "--compute-config-percent",
        type=_percent,
        default=10.0,
        help="测试计算资源配额百分比",
    )
    parser.add_argument(
        "--storage-config-mb",
        type=_nonnegative_float,
        default=128.0,
        help="测试存储资源配额 MB",
    )
    parser.add_argument(
        "--forwarding-config-mbps",
        type=_nonnegative_float,
        default=10.0,
        help="测试转发资源配额 Mbps",
    )
    parser.add_argument(
        "--robustness",
        action="store_true",
        help="额外执行字段缺失、类型错误和非法 JSON 健壮性测试",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="输出每个 HTTP 请求和响应正文",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run_suite(args)
    except ValueError as error:
        print(f"参数错误: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
