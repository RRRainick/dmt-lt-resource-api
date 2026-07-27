"""从 config.json 读取 HTTP 服务参数并启动 ResourceManager。"""

from __future__ import annotations

import os

import uvicorn

from .main import _as_bool, _load_config, _setting


def main() -> None:
    config = _load_config()
    host = str(_setting(config, "server", "host", "SERVER_HOST", "0.0.0.0"))
    port = int(_setting(config, "server", "port", "SERVER_PORT", 9001))
    workers = int(_setting(config, "server", "workers", "SERVER_WORKERS", 1))
    uvicorn.run(
        "resource_manager.main:app",
        host=host,
        port=port,
        workers=workers,
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
    )


if __name__ == "__main__":
    main()
