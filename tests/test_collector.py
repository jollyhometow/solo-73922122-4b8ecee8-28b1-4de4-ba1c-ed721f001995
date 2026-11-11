import os
import tempfile
import unittest

import httpx

from app.collector.config import CollectorSettings
from app.collector.portal_client import PortalClient
from app.collector.service import CollectionManager
from portal.app import PortalSettings
from tests.portal_test_utils import PortalServer

MONTH = "2025-10"  # 7 份工单，3 页（3+3+1）；WO-202510-005 首次详情断流


class CollectorCrawlTests(unittest.TestCase):
    """采集端端到端：翻页前会话过期重登、详情断流停页、续采、去重。"""

    @classmethod
    def setUpClass(cls):
        cls.portal = PortalServer().__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.portal.__exit__(None, None, None)

    def setUp(self):
        httpx.post(f"{self.portal.base_url}/_demo/reset")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.created_clients: list[PortalClient] = []

    def make_manager(self) -> CollectionManager:
        settings = CollectorSettings(
            portal_base_url=self.portal.base_url,
            portal_username="demo",
            portal_password="demo123",
            db_path=os.path.join(self.tmp.name, "collector.db"),
            http_timeout=5.0,
        )

        def factory() -> PortalClient:
            client = PortalClient(
                base_url=settings.portal_base_url,
                username=settings.portal_username,
                password=settings.portal_password,
                timeout=settings.http_timeout,
            )
            self.created_clients.append(client)
            return client

        return CollectionManager(settings, client_factory=factory)

    def test_first_crawl_stops_at_broken_detail_and_keeps_previous_pages(self):
        manager = self.make_manager()
        manager.crawl(MONTH)

        task = manager.storage.get_task(MONTH)
        self.assertEqual(task["status"], "failed")
        self.assertIn("WO-202510-005", task["last_error"])
        # 第一页已整页提交；游标停在第二页，未推进
        self.assertEqual(task["pages_committed"], 1)
        self.assertEqual(task["records_committed"], 3)
        self.assertIn("page=2", task["next_page_url"])
        self.assertEqual(manager.storage.count_records(MONTH), 3)
        # 翻页前会话过期 → 采集过程中发生过重新登录
        self.assertGreaterEqual(self.created_clients[0].login_count, 2)

    def test_resume_after_failure_completes_without_duplicates(self):
        manager = self.make_manager()
        manager.crawl(MONTH)
        self.assertEqual(manager.storage.get_task(MONTH)["status"], "failed")

        # 断流详情只失败一次：重新触发（续采）后从第二页继续直至完成
        manager.crawl(MONTH)
        task = manager.storage.get_task(MONTH)
        self.assertEqual(task["status"], "completed")
        self.assertIsNone(task["next_page_url"])
        self.assertEqual(task["pages_committed"], 3)

        records = manager.storage.list_records(MONTH, limit=100, offset=0)
        self.assertEqual(len(records), 7)
        order_numbers = [r["work_order_no"] for r in records]
        self.assertEqual(len(set(order_numbers)), 7)  # 已提交工单不重复
        self.assertEqual(
            sorted(order_numbers),
            [f"WO-202510-{seq:03d}" for seq in range(1, 8)],
        )
        for record in records:
            self.assertTrue(record["vehicle_no"])
            self.assertTrue(record["fault_code"])
            self.assertIsInstance(record["completed_at"], int)

    def test_relogin_on_every_page_turn_when_budget_is_one(self):
        manager = self.make_manager()
        manager.crawl("2025-09")  # 无故障注入的月份：3 页
        task = manager.storage.get_task("2025-09")
        self.assertEqual(task["status"], "completed")
        self.assertEqual(manager.storage.count_records("2025-09"), 7)
        # 初始登录 1 次 + 每次翻页前会话过期重登 2 次
        self.assertEqual(self.created_clients[0].login_count, 3)

    def test_second_login_failure_stops_at_current_page(self):
        # 会话预算为 0：任何列表页都会跳回登录，重登一次后仍失败 → 停在当前页
        with PortalServer(
            PortalSettings(latency_ms=0, session_page_budget=0)
        ) as strict_portal:
            settings = CollectorSettings(
                portal_base_url=strict_portal.base_url,
                portal_username="demo",
                portal_password="demo123",
                db_path=os.path.join(self.tmp.name, "strict.db"),
                http_timeout=5.0,
            )
            manager = CollectionManager(settings)
            manager.crawl(MONTH)
            task = manager.storage.get_task(MONTH)
            self.assertEqual(task["status"], "failed")
            self.assertIn("重新登录后仍跳回登录页", task["last_error"])
            self.assertEqual(task["pages_committed"], 0)
            self.assertEqual(task["next_page_url"], settings.start_url(MONTH))
            self.assertEqual(manager.storage.count_records(MONTH), 0)

    def test_restart_resets_interrupted_task_and_resumes(self):
        manager = self.make_manager()
        manager.crawl(MONTH)  # 停在第二页（断流详情）
        self.assertEqual(manager.storage.get_task(MONTH)["status"], "failed")

        # 模拟进程中断后重启：新管理器把遗留 running 标记为失败，游标不动
        manager.storage.mark_running(MONTH, "http://unused")
        restarted = self.make_manager()
        task = restarted.storage.get_task(MONTH)
        self.assertEqual(task["status"], "failed")
        self.assertIn("page=2", task["next_page_url"])

        restarted.crawl(MONTH)
        task = restarted.storage.get_task(MONTH)
        self.assertEqual(task["status"], "completed")
        self.assertEqual(restarted.storage.count_records(MONTH), 7)


if __name__ == "__main__":
    unittest.main(verbosity=2)
