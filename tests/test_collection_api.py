import os
import tempfile
import time
import unittest

import httpx
from fastapi.testclient import TestClient

from app.main import app
from portal.app import PortalSettings
from tests.portal_test_utils import PortalServer

MONTH = "2025-10"
ENV_KEYS = (
    "PORTAL_BASE_URL",
    "PORTAL_USERNAME",
    "PORTAL_PASSWORD",
    "COLLECTOR_DB_PATH",
)


def wait_for_terminal(client: TestClient, month: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        view = client.get(f"/api/collection/tasks/{month}").json()
        if view["status"] != "running":
            return view
        time.sleep(0.05)
    raise AssertionError(f"任务 {month} 未在 {timeout}s 内结束运行")


class CollectionApiTests(unittest.TestCase):
    """采集 API：触发、并发去重、失败续采、进度与记录查询。"""

    @classmethod
    def setUpClass(cls):
        # 门户带 100ms 模拟延迟，保证“运行中”状态可被并发触发观察到
        cls.portal = PortalServer(PortalSettings(latency_ms=100)).__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.portal.__exit__(None, None, None)

    def setUp(self):
        httpx.post(f"{self.portal.base_url}/_demo/reset")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._saved_env = {key: os.environ.get(key) for key in ENV_KEYS}
        os.environ["PORTAL_BASE_URL"] = self.portal.base_url
        os.environ["PORTAL_USERNAME"] = "demo"
        os.environ["PORTAL_PASSWORD"] = "demo123"
        os.environ["COLLECTOR_DB_PATH"] = os.path.join(self.tmp.name, "collector.db")
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_collect_resume_and_query_flow(self):
        with TestClient(app) as client:
            # 1. 触发采集 → 202 started
            first = client.post("/api/collection/tasks", json={"month": MONTH})
            self.assertEqual(first.status_code, 202)
            self.assertEqual(first.json()["status"], "started")

            # 2. 同任务并发触发 → 只运行一次，其余返回运行中状态
            second = client.post("/api/collection/tasks", json={"month": MONTH})
            self.assertEqual(second.status_code, 200)
            self.assertEqual(second.json()["status"], "running")

            # 3. 首轮运行：第二页第二份详情断流 → 停在第二页，第一页保留
            view = wait_for_terminal(client, MONTH)
            self.assertEqual(view["status"], "failed")
            self.assertEqual(view["pages_committed"], 1)
            self.assertEqual(view["total_records"], 3)
            self.assertIn("page=2", view["next_page_url"])
            self.assertIn("WO-202510-005", view["last_error"])

            # 4. 再次触发即续采 → 从第二页继续直至完成
            resumed = client.post("/api/collection/tasks", json={"month": MONTH})
            self.assertEqual(resumed.status_code, 202)
            self.assertEqual(resumed.json()["status"], "started")
            view = wait_for_terminal(client, MONTH)
            self.assertEqual(view["status"], "completed")
            self.assertIsNone(view["next_page_url"])
            self.assertEqual(view["pages_committed"], 3)
            self.assertEqual(view["total_records"], 7)

            # 5. 已采记录查询：7 份工单、字段齐全、不重复
            response = client.get(f"/api/collection/tasks/{MONTH}/records")
            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertEqual(body["total"], 7)
            self.assertEqual(len(body["records"]), 7)
            order_numbers = [r["work_order_no"] for r in body["records"]]
            self.assertEqual(len(set(order_numbers)), 7)
            for record in body["records"]:
                self.assertTrue(record["vehicle_no"])
                self.assertTrue(record["fault_code"])
                self.assertIsInstance(record["completed_at"], int)

            # 分页参数
            page = client.get(
                f"/api/collection/tasks/{MONTH}/records", params={"limit": 3, "offset": 6}
            )
            self.assertEqual(page.status_code, 200)
            self.assertEqual(len(page.json()["records"]), 1)

            # 6. 已完成任务再次触发 → completed，不重复运行
            again = client.post("/api/collection/tasks", json={"month": MONTH})
            self.assertEqual(again.status_code, 200)
            self.assertEqual(again.json()["status"], "completed")

            # 7. 任务列表包含该月份
            tasks = client.get("/api/collection/tasks").json()["tasks"]
            self.assertIn(MONTH, [task["month"] for task in tasks])

    def test_validation_and_unknown_task(self):
        with TestClient(app) as client:
            bad_month = client.post("/api/collection/tasks", json={"month": "2025-13"})
            self.assertEqual(bad_month.status_code, 422)

            bad_format = client.post("/api/collection/tasks", json={"month": "202510"})
            self.assertEqual(bad_format.status_code, 422)

            missing_field = client.post("/api/collection/tasks", json={})
            self.assertEqual(missing_field.status_code, 422)

            unknown = client.get("/api/collection/tasks/2099-01")
            self.assertEqual(unknown.status_code, 404)

            unknown_records = client.get("/api/collection/tasks/2099-01/records")
            self.assertEqual(unknown_records.status_code, 404)

            invalid_path_month = client.get("/api/collection/tasks/2025-13")
            self.assertEqual(invalid_path_month.status_code, 422)

    def test_collection_endpoints_are_in_openapi_docs(self):
        with TestClient(app) as client:
            openapi = client.get("/openapi.json").json()
            self.assertIn("/api/collection/tasks", openapi["paths"])
            self.assertIn("/api/collection/tasks/{month}", openapi["paths"])
            self.assertIn(
                "/api/collection/tasks/{month}/records", openapi["paths"]
            )
            # 原接口保留
            self.assertIn("/suppress", openapi["paths"])
            self.assertIn("/health", openapi["paths"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
