import unittest

import httpx

from app.collector.parsing import parse_csrf_token, parse_detail_page, parse_list_page
from tests.portal_test_utils import PortalServer


class PortalTests(unittest.TestCase):
    """演示门户行为：CSRF 登录、Cookie 会话、翻页前过期、详情断流。"""

    @classmethod
    def setUpClass(cls):
        cls.portal = PortalServer().__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.portal.__exit__(None, None, None)

    def setUp(self):
        self.client = httpx.Client(
            base_url=self.portal.base_url, follow_redirects=False
        )
        self.addCleanup(self.client.close)
        self.client.post("/_demo/reset")

    def login(self, username="demo", password="demo123", token=None):
        page = self.client.get("/login")
        assert page.status_code == 200
        token = token if token is not None else parse_csrf_token(page.text)
        return self.client.post(
            "/login",
            data={
                "username": username,
                "password": password,
                "csrf_token": token,
            },
        )

    def test_unauthenticated_list_redirects_to_login(self):
        response = self.client.get("/workorders?month=2025-10")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["location"])

    def test_login_requires_csrf_token_and_credentials(self):
        response = self.client.post(
            "/login",
            data={
                "username": "demo",
                "password": "demo123",
                "csrf_token": "forged",
            },
        )
        self.assertEqual(response.status_code, 403)

        response = self.login(password="wrong")
        self.assertEqual(response.status_code, 401)

        response = self.login()
        self.assertEqual(response.status_code, 303)
        self.assertIn("portal_session", self.client.cookies)

    def test_list_and_detail_pages(self):
        self.login()
        response = self.client.get("/workorders?month=2025-10")
        self.assertEqual(response.status_code, 200)
        hrefs, next_href = parse_list_page(response.text)
        self.assertEqual(len(hrefs), 3)
        self.assertEqual(next_href, "/workorders?month=2025-10&page=2")

        detail = self.client.get(hrefs[0])
        self.assertEqual(detail.status_code, 200)
        fields = parse_detail_page(detail.text)
        self.assertEqual(fields["work_order_no"], "WO-202510-001")
        self.assertIsInstance(fields["completed_at"], int)

    def test_session_expires_before_second_page(self):
        # 会话列表页预算为 1：第一页正常，翻页前会话被强制失效
        self.login()
        first = self.client.get("/workorders?month=2025-10")
        self.assertEqual(first.status_code, 200)
        second = self.client.get("/workorders?month=2025-10&page=2")
        self.assertEqual(second.status_code, 302)
        self.assertIn("/login", second.headers["location"])
        # 旧会话已失效：第一页同样无法再看
        again = self.client.get("/workorders?month=2025-10")
        self.assertEqual(again.status_code, 302)

    def test_detail_fails_once_with_broken_stream_then_recovers(self):
        self.login()
        with self.assertRaises(httpx.HTTPError):
            self.client.get("/workorders/WO-202510-005")
        recovered = self.client.get("/workorders/WO-202510-005")
        self.assertEqual(recovered.status_code, 200)
        fields = parse_detail_page(recovered.text)
        self.assertEqual(fields["work_order_no"], "WO-202510-005")

    def test_unknown_month_returns_404(self):
        self.login()
        response = self.client.get("/workorders?month=2030-01")
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
