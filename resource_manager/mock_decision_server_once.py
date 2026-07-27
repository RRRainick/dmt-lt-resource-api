"""仅执行一次资源配置验证的模拟课题四决策服务。"""

from __future__ import annotations

from .mock_decision_server import run


def main() -> None:
    run(once=True)


if __name__ == "__main__":
    main()
