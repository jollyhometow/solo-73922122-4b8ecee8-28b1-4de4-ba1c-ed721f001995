import os
import tempfile
import unittest

from app.collector.storage import Storage


def make_records(page_url: str, numbers) -> list[dict]:
    return [
        {
            "work_order_no": f"WO-202510-{seq:03d}",
            "vehicle_no": "沪A10203",
            "fault_code": "P0301",
            "completed_at": 1730419200 + seq,
            "page_url": page_url,
            "detail_url": f"http://portal/workorders/WO-202510-{seq:03d}",
        }
        for seq in numbers
    ]


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "collector.db")
        self.storage = Storage(self.db_path)

    def test_mark_running_creates_task_and_keeps_cursor_on_resume(self):
        self.storage.mark_running("2025-10", "http://portal/workorders?month=2025-10")
        task = self.storage.get_task("2025-10")
        self.assertEqual(task["status"], "running")
        self.assertEqual(
            task["next_page_url"], "http://portal/workorders?month=2025-10"
        )

        # 模拟已推进到第 2 页后再次启动：游标必须保留（续采）
        self.storage.commit_page("2025-10", [], "http://portal/page2")
        self.storage.mark_running("2025-10", "http://portal/workorders?month=2025-10")
        task = self.storage.get_task("2025-10")
        self.assertEqual(task["next_page_url"], "http://portal/page2")
        self.assertEqual(task["pages_committed"], 1)

    def test_commit_page_is_atomic_and_deduplicates(self):
        self.storage.mark_running("2025-10", "http://portal/p1")
        records = make_records("http://portal/p1", [1, 2, 3])
        inserted = self.storage.commit_page("2025-10", records, "http://portal/p2")
        self.assertEqual(inserted, 3)
        self.assertEqual(self.storage.count_records("2025-10"), 3)

        task = self.storage.get_task("2025-10")
        self.assertEqual(task["next_page_url"], "http://portal/p2")
        self.assertEqual(task["records_committed"], 3)

        # 同一页重复提交：已提交工单不重复，游标仍可正常推进
        inserted = self.storage.commit_page("2025-10", records, "http://portal/p3")
        self.assertEqual(inserted, 0)
        self.assertEqual(self.storage.count_records("2025-10"), 3)
        task = self.storage.get_task("2025-10")
        self.assertEqual(task["records_committed"], 3)
        self.assertEqual(task["next_page_url"], "http://portal/p3")

    def test_mark_failed_keeps_cursor_and_records(self):
        self.storage.mark_running("2025-10", "http://portal/p1")
        self.storage.commit_page(
            "2025-10", make_records("http://portal/p1", [1, 2, 3]), "http://portal/p2"
        )
        self.storage.mark_failed("2025-10", "断流")
        task = self.storage.get_task("2025-10")
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["last_error"], "断流")
        self.assertEqual(task["next_page_url"], "http://portal/p2")
        self.assertEqual(self.storage.count_records("2025-10"), 3)

    def test_reset_interrupted_marks_running_as_failed(self):
        self.storage.mark_running("2025-10", "http://portal/p1")
        self.assertEqual(self.storage.reset_interrupted(), 1)
        task = self.storage.get_task("2025-10")
        self.assertEqual(task["status"], "failed")
        self.assertIn("中断", task["last_error"])
        # 已完成任务不受影响
        self.storage.mark_running("2025-09", "http://portal/p9")
        self.storage.mark_completed("2025-09")
        self.assertEqual(self.storage.reset_interrupted(), 0)
        self.assertEqual(self.storage.get_task("2025-09")["status"], "completed")

    def test_list_records_pagination(self):
        self.storage.mark_running("2025-10", "http://portal/p1")
        self.storage.commit_page(
            "2025-10", make_records("http://portal/p1", [1, 2, 3]), None
        )
        page1 = self.storage.list_records("2025-10", limit=2, offset=0)
        page2 = self.storage.list_records("2025-10", limit=2, offset=2)
        self.assertEqual(len(page1), 2)
        self.assertEqual(len(page2), 1)
        self.assertEqual(page1[0]["work_order_no"], "WO-202510-001")
        self.assertEqual(page2[0]["work_order_no"], "WO-202510-003")


if __name__ == "__main__":
    unittest.main(verbosity=2)
