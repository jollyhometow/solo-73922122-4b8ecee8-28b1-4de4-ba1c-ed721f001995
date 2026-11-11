"""采集器配置：HTTP 源地址与演示登录信息由环境变量固定。"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class CollectorSettings:
    portal_base_url: str = "http://localhost:8080"
    portal_username: str = "demo"
    portal_password: str = "demo123"
    db_path: str = "collector.db"
    http_timeout: float = 10.0

    @classmethod
    def from_env(cls) -> "CollectorSettings":
        return cls(
            portal_base_url=os.environ.get("PORTAL_BASE_URL", "http://localhost:8080"),
            portal_username=os.environ.get("PORTAL_USERNAME", "demo"),
            portal_password=os.environ.get("PORTAL_PASSWORD", "demo123"),
            db_path=os.environ.get("COLLECTOR_DB_PATH", "collector.db"),
            http_timeout=float(os.environ.get("COLLECTOR_HTTP_TIMEOUT", "10")),
        )

    def start_url(self, month: str) -> str:
        """某月份第一页列表地址；之后完全沿页面中的下一页链接取数。"""

        return f"{self.portal_base_url}/workorders?month={month}"
