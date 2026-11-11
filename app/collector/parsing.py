"""维修门户 HTML 解析：登录 CSRF 令牌、列表页链接、详情页字段。"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

LIST_DETAIL_LINK_CLASS = "detail-link"
DETAIL_FIELDS = ("work_order_no", "vehicle_no", "fault_code", "completed_at")

_TOKEN_RE = re.compile(r"\S+")
_COMPLETED_AT_RE = re.compile(r"\d{1,15}")


class ParseError(Exception):
    """HTML 结构与预期不符。"""


class DetailValidationError(ParseError):
    """详情页字段缺失或非法。"""


def parse_csrf_token(html_text: str) -> str:
    """从登录表单中提取 CSRF 令牌。"""

    soup = BeautifulSoup(html_text, "html.parser")
    tag = soup.find("input", attrs={"name": "csrf_token"})
    token = tag.get("value") if tag else None
    if not token:
        raise ParseError("登录页缺少 CSRF 令牌隐藏域")
    return token


def parse_list_page(html_text: str) -> tuple[list[str], str | None]:
    """解析列表页，返回（详情链接列表，下一页链接或 None）。"""

    soup = BeautifulSoup(html_text, "html.parser")

    detail_hrefs: list[str] = []
    for tag in soup.find_all("a", class_=LIST_DETAIL_LINK_CLASS):
        href = tag.get("href")
        if href and href.strip():
            detail_hrefs.append(href.strip())

    next_href: str | None = None
    for tag in soup.find_all("a"):
        rel = tag.get("rel") or []
        if "next" in rel:
            href = tag.get("href")
            next_href = href.strip() if href and href.strip() else None
            break

    return detail_hrefs, next_href


def parse_detail_page(html_text: str) -> dict:
    """解析并校验详情页，返回工单号、车辆号、故障码、完工时间（UTC 秒）。

    任何字段缺失或非法都会抛出 :class:`DetailValidationError`，
    调用方据此判定“坏详情”，不推进当前页。
    """

    soup = BeautifulSoup(html_text, "html.parser")
    values: dict[str, str] = {}
    for field_name in DETAIL_FIELDS:
        tag = soup.find(attrs={"data-field": field_name})
        if tag is None:
            raise DetailValidationError(f"详情页缺少字段 {field_name}")
        values[field_name] = tag.get_text(strip=True)

    work_order_no = values["work_order_no"]
    if (
        not work_order_no
        or len(work_order_no) > 64
        or not _TOKEN_RE.fullmatch(work_order_no)
    ):
        raise DetailValidationError(f"工单号非法: {work_order_no!r}")

    vehicle_no = values["vehicle_no"]
    if not vehicle_no or len(vehicle_no) > 64:
        raise DetailValidationError(f"车辆号非法: {vehicle_no!r}")

    fault_code = values["fault_code"]
    if not fault_code or len(fault_code) > 64:
        raise DetailValidationError(f"故障码非法: {fault_code!r}")

    completed_raw = values["completed_at"]
    if not _COMPLETED_AT_RE.fullmatch(completed_raw):
        raise DetailValidationError(f"完工时间不是 UTC 整数秒: {completed_raw!r}")

    return {
        "work_order_no": work_order_no,
        "vehicle_no": vehicle_no,
        "fault_code": fault_code,
        "completed_at": int(completed_raw),
    }
