"""维修门户 HTTP 客户端：CSRF 登录、Cookie 会话、失效后最多重登一次。"""

from __future__ import annotations

from urllib.parse import urlparse

import httpx

from .parsing import parse_csrf_token

SESSION_COOKIE_NAME = "portal_session"
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class FetchError(Exception):
    """一次 HTTP 取数最终失败（含重登一次后仍失败）。"""


def _is_login_redirect(response: httpx.Response) -> bool:
    if response.status_code not in _REDIRECT_STATUSES:
        return False
    location = response.headers.get("location", "")
    return "/login" in urlparse(location).path


class PortalClient:
    """带 Cookie 会话的门户客户端。

    每个 HTTP 请求在会话失效跳回登录页时，重新取 CSRF 令牌登录并重取
    原请求一次；再失败则抛出 :class:`FetchError`，由调用方停在当前页。
    """

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        timeout: float = 10.0,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            follow_redirects=False,
            headers={"User-Agent": "bus-maintenance-collector/1.0"},
        )
        self._username = username
        self._password = password
        self.login_count = 0  # 登录次数，供测试与运维核对重登行为

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PortalClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def login(self) -> None:
        """获取登录页 CSRF 令牌，提交登录表单，刷新会话 Cookie。"""

        try:
            page = self._client.get("/login")
        except httpx.HTTPError as exc:
            raise FetchError(f"获取登录页失败: {exc}") from exc
        if page.status_code != 200:
            raise FetchError(f"获取登录页失败: HTTP {page.status_code}")

        token = parse_csrf_token(page.text)

        try:
            response = self._client.post(
                "/login",
                data={
                    "username": self._username,
                    "password": self._password,
                    "csrf_token": token,
                },
            )
        except httpx.HTTPError as exc:
            raise FetchError(f"提交登录表单失败: {exc}") from exc
        if response.status_code not in (302, 303):
            raise FetchError(f"登录被拒绝: HTTP {response.status_code}")
        if not self._client.cookies.get(SESSION_COOKIE_NAME):
            raise FetchError("登录后未获得会话 Cookie")
        self.login_count += 1

    def fetch(self, url: str) -> str:
        """GET 一个页面；会话失效跳回登录时重登一次并重取原请求。

        - 跳回登录 → 重登并重取；重取仍跳回登录 → FetchError（停在当前页）；
        - 断流、非 200 响应（如 500）→ 立即 FetchError，不重登、不跳页。
        """

        for attempt in (0, 1):
            try:
                response = self._client.get(url)
            except httpx.HTTPError as exc:
                raise FetchError(f"请求失败 {url}: {exc}") from exc

            if _is_login_redirect(response):
                if attempt == 1:
                    raise FetchError(f"重新登录后仍跳回登录页，停在当前页: {url}")
                self.login()
                continue

            if response.status_code in _REDIRECT_STATUSES:
                location = response.headers.get("location", "")
                raise FetchError(
                    f"未预期的重定向 HTTP {response.status_code} {url} -> {location}"
                )
            if response.status_code != 200:
                raise FetchError(f"HTTP {response.status_code}: {url}")
            return response.text

        raise FetchError(f"请求失败 {url}")  # pragma: no cover - 不可达
