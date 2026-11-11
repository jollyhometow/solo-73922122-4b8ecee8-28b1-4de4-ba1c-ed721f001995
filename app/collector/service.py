"""按月采集任务管理：单进程内同任务只运行一次，失败/中断后可续采。"""

from __future__ import annotations

import threading
from typing import Any, Callable
from urllib.parse import urljoin

from .config import CollectorSettings
from .parsing import ParseError, parse_detail_page, parse_list_page
from .portal_client import FetchError, PortalClient
from .storage import Storage


class CollectionManager:
    """采集任务生命周期管理。

    服务以单 API 进程运行，因此用进程内集合即可保证“同任务并发触发
    只运行一次”。任务状态与游标全部落在 SQLite，进程重启后从游标
    页续采。
    """

    def __init__(
        self,
        settings: CollectorSettings,
        client_factory: Callable[[], PortalClient] | None = None,
    ) -> None:
        self._settings = settings
        self._storage = Storage(settings.db_path)
        self._client_factory = client_factory or self._default_client_factory
        self._lock = threading.Lock()
        self._running: set[str] = set()
        # 进程重启后，此前“运行中”的任务视为被中断：游标未推进，可续采。
        self._storage.reset_interrupted()

    def _default_client_factory(self) -> PortalClient:
        return PortalClient(
            base_url=self._settings.portal_base_url,
            username=self._settings.portal_username,
            password=self._settings.portal_password,
            timeout=self._settings.http_timeout,
        )

    @property
    def storage(self) -> Storage:
        return self._storage

    # ------------------------------------------------------------------
    # 触发
    # ------------------------------------------------------------------
    def trigger(self, month: str) -> str:
        """触发采集，返回 started / running / completed。"""

        with self._lock:
            if month in self._running:
                return "running"
            task = self._storage.get_task(month)
            if task is not None and task["status"] == "completed":
                return "completed"
            self._running.add(month)
            # 同步落库 running 状态，让紧随其后的进度查询立即可见；
            # 已有任务保留游标，实现续采。
            self._storage.mark_running(month, self._settings.start_url(month))

        thread = threading.Thread(
            target=self._run_guarded,
            args=(month,),
            name=f"collect-{month}",
            daemon=True,
        )
        thread.start()
        return "started"

    def _run_guarded(self, month: str) -> None:
        try:
            self.crawl(month)
        except Exception as exc:  # 兜底：任何意外都落库为失败，等待续采
            try:
                self._storage.mark_failed(month, f"采集内部错误: {exc!r}")
            except Exception:
                pass
        finally:
            with self._lock:
                self._running.discard(month)

    # ------------------------------------------------------------------
    # 采集主循环
    # ------------------------------------------------------------------
    def crawl(self, month: str) -> None:
        """从游标页开始采集；整页成功才推进游标。可被测试同步调用。"""

        settings = self._settings
        self._storage.mark_running(month, settings.start_url(month))
        client = self._client_factory()
        try:
            client.login()
            while True:
                task = self._storage.get_task(month)
                page_url = (task or {}).get("next_page_url") or settings.start_url(
                    month
                )

                list_html = client.fetch(page_url)
                detail_hrefs, next_href = parse_list_page(list_html)

                records: list[dict[str, Any]] = []
                for href in detail_hrefs:
                    detail_url = urljoin(page_url, href)
                    detail_html = client.fetch(detail_url)
                    fields = parse_detail_page(detail_html)
                    records.append(
                        {**fields, "page_url": page_url, "detail_url": detail_url}
                    )

                next_url = urljoin(page_url, next_href) if next_href else None
                # 整页详情全部获取并校验后，记录与下一页地址一起提交。
                self._storage.commit_page(month, records, next_url)
                if next_url is None:
                    self._storage.mark_completed(month)
                    return
        except (FetchError, ParseError) as exc:
            # 断流、坏详情、重登后仍失效：不推进当前页，等待续采。
            self._storage.mark_failed(month, str(exc))
        finally:
            client.close()

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def task_view(self, month: str) -> dict[str, Any] | None:
        task = self._storage.get_task(month)
        if task is None:
            return None
        task["total_records"] = self._storage.count_records(month)
        return task

    def list_task_views(self) -> list[dict[str, Any]]:
        views = []
        for task in self._storage.list_tasks():
            task["total_records"] = self._storage.count_records(task["month"])
            views.append(task)
        return views
