from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from .models import NodeResult, RequestIdCounter, now_ms


class DecisionSystemError(RuntimeError):
    """课题四接口调用失败。"""


class DecisionSystem:
    """处理课题三与课题四之间的资源配置流程。"""

    def __init__(
        self,
        base_url: str,
        timeout_seconds: float = 3.0,
        request_id_start: int = 1,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._opener = build_opener(ProxyHandler({}))
        self._res_cfg_request_ids = RequestIdCounter(request_id_start)

    def get_next_res_cfg(self) -> dict[str, Any]:
        """向课题四请求下一份资源配置策略。"""

        request_id = str(self._res_cfg_request_ids.next())
        response = self._post(
            "/getNextResCfg",
            {"data": {"request_id": request_id, "cur_time": now_ms()}},
        )
        self._check_response(response, "获取资源策略")

        data = response.get("data")
        if not isinstance(data, dict):
            raise DecisionSystemError("获取资源策略返回 data 不是对象")
        if str(data.get("request_id")) != request_id:
            raise DecisionSystemError("获取资源策略返回 request_id 与请求不一致")
        if not isinstance(data.get("policy_list", []), list):
            raise DecisionSystemError("获取资源策略返回 policy_list 不是数组")
        return data

    def post_impl_status(
        self,
        request_id: str | None,
        results: list[NodeResult],
    ) -> None:
        """把课题二各 node 的配置结果返回给课题四，不要求业务响应体。"""

        success_count = sum(result.success for result in results)
        if success_count == len(results):
            exec_status = "success"
        elif success_count == 0:
            exec_status = "failed"
        else:
            exec_status = "partial_success"

        data = {
            "request_id": request_id or str(self._res_cfg_request_ids.next()),
            "task_type": "res_cfg",
            "exec_status": exec_status,
            "reason_code": self._get_reason_code(exec_status, results),
            "res": "resource config finished",
            "detail": {
                "node_results": [
                    {
                        "node_id": result.node_id,
                        "success": result.success,
                        "data": result.data,
                        "error": result.error,
                    }
                    for result in results
                ]
            },
        }
        self._post("/postImplStatus", {"data": data}, expect_json=False)

    @staticmethod
    def _get_reason_code(
        exec_status: str,
        results: list[NodeResult],
    ) -> str:
        if exec_status == "success":
            return "ok"
        if any(result.error and "timeout" in result.error.lower() for result in results):
            return "timeout"
        return "internal_error"

    def _post(
        self,
        path: str,
        body: dict[str, Any],
        *,
        expect_json: bool = True,
    ) -> dict[str, Any] | None:
        request = Request(
            f"{self.base_url}{path}",
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as error:
            body_text = error.read().decode("utf-8", errors="replace")
            raise DecisionSystemError(f"HTTP {error.code}：{body_text}") from error
        except (URLError, TimeoutError) as error:
            raise DecisionSystemError(f"课题四请求失败：{error}") from error

        if not expect_json:
            return None

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as error:
            raise DecisionSystemError(f"课题四返回的不是合法 JSON：{raw}") from error
        if not isinstance(data, dict):
            raise DecisionSystemError("课题四返回的 JSON 不是对象")
        return data

    @staticmethod
    def _check_response(response: dict[str, Any], action: str) -> None:
        if response.get("code") != 0:
            raise DecisionSystemError(
                f"{action}失败：code={response.get('code')}，msg={response.get('msg', '')}"
            )
