"""核心逻辑测试：python -m unittest discover -s tests -v

不联网、不弹窗，用临时数据库跑通「过滤 → 评分 → 去重 → 接口」全链路。
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.engine import Engine  # noqa: E402
from app.notifier import Notifier  # noqa: E402
from app.scoring import evaluate, freshness_score, grade_of  # noqa: E402
from app.server import Context, create_server  # noqa: E402
from app.sources.base import RawItem  # noqa: E402
from app.sources.http_json import dig, to_iso  # noqa: E402
from app.sources.manual import parse_any, parse_pasted  # noqa: E402
from app.store import TZ, Store, now_iso  # noqa: E402


class BaseCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "test.db")
        for task in self.store.list_tasks():
            self.store.delete_task(task["id"])
        self.store.save_settings({"notify": {"desktop": False, "sound": False, "min_score": 0}})
        self.notifier = Notifier(self.store)
        self.engine = Engine(self.store, self.notifier)

    def tearDown(self) -> None:
        self.engine.stop()
        self.tmp.cleanup()

    def make_task(self, **kwargs) -> dict:
        base = {
            "name": "测试任务",
            "keywords": ["iphone"],
            "max_price": 3000,
            "min_price": 0,
            "min_discount": 0.2,
            "interval_sec": 30,
            "source": "mock",
        }
        base.update(kwargs)
        return self.store.create_task(base)


class TestParsing(unittest.TestCase):
    def test_parse_pasted_space_separated(self) -> None:
        rows = parse_pasted("iPhone 13 128G 95新 自用 1899 https://www.goofish.com/item?id=1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["price"], 1899.0)
        self.assertTrue(rows[0]["url"].startswith("https://"))

    def test_parse_pasted_price_symbols(self) -> None:
        rows = parse_pasted("RTX3060 显卡 ¥1,299")
        self.assertEqual(rows[0]["price"], 1299.0)
        self.assertEqual(rows[0]["title"], "RTX3060 显卡")

    def test_parse_skips_pure_link(self) -> None:
        self.assertEqual(parse_pasted("https://example.com/x"), [])

    def test_dig_and_time(self) -> None:
        self.assertEqual(dig({"data": {"items": [{"id": 7}]}}, "data.items.0.id"), 7)
        self.assertEqual(dig({}, "a.b.c", "fallback"), "fallback")
        self.assertTrue(to_iso(1735689600000).startswith("2025-01-01"))

    def test_share_text_with_price(self) -> None:
        text = ('【闲鱼】https://m.tb.cn/h.6abc “iPhone 13 128G 国行 95新” '
                "¥1899 点击链接直接打开")
        rows = parse_any(text)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["price"], 1899.0)
        self.assertTrue(rows[0]["url"].startswith("https://m.tb.cn/"))
        self.assertIn("iPhone 13", rows[0]["title"])

    def test_csv_with_chinese_header(self) -> None:
        text = ("标题,价格,链接,行情价\n"
                "iPhone 13 128G 95新,1899,https://www.goofish.com/item?id=1,4200\n"
                "RTX 3060 显卡,1450,,2600")
        rows = parse_any(text)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["price"], 1899.0)
        self.assertEqual(rows[0]["extra"]["market_price"], 4200.0)
        self.assertEqual(rows[1]["title"], "RTX 3060 显卡")

    def test_json_text_with_alias_keys(self) -> None:
        text = ('[{"标题": "RTX 3060 显卡", "价格": "1,450", '
                '"链接": "https://www.goofish.com/item?id=2", "想要人数": 12}]')
        rows = parse_any(text)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["price"], 1450.0)
        self.assertEqual(rows[0]["want"], 12)
        self.assertTrue(rows[0]["url"].endswith("id=2"))


class TestScoring(unittest.TestCase):
    def test_grade_thresholds(self) -> None:
        self.assertEqual(grade_of(90), "S")
        self.assertEqual(grade_of(70), "A")
        self.assertEqual(grade_of(55), "B")
        self.assertEqual(grade_of(10), "C")

    def test_freshness_decays(self) -> None:
        now = datetime.now(TZ)
        fresh, _ = freshness_score((now - timedelta(minutes=20)).isoformat(), now)
        old, _ = freshness_score((now - timedelta(days=6)).isoformat(), now)
        self.assertGreater(fresh, old)

    def test_deep_discount_scores_high(self) -> None:
        item = {"published_at": now_iso(), "want": 200, "region": "惠州",
                "extra": {"free_shipping": True, "seller_score": 98}}
        task = {"region": "惠州"}
        score, grade, reasons = evaluate(item, task, 3000, 0.55)
        self.assertGreaterEqual(score, 80)
        self.assertEqual(grade, "S")
        self.assertTrue(any("参考价" in r for r in reasons))


class TestStoreAndEngine(BaseCase):
    def test_task_crud(self) -> None:
        task = self.make_task(name="iPhone 捡漏")
        self.assertEqual(task["name"], "iPhone 捡漏")
        updated = self.store.update_task(task["id"], {"max_price": 2500, "keywords": ["13", "14"]})
        self.assertEqual(updated["max_price"], 2500)
        self.assertEqual(updated["keywords"], ["13", "14"])
        self.store.delete_task(task["id"])
        self.assertIsNone(self.store.get_task(task["id"]))

    def test_filters_and_hit_dedupe(self) -> None:
        task = self.make_task()
        good = RawItem(
            item_id="x-1", title="iPhone 13 128G 国行 95新", price=1500,
            region="惠州", want=42, published_at=now_iso(),
            extra={"market_price": 3000, "free_shipping": True, "seller_score": 96},
        )
        self.assertEqual(self.engine.ingest([good], [task["id"]]), 1)
        # 同一商品再次出现不应重复记一条命中
        self.assertEqual(self.engine.ingest([good], [task["id"]]), 0)

        too_expensive = RawItem(item_id="x-2", title="iPhone 13 全新", price=9999,
                                published_at=now_iso(), extra={"market_price": 3000})
        self.assertEqual(self.engine.ingest([too_expensive], [task["id"]]), 0)

        wrong_word = RawItem(item_id="x-3", title="小米 13 手机壳", price=30,
                             published_at=now_iso(), extra={"market_price": 3000})
        self.assertEqual(self.engine.ingest([wrong_word], [task["id"]]), 0)

        excluded = RawItem(item_id="x-4", title="iPhone 13 主板 维修", price=200,
                           published_at=now_iso(), extra={"market_price": 3000})
        self.store.update_task(task["id"], {"exclude": ["主板"]})
        self.assertEqual(self.engine.ingest([excluded], [task["id"]]), 0)

    def test_discount_threshold(self) -> None:
        task = self.make_task(min_discount=0.5)
        weak = RawItem(item_id="x-5", title="iPhone 14 128G", price=2900,
                       published_at=now_iso(), extra={"market_price": 3000})
        self.assertEqual(self.engine.ingest([weak], [task["id"]]), 0)

    def test_reference_price_fallback_to_median(self) -> None:
        task = self.make_task(min_discount=0.3)
        items = [
            RawItem(item_id=f"m-{i}", title="iphone 13 128G", price=p, published_at=now_iso())
            for i, p in enumerate([2600, 2700, 2800, 2650, 2750, 1200])
        ]
        hits = self.engine.ingest(items, [task["id"]])
        self.assertEqual(hits, 1)  # 只有 1200 这条相对中位数的折扣够

    def test_manual_import_marks_hits(self) -> None:
        task = self.make_task()
        result = self.engine.import_items(
            [{"title": "iPhone 13 128G 自用 95新", "price": 1600,
              "extra": {"market_price": 3200}}],
            [task["id"]],
        )
        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["hits"], 1)
        self.assertEqual(len(self.store.list_hits()), 1)


class HttpCase(BaseCase):
    def setUp(self) -> None:
        super().setUp()
        self.ctx = Context(self.store, self.engine, self.notifier)
        self.server = create_server("127.0.0.1", 0, self.ctx)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def get_json(self, path: str) -> dict:
        with urllib.request.urlopen(self.url(path), timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def post_json(self, path: str, payload: dict) -> dict:
        request = urllib.request.Request(
            self.url(path), data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))


class TestHttpApi(HttpCase):
    def test_state_contains_task_and_ui_fields(self) -> None:
        self.make_task()
        data = self.get_json("/api/state")
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["tasks"]), 1)
        self.assertIn("mock", [s["name"] for s in data["sources"]])

    def test_import_then_hits(self) -> None:
        task = self.make_task()
        res = self.post_json("/api/import", {
            "items": [{"title": "iPhone 13 128G 95新 自用", "price": 1500,
                       "extra": {"market_price": 3000}}],
            "task_ids": [task["id"]],
        })
        self.assertTrue(res["ok"])
        self.assertEqual(res["hits"], 1)
        hits = self.get_json("/api/hits")["hits"]
        self.assertEqual(len(hits), 1)
        self.assertIn(hits[0]["grade"], "SABC")
        self.assertGreater(hits[0]["discount"], 0)

    def test_import_without_reference_records_no_hit(self) -> None:
        task = self.make_task()
        res = self.post_json("/api/import", {"text": "iPhone 13 128G 95新 1500",
                                             "task_ids": [task["id"]]})
        self.assertTrue(res["ok"])
        self.assertEqual(res["imported"], 1)
        self.assertEqual(res["hits"], 0)
        self.assertEqual(len(self.get_json("/api/hits")["hits"]), 0)

    def test_create_and_toggle_task(self) -> None:
        res = self.post_json("/api/tasks", {"name": "新任务", "keywords": "iphone, 显卡", "max_price": 2000})
        self.assertTrue(res["ok"])
        self.assertEqual(res["task"]["keywords"], ["iphone", "显卡"])
        toggle = self.post_json(f"/api/tasks/{res['task']['id']}/toggle", {})
        self.assertFalse(toggle["task"]["enabled"])

    def test_index_served(self) -> None:
        with urllib.request.urlopen(self.url("/"), timeout=5) as resp:
            body = resp.read().decode("utf-8")
            self.assertEqual(resp.status, 200)
        self.assertIn("闲鱼低价捕捉机", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
