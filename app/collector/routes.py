"""按月采集 API：触发/续采、进度查询、已采记录查询。"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, HTTPException, Path, Query, Request, Response
from pydantic import BaseModel, Field

from .service import CollectionManager

# 月份 YYYY-MM，MM 为 01..12
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"

router = APIRouter(prefix="/api/collection", tags=["collection"])


class StartTaskRequest(BaseModel):
    month: str = Field(
        ...,
        pattern=MONTH_PATTERN,
        description="采集月份，格式 YYYY-MM",
        examples=["2025-10"],
    )


class TaskView(BaseModel):
    month: str
    status: str = Field(
        description="running=运行中；completed=已完成；failed=失败/中断，可重新触发续采"
    )
    next_page_url: Optional[str] = Field(
        description="游标：下次续采的列表页地址；任务完成后为 null"
    )
    pages_committed: int = Field(description="已整页提交的页数")
    records_committed: int = Field(description="已提交记录数（去重后实际新增）")
    total_records: int = Field(description="库中该月份记录总数")
    last_error: Optional[str] = Field(description="最近一次失败原因")
    started_at: Optional[int] = Field(description="最近一次启动时间，UTC 秒")
    finished_at: Optional[int] = Field(description="最近一次结束时间，UTC 秒")
    updated_at: int = Field(description="状态更新时间，UTC 秒")


class StartTaskResponse(BaseModel):
    status: str = Field(
        description="started=本次新启动；running=同任务已在运行，未重复执行；completed=已完成"
    )
    message: str
    task: TaskView


class TaskListResponse(BaseModel):
    tasks: List[TaskView]


class RecordView(BaseModel):
    work_order_no: str = Field(description="工单号")
    vehicle_no: str = Field(description="车辆号")
    fault_code: str = Field(description="故障码")
    completed_at: int = Field(description="完工时间，UTC 整数秒")
    page_url: str = Field(description="来源列表页")
    detail_url: str = Field(description="来源详情页")
    collected_at: int = Field(description="入库时间，UTC 秒")


class RecordListResponse(BaseModel):
    month: str
    total: int
    limit: int
    offset: int
    records: List[RecordView]


def _manager(request: Request) -> CollectionManager:
    manager = getattr(request.app.state, "collection_manager", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="采集服务未初始化")
    return manager


def _task_or_404(manager: CollectionManager, month: str) -> dict:
    view = manager.task_view(month)
    if view is None:
        raise HTTPException(
            status_code=404, detail=f"月份 {month} 还没有采集任务，请先触发采集"
        )
    return view


@router.post(
    "/tasks",
    response_model=StartTaskResponse,
    summary="触发按月采集（失败或中断后再次调用即续采）",
    description=(
        "为指定月份启动完工记录采集。任务在后台运行：沿列表页下一页链接取数，"
        "整页详情全部获取并校验后，记录与下一页地址一起提交 SQLite。"
        "同月份任务并发触发只运行一次，其余返回 running；"
        "失败或进程中断后再次调用本接口即从游标页续采；已完成任务直接返回 completed。"
    ),
    response_description="已启动返回 202；已在运行或已完成返回 200",
    responses={
        202: {"description": "任务已启动"},
        200: {"description": "任务正在运行中，或此前已完成"},
        422: {"description": "月份格式非法"},
    },
)
def start_collection_task(
    payload: StartTaskRequest, request: Request, response: Response
) -> StartTaskResponse:
    manager = _manager(request)
    outcome = manager.trigger(payload.month)
    view = _task_or_404(manager, payload.month)
    if outcome == "started":
        response.status_code = 202
        message = "采集任务已启动；同任务并发触发只会运行一次"
    elif outcome == "running":
        message = "相同月份任务正在运行中，本次触发未重复执行"
    else:
        message = "该月份已完成采集，无需重复运行"
    return StartTaskResponse(status=outcome, message=message, task=view)


@router.get(
    "/tasks",
    response_model=TaskListResponse,
    summary="全部采集任务进度",
)
def list_collection_tasks(request: Request) -> TaskListResponse:
    return TaskListResponse(tasks=_manager(request).list_task_views())


@router.get(
    "/tasks/{month}",
    response_model=TaskView,
    summary="单月采集任务进度",
    responses={404: {"description": "该月份还没有采集任务"}},
)
def get_collection_task(
    request: Request,
    month: str = Path(..., pattern=MONTH_PATTERN, description="月份 YYYY-MM"),
) -> dict:
    return _task_or_404(_manager(request), month)


@router.get(
    "/tasks/{month}/records",
    response_model=RecordListResponse,
    summary="已采完工记录查询",
    responses={404: {"description": "该月份还没有采集任务"}},
)
def list_collection_records(
    request: Request,
    month: str = Path(..., pattern=MONTH_PATTERN, description="月份 YYYY-MM"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> RecordListResponse:
    manager = _manager(request)
    _task_or_404(manager, month)
    records = manager.storage.list_records(month, limit, offset)
    total = manager.storage.count_records(month)
    return RecordListResponse(
        month=month, total=total, limit=limit, offset=offset, records=records
    )
