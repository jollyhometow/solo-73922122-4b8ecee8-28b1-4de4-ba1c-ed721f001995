import unittest

from app.collector.parsing import (
    DetailValidationError,
    ParseError,
    parse_csrf_token,
    parse_detail_page,
    parse_list_page,
)

LOGIN_HTML = """
<!doctype html><html><body>
<form method="post" action="/login">
  <input type="hidden" name="csrf_token" value="token-abc-123">
  <input name="username"><input name="password" type="password">
</form>
</body></html>
"""

LIST_HTML_PAGE1 = """
<!doctype html><html><body>
<table class="work-order-list">
<tr><td>WO-202510-001</td><td><a class="detail-link" href="/workorders/WO-202510-001">详情</a></td></tr>
<tr><td>WO-202510-002</td><td><a class="detail-link" href="/workorders/WO-202510-002">详情</a></td></tr>
<tr><td>WO-202510-003</td><td><a class="detail-link" href="/workorders/WO-202510-003">详情</a></td></tr>
</table>
<nav><a rel="next" href="/workorders?month=2025-10&amp;page=2">下一页</a></nav>
</body></html>
"""

LIST_HTML_LAST_PAGE = """
<!doctype html><html><body>
<table class="work-order-list">
<tr><td>WO-202510-007</td><td><a class="detail-link" href="/workorders/WO-202510-007">详情</a></td></tr>
</table>
<nav><span>没有下一页</span></nav>
</body></html>
"""

DETAIL_HTML = """
<!doctype html><html><body>
<dl>
  <dt>工单号</dt><dd data-field="work_order_no">WO-202510-001</dd>
  <dt>车辆号</dt><dd data-field="vehicle_no">沪A10203</dd>
  <dt>故障码</dt><dd data-field="fault_code">P0301</dd>
  <dt>完工时间</dt><dd data-field="completed_at">1730419200</dd>
</dl>
</body></html>
"""


class ParseCsrfTokenTests(unittest.TestCase):
    def test_extracts_token(self):
        self.assertEqual(parse_csrf_token(LOGIN_HTML), "token-abc-123")

    def test_missing_token_raises(self):
        with self.assertRaises(ParseError):
            parse_csrf_token("<html><body><form></form></body></html>")


class ParseListPageTests(unittest.TestCase):
    def test_detail_links_and_next_page(self):
        hrefs, next_href = parse_list_page(LIST_HTML_PAGE1)
        self.assertEqual(
            hrefs,
            [
                "/workorders/WO-202510-001",
                "/workorders/WO-202510-002",
                "/workorders/WO-202510-003",
            ],
        )
        # HTML 中的 &amp; 应被还原为 &
        self.assertEqual(next_href, "/workorders?month=2025-10&page=2")

    def test_last_page_has_no_next_link(self):
        hrefs, next_href = parse_list_page(LIST_HTML_LAST_PAGE)
        self.assertEqual(hrefs, ["/workorders/WO-202510-007"])
        self.assertIsNone(next_href)


class ParseDetailPageTests(unittest.TestCase):
    def test_valid_detail(self):
        fields = parse_detail_page(DETAIL_HTML)
        self.assertEqual(
            fields,
            {
                "work_order_no": "WO-202510-001",
                "vehicle_no": "沪A10203",
                "fault_code": "P0301",
                "completed_at": 1730419200,
            },
        )

    def test_missing_field_raises(self):
        html = DETAIL_HTML.replace('data-field="fault_code"', 'data-field="other"')
        with self.assertRaises(DetailValidationError):
            parse_detail_page(html)

    def test_non_integer_completed_at_raises(self):
        html = DETAIL_HTML.replace("1730419200", "2025-11-01 10:00")
        with self.assertRaises(DetailValidationError):
            parse_detail_page(html)

    def test_empty_work_order_no_raises(self):
        html = DETAIL_HTML.replace("WO-202510-001", "")
        with self.assertRaises(DetailValidationError):
            parse_detail_page(html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
