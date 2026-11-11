"""演示门户的确定性数据集。

数据由月份字符串哈希驱动，进程重启后内容保持一致，这样采集端
重试、续采时看到的工单集合不变，便于演示“已提交工单不重复”。
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from datetime import datetime, timezone

# 已封存（可采集）的月份
ARCHIVED_MONTHS: tuple[str, ...] = ("2025-08", "2025-09", "2025-10")

ORDERS_PER_MONTH = 7
PAGE_SIZE = 3

FLEET: tuple[str, ...] = (
    "沪A10203",
    "沪A10455",
    "沪B20776",
    "沪A30918",
    "沪B33210",
    "沪A41827",
    "沪A45560",
    "沪B52008",
    "沪A61139",
    "沪B70842",
)

FAULT_CODES: tuple[str, ...] = (
    "P0301",
    "P0420",
    "P0171",
    "P0562",
    "P0700",
    "B0021",
    "B1318",
    "U0100",
    "U0121",
    "C0035",
)


@dataclass(frozen=True)
class WorkOrder:
    work_order_no: str  # 工单号（唯一）
    vehicle_no: str  # 车辆号
    fault_code: str  # 故障码
    completed_at: int  # 完工时间，UTC 整数秒
    month: str  # 所属封存月份 YYYY-MM


def orders_for_month(month: str) -> list[WorkOrder]:
    """生成某封存月份的完工工单，按完工时间升序、工单号顺序编号。"""

    year = int(month[0:4])
    mon = int(month[5:7])
    if not 1 <= mon <= 12:
        raise ValueError(f"非法月份: {month!r}")
    month_start = int(datetime(year, mon, 1, tzinfo=timezone.utc).timestamp())
    seed = int(
        hashlib.sha256(f"maintenance-portal/{month}".encode("utf-8")).hexdigest()[:16],
        16,
    )
    rng = random.Random(seed)

    raw: list[tuple[int, str, str]] = []
    for _ in range(ORDERS_PER_MONTH):
        raw.append(
            (
                month_start + rng.randrange(0, 27 * 24 * 3600),
                FLEET[rng.randrange(len(FLEET))],
                FAULT_CODES[rng.randrange(len(FAULT_CODES))],
            )
        )
    raw.sort()

    return [
        WorkOrder(
            work_order_no=f"WO-{year}{mon:02d}-{seq:03d}",
            vehicle_no=vehicle,
            fault_code=fault,
            completed_at=completed_at,
            month=month,
        )
        for seq, (completed_at, vehicle, fault) in enumerate(raw, start=1)
    ]


def build_dataset() -> dict[str, list[WorkOrder]]:
    return {month: orders_for_month(month) for month in ARCHIVED_MONTHS}
