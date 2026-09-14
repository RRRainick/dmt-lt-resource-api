#!/usr/bin/env python3
"""Run a single modality deploy or delete operation."""
from __future__ import annotations

import argparse
import json
import math
import sys
from typing import Sequence

from modality_client import ModalityClient, TransportFailure, build_base_url


def nonempty(value: str) -> str:
    value = value.strip()
    if not value:
        raise argparse.ArgumentTypeError("不能为空")
    return value


def port(value: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("端口必须是整数") from error
    if not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError("端口必须在 1..65535 之间")
    return number


def nonnegative(value: str) -> float:
    try:
        number = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("必须是数值") from error
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("必须是非负有限数值")
    return number


def percent(value: str) -> float:
    number = nonnegative(value)
    if number > 100:
        raise argparse.ArgumentTypeError("百分比不能大于 100")
    return number


def timeout(value: str) -> float:
    number = nonnegative(value)
    if number == 0:
        raise argparse.ArgumentTypeError("超时必须大于 0")
    return number


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", required=True, choices=("deploy", "delete"))
    parser.add_argument("--modality", required=True, type=nonempty)
    parser.add_argument("--ip", required=True, type=nonempty)
    parser.add_argument("--port", required=True, type=port)
    parser.add_argument("--node-id", required=True, type=nonempty)
    parser.add_argument("--scheme", choices=("http", "https"), default="http")
    parser.add_argument("--timeout", type=timeout, default=3.0, help="超时秒数，默认 3")
    parser.add_argument("--compute-config-percent", type=percent, help="部署计算配额，默认 10%%")
    parser.add_argument("--storage-config-mb", type=nonnegative, help="部署存储配额，默认 128 MB")
    parser.add_argument("--forwarding-config-mbps", type=nonnegative, help="部署转发配额，默认 10 Mbps")
    args = parser.parse_args(argv)
    defaults = {"compute_config_percent": 10.0, "storage_config_mb": 128.0,
                "forwarding_config_mbps": 10.0}
    for name, default in defaults.items():
        if getattr(args, name) is not None and args.action == "delete":
            parser.error("delete 不接受资源配额参数")
        if getattr(args, name) is None:
            setattr(args, name, default)
    try:
        args.base_url = build_base_url(args.scheme, args.ip, args.port)
    except ValueError as error:
        parser.error(str(error))
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    client = ModalityClient(args.base_url, args.timeout)
    try:
        if args.action == "deploy":
            result = client.deploy(
                args.node_id, args.modality,
                compute_config_percent=args.compute_config_percent,
                storage_config_mb=args.storage_config_mb,
                forwarding_config_mbps=args.forwarding_config_mbps,
            )
        else:
            result = client.delete(args.node_id, args.modality)
    except (TransportFailure, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1

    if result.json_error is not None:
        print(f"HTTP {result.status}: 响应不是合法 JSON: {result.body!r}", file=sys.stderr)
        return 1
    print(json.dumps(result.payload, ensure_ascii=False, indent=2))
    payload = result.payload
    if not 200 <= result.status < 300:
        print(f"请求失败: HTTP {result.status}", file=sys.stderr)
        return 1
    if (not isinstance(payload, dict) or type(payload.get("code")) is not int
            or payload["code"] != 0):
        print("业务失败或响应格式错误: 需要整数 code=0", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
