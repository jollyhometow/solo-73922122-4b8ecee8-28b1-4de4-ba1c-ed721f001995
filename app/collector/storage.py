"""SQLite 持久化：完工记录与采集游标（当前页地址）。

整页记录与下一页地址在同一事务提交；断流、坏详情或进程中断都不会
推进游标，重启后从游标页续采；已提交工单按 (month, work_order_no)
主键去重。
"""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator, Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS collection_tasks (
    month TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    next_page_url TEXT,
    pages_committed INTEGER NOT NULL DEFAULT 0,
    records_committed INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    started_at INTEGER,
    finished_at INTEGER,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS work_orders (
    month TEXT NOT NULL,
    work_order_no TEXT NOT NULL,
    vehicle_no TEXT NOT NULL,
    fault_code TEXT NOT NULL,
    completed_at INTEGER NOT NULL,
    page_url TEXT NOT NULL,
    detail_url TEXT NOT NULL,
    collected_at INTEGER NOT NULL,
    PRIMARY KEY (month, work_order_no)
);
"""


class Storage:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        directory = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(directory, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 任务状态
    # ------------------------------------------------------------------
    def reset_interrupted(self) -> int:
        """进程重启后，把遗留 running 任务标记为失败（游标未推进，可续采）。"""

        now = int(time.time())
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE collection_tasks SET status='failed', last_error=?, "
                "updated_at=? WHERE status='running'",
                ("进程中断，任务停止；重新触发即可从当前页续采", now),
            )
            return cursor.rowcount

    def get_task(self, month: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM collection_tasks WHERE month=?", (month,)
            ).fetchone()
            return dict(row) if row else None

    def list_tasks(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM collection_tasks ORDER BY month"
            ).fetchall()
            return [dict(row) for row in rows]

    def mark_running(self, month: str, start_url: str) -> None:
        """置为运行中；已有任务保留游标（续采），新任务从起始页开始。"""

        now = int(time.time())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO collection_tasks
                    (month, status, next_page_url, started_at, updated_at)
                VALUES (?, 'running', ?, ?, ?)
                ON CONFLICT(month) DO UPDATE SET
                    status='running',
                    started_at=excluded.started_at,
                    finished_at=NULL,
                    last_error=NULL,
                    updated_at=excluded.updated_at
                """,
                (month, start_url, now, now),
            )

    def commit_page(
        self,
        month: str,
        records: Sequence[dict[str, Any]],
        next_page_url: str | None,
    ) -> int:
        """整页记录与下一页地址同事务提交；返回实际新增记录数。"""

        now = int(time.time())
        inserted = 0
        with self._connect() as conn:
            for record in records:
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO work_orders
                        (month, work_order_no, vehicle_no, fault_code,
                         completed_at, page_url, detail_url, collected_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        month,
                        record["work_order_no"],
                        record["vehicle_no"],
                        record["fault_code"],
                        record["completed_at"],
                        record["page_url"],
                        record["detail_url"],
                        now,
                    ),
                )
                inserted += cursor.rowcount
            conn.execute(
                """
                UPDATE collection_tasks
                SET next_page_url=?,
                    pages_committed=pages_committed+1,
                    records_committed=records_committed+?,
                    updated_at=?
                WHERE month=?
                """,
                (next_page_url, inserted, now, month),
            )
        return inserted

    def mark_failed(self, month: str, error: str) -> None:
        """标记失败；游标保持不动，重新触发即从当前页续采。"""

        now = int(time.time())
        with self._connect() as conn:
            conn.execute(
                "UPDATE collection_tasks SET status='failed', last_error=?, "
                "finished_at=?, updated_at=? WHERE month=?",
                (error, now, now, month),
            )

    def mark_completed(self, month: str) -> None:
        now = int(time.time())
        with self._connect() as conn:
            conn.execute(
                "UPDATE collection_tasks SET status='completed', "
                "next_page_url=NULL, last_error=NULL, finished_at=?, "
                "updated_at=? WHERE month=?",
                (now, now, month),
            )

    # ------------------------------------------------------------------
    # 记录查询
    # ------------------------------------------------------------------
    def count_records(self, month: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM work_orders WHERE month=?", (month,)
            ).fetchone()
            return int(row["n"])

    def list_records(
        self, month: str, limit: int, offset: int
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT work_order_no, vehicle_no, fault_code, completed_at,
                       page_url, detail_url, collected_at
                FROM work_orders
                WHERE month=?
                ORDER BY completed_at, work_order_no
                LIMIT ? OFFSET ?
                """,
                (month, limit, offset),
            ).fetchall()
            return [dict(row) for row in rows]
