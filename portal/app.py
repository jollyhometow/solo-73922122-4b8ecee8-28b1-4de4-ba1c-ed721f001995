"""本地演示维修门户。

模拟一个采用 Cookie 会话 + CSRF 登录表单的维修门户，供采集 API 演示：

- ``GET  /login``                      登录表单（隐藏域携带 CSRF 令牌，同时下发 CSRF Cookie）
- ``POST /login``                      校验令牌与口令，签发会话 Cookie，303 回首页
- ``GET  /``                           已封存月份入口（需登录）
- ``GET  /workorders?month=&page=``    完工记录 HTML 列表（``rel="next"`` 翻页）
- ``GET  /workorders/{工单号}``        完工记录 HTML 详情
- ``POST /_demo/reset``                重置故障注入与会话（供反复演示）
- ``GET  /healthz``                    健康检查

演示故障由环境变量控制：

- ``PORTAL_SESSION_PAGE_BUDGET``  每个会话可服务的列表页数，超出即强制失效
  （默认 1，用于演示“翻页前会话过期”）；
- ``PORTAL_FAIL_ONCE_ORDERS``     逗号分隔的工单号，首次请求其详情时断流
  （默认 ``WO-202510-005``，即 2025-10 第二页的第二份详情）；
- ``PORTAL_LATENCY_MS``           每个数据请求的模拟延迟（毫秒）；
- ``PORTAL_SESSION_TTL_SECONDS``  会话最长存活时间；
- ``PORTAL_USERNAME`` / ``PORTAL_PASSWORD``  演示登录口令。
"""

from __future__ import annotations

import asyncio
import html
import os
import secrets
import time
import urllib.parse
from dataclasses import dataclass, field

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from .data import ARCHIVED_MONTHS, PAGE_SIZE, WorkOrder, build_dataset

CSRF_COOKIE = "portal_csrf"
SESSION_COOKIE = "portal_session"


@dataclass(frozen=True)
class PortalSettings:
    username: str = "demo"
    password: str = "demo123"
    session_ttl_seconds: float = 3600.0
    session_page_budget: int = 1
    fail_once_orders: tuple[str, ...] = ("WO-202510-005",)
    latency_ms: int = 200

    @classmethod
    def from_env(cls) -> "PortalSettings":
        return cls(
            username=os.environ.get("PORTAL_USERNAME", "demo"),
            password=os.environ.get("PORTAL_PASSWORD", "demo123"),
            session_ttl_seconds=float(
                os.environ.get("PORTAL_SESSION_TTL_SECONDS", "3600")
            ),
            session_page_budget=int(os.environ.get("PORTAL_SESSION_PAGE_BUDGET", "1")),
            fail_once_orders=tuple(
                item.strip()
                for item in os.environ.get(
                    "PORTAL_FAIL_ONCE_ORDERS", "WO-202510-005"
                ).split(",")
                if item.strip()
            ),
            latency_ms=int(os.environ.get("PORTAL_LATENCY_MS", "200")),
        )


@dataclass
class PortalState:
    settings: PortalSettings
    dataset: dict[str, list[WorkOrder]]
    sessions: dict[str, dict] = field(default_factory=dict)
    csrf_tokens: dict[str, float] = field(default_factory=dict)
    fail_once_remaining: set[str] = field(init=False)

    def __post_init__(self) -> None:
        self.fail_once_remaining = set(self.settings.fail_once_orders)

    def reset_demo(self) -> None:
        self.sessions.clear()
        self.csrf_tokens.clear()
        self.fail_once_remaining = set(self.settings.fail_once_orders)


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html><head>"
        f'<meta charset="utf-8"><title>{_esc(title)}</title>'
        "</head><body>"
        f"{body}"
        "</body></html>"
    )


def _login_redirect() -> RedirectResponse:
    return RedirectResponse(url="/login", status_code=302)


def _detail_html(order: WorkOrder) -> str:
    return _page(
        f"工单 {order.work_order_no}",
        f"""
<h1>完工工单详情</h1>
<dl>
  <dt>工单号</dt><dd data-field="work_order_no">{_esc(order.work_order_no)}</dd>
  <dt>车辆号</dt><dd data-field="vehicle_no">{_esc(order.vehicle_no)}</dd>
  <dt>故障码</dt><dd data-field="fault_code">{_esc(order.fault_code)}</dd>
  <dt>完工时间</dt><dd data-field="completed_at">{order.completed_at}</dd>
</dl>
<p><a href="/workorders?month={_esc(order.month)}&amp;page=1">返回列表</a></p>
""",
    )


def _broken_stream_response(order_id: str) -> StreamingResponse:
    """模拟断流：先吐出部分 HTML，再中断连接。"""

    async def body():
        yield (
            "<!doctype html><html><head>"
            f"<title>工单 {_esc(order_id)}</title></head><body>"
            "<h1>完工工单详情</h1><dl>"
        )
        raise RuntimeError(f"模拟断流：详情 {order_id} 响应中断")

    return StreamingResponse(body(), media_type="text/html; charset=utf-8")


def create_app(settings: PortalSettings | None = None) -> FastAPI:
    settings = settings or PortalSettings.from_env()
    state = PortalState(settings=settings, dataset=build_dataset())
    orders_by_no = {
        order.work_order_no: order
        for orders in state.dataset.values()
        for order in orders
    }

    app = FastAPI(
        title="本地演示维修门户",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.portal = state

    async def _latency() -> None:
        if settings.latency_ms > 0:
            await asyncio.sleep(settings.latency_ms / 1000)

    def _valid_session(request: Request) -> dict | None:
        sid = request.cookies.get(SESSION_COOKIE)
        if not sid:
            return None
        session = state.sessions.get(sid)
        if session is None:
            return None
        if time.monotonic() - session["created"] > settings.session_ttl_seconds:
            state.sessions.pop(sid, None)
            return None
        return session

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/login", response_class=HTMLResponse)
    async def login_form() -> HTMLResponse:
        token = secrets.token_urlsafe(24)
        state.csrf_tokens[token] = time.monotonic()
        # 顺手清理一小时前的旧令牌
        cutoff = time.monotonic() - 3600
        for old_token, issued_at in list(state.csrf_tokens.items()):
            if issued_at < cutoff:
                state.csrf_tokens.pop(old_token, None)
        body = f"""
<h1>维修门户登录</h1>
<form method="post" action="/login">
  <input type="hidden" name="csrf_token" value="{_esc(token)}">
  <p><label>用户名 <input name="username" autocomplete="username"></label></p>
  <p><label>密码 <input name="password" type="password" autocomplete="current-password"></label></p>
  <p><button type="submit">登录</button></p>
</form>
"""
        response = HTMLResponse(_page("登录", body))
        response.set_cookie(CSRF_COOKIE, token, httponly=True, path="/")
        return response

    @app.post("/login")
    async def login_submit(request: Request):
        raw = (await request.body()).decode("utf-8", "replace")
        form = {key: values[0] for key, values in urllib.parse.parse_qs(raw).items()}

        token = form.get("csrf_token", "")
        cookie_token = request.cookies.get(CSRF_COOKIE, "")
        if not token or token != cookie_token or token not in state.csrf_tokens:
            return HTMLResponse(
                _page("登录失败", "<p>CSRF 令牌无效，请重新打开登录页。</p>"),
                status_code=403,
            )
        state.csrf_tokens.pop(token, None)  # 令牌一次性使用

        if (
            form.get("username") != settings.username
            or form.get("password") != settings.password
        ):
            return HTMLResponse(
                _page("登录失败", "<p>用户名或密码错误。</p>"),
                status_code=401,
            )

        sid = secrets.token_urlsafe(24)
        state.sessions[sid] = {
            "username": settings.username,
            "created": time.monotonic(),
            "pages_served": 0,
        }
        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie(SESSION_COOKIE, sid, httponly=True, path="/")
        return response

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        if _valid_session(request) is None:
            return _login_redirect()
        links = "".join(
            f'<li><a href="/workorders?month={_esc(month)}">{_esc(month)} 完工记录</a></li>'
            for month in ARCHIVED_MONTHS
        )
        body = f"""
<h1>维修门户（演示）</h1>
<p>已封存月份：</p>
<ul>{links}</ul>
<p>会话列表页预算：{settings.session_page_budget}；
首次断流详情：{_esc(", ".join(sorted(state.fail_once_remaining)) or "（无）")}</p>
"""
        return HTMLResponse(_page("维修门户", body))

    @app.get("/workorders", response_class=HTMLResponse)
    async def work_order_list(request: Request, month: str = "", page: int = 1):
        session = _valid_session(request)
        if session is None:
            return _login_redirect()
        if session["pages_served"] >= settings.session_page_budget:
            # 演示：会话在翻页前被强制失效
            sid = request.cookies.get(SESSION_COOKIE)
            if sid:
                state.sessions.pop(sid, None)
            return _login_redirect()
        session["pages_served"] += 1
        await _latency()

        orders = state.dataset.get(month)
        if orders is None:
            return HTMLResponse(
                _page("月份未封存", f"<p>月份 {_esc(month)} 尚未封存或不存在。</p>"),
                status_code=404,
            )
        total_pages = max(1, (len(orders) + PAGE_SIZE - 1) // PAGE_SIZE)
        if page < 1 or page > total_pages:
            return HTMLResponse(
                _page("页码越界", "<p>页码超出范围。</p>"),
                status_code=404,
            )

        chunk = orders[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
        rows = "".join(
            "<tr>"
            f"<td>{_esc(order.work_order_no)}</td>"
            f"<td>{_esc(order.vehicle_no)}</td>"
            f"<td>{_esc(order.fault_code)}</td>"
            f"<td>{order.completed_at}</td>"
            f'<td><a class="detail-link" href="/workorders/{_esc(order.work_order_no)}">详情</a></td>'
            "</tr>"
            for order in chunk
        )
        if page < total_pages:
            next_link = (
                f'<a rel="next" href="/workorders?month={_esc(month)}'
                f'&amp;page={page + 1}">下一页</a>'
            )
        else:
            next_link = "<span>没有下一页</span>"
        body = f"""
<h1>完工记录 - {_esc(month)}（第 {page}/{total_pages} 页）</h1>
<table class="work-order-list">
<thead><tr><th>工单号</th><th>车辆号</th><th>故障码</th><th>完工时间(UTC秒)</th><th>操作</th></tr></thead>
<tbody>{rows}</tbody>
</table>
<nav>{next_link}</nav>
"""
        return HTMLResponse(_page(f"完工记录 {month}", body))

    @app.get("/workorders/{order_id}", response_class=HTMLResponse)
    async def work_order_detail(request: Request, order_id: str):
        if _valid_session(request) is None:
            return _login_redirect()
        await _latency()
        order = orders_by_no.get(order_id)
        if order is None:
            return HTMLResponse(
                _page("工单不存在", "<p>工单不存在。</p>"),
                status_code=404,
            )
        if order_id in state.fail_once_remaining:
            state.fail_once_remaining.discard(order_id)
            return _broken_stream_response(order_id)
        return HTMLResponse(_detail_html(order))

    @app.post("/_demo/reset")
    async def demo_reset() -> JSONResponse:
        state.reset_demo()
        return JSONResponse(
            {
                "status": "reset",
                "fail_once_orders": sorted(state.fail_once_remaining),
            }
        )

    return app


app = create_app()
