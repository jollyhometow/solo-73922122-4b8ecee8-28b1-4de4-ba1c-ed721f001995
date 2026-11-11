"""测试辅助：在后台线程中启动本地演示维修门户。"""

from __future__ import annotations

import socket
import threading
import time

import uvicorn

from portal.app import PortalSettings, create_app


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class PortalServer:
    """上下文管理器：uvicorn 线程内运行演示门户。"""

    def __init__(self, settings: PortalSettings | None = None) -> None:
        self.settings = settings or PortalSettings(latency_ms=0)
        self.port = free_port()
        config = uvicorn.Config(
            create_app(self.settings),
            host="127.0.0.1",
            port=self.port,
            log_level="error",
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(
            target=self.server.run, name="portal-test-server", daemon=True
        )

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> "PortalServer":
        self.thread.start()
        deadline = time.time() + 15
        while not self.server.started:
            if time.time() > deadline:
                raise RuntimeError("演示门户未能在超时内启动")
            time.sleep(0.01)
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=15)
