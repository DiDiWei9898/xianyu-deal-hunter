"""本地 HTTP 服务：静态控制台 + REST API + SSE 实时推送。"""

from __future__ import annotations

import json
import mimetypes
import queue
import re
import sys
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__
from .config import APP_NAME, APP_VERSION, DB_FILE, DEFAULT_TASK, WEB_DIR
from .scoring import GRADE_LABELS
from .sources import available, get_source
from .sources.manual import parse_any

TASK_ITEM_RE = re.compile(r"^/api/tasks/(\d+)$")
TASK_ACTION_RE = re.compile(r"^/api/tasks/(\d+)/(run|toggle)$")
HIT_STATUS_RE = re.compile(r"^/api/hits/(\d+)/status$")


class Context:
    def __init__(self, store, engine, notifier) -> None:
        self.store = store
        self.engine = engine
        self.notifier = notifier


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"XianyuDealHunter/{__version__}"

    # ---------------------------------------------------------------- 基础
    @property
    def ctx(self) -> Context:
        return self.server.ctx  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        if self.path.startswith("/api/events"):
            return
        sys.stderr.write(f"[http] {self.address_string()} {fmt % args}\n")

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, message: str, status: int = 400) -> None:
        self._json({"ok": False, "error": message}, status)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    # ---------------------------------------------------------------- 视图
    @staticmethod
    def _hit_view(row: dict) -> dict:
        row = dict(row)
        row["grade_label"] = GRADE_LABELS.get(row.get("grade", "C"), "")
        row["demo"] = str(row.get("item_id", "")).startswith("mock")
        return row

    def _state(self) -> dict:
        store = self.ctx.store
        return {
            "ok": True,
            "app": {"name": APP_NAME, "version": APP_VERSION, "db": str(DB_FILE)},
            "engine": {"running": self.ctx.engine.running},
            "tasks": store.list_tasks(),
            "stats": store.stats(),
            "grades": store.grade_distribution(),
            "settings": store.get_settings(),
            "sources": available(),
            "alerts": store.list_alerts(limit=12),
            "hits": [self._hit_view(h) for h in store.list_hits(limit=40)],
        }

    # ---------------------------------------------------------------- 路由
    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/api/events":
                return self._sse()
            if path == "/api/state":
                return self._json(self._state())
            if path == "/api/hits":
                task_id = _int(query.get("task_id", [None])[0])
                grade = query.get("grade", [None])[0]
                limit = _int(query.get("limit", [60])[0]) or 60
                rows = self.ctx.store.list_hits(task_id=task_id, grade=grade, limit=limit)
                return self._json({"ok": True, "hits": [self._hit_view(r) for r in rows]})
            if path == "/api/alerts":
                return self._json({"ok": True, "alerts": self.ctx.store.list_alerts(limit=40)})
            if path == "/api/export.csv":
                text = self.ctx.store.export_csv(self.ctx.store.list_hits(limit=300))
                return self._send(
                    200, text.encode("utf-8"), "text/csv; charset=utf-8",
                    {"Content-Disposition": 'attachment; filename="xianyu_hits.csv"'},
                )
            if path.startswith("/api/"):
                return self._error("接口不存在", 404)
            return self._static(path)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            return self._error("服务端处理失败，请查看控制台日志", 500)

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        try:
            data = self._body()
            if path == "/api/tasks":
                return self._create_task(data)
            match = TASK_ACTION_RE.match(path)
            if match:
                task_id, action = int(match.group(1)), match.group(2)
                if action == "run":
                    started = self.ctx.engine.submit(task_id)
                    return self._json({"ok": True, "started": started})
                task = self.ctx.store.get_task(task_id)
                if not task:
                    return self._error("任务不存在", 404)
                updated = self.ctx.store.update_task(task_id, {"enabled": not task["enabled"]})
                return self._json({"ok": True, "task": updated})
            match = HIT_STATUS_RE.match(path)
            if match:
                hit_id = int(match.group(1))
                status = str(data.get("status") or "ignored")
                self.ctx.store.mark_hit(hit_id, status)
                return self._json({"ok": True})
            if path == "/api/import":
                return self._import(data)
            if path == "/api/settings":
                merged = self.ctx.store.save_settings(data)
                return self._json({"ok": True, "settings": merged})
            if path == "/api/engine":
                running = bool(data.get("running", True))
                return self._json({"ok": True, "running": self.ctx.engine.set_running(running)})
            if path == "/api/source/test":
                return self._test_source(data)
            if path == "/api/notify/test":
                return self._test_notify(data)
            return self._error("接口不存在", 404)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return self._error(f"处理失败：{exc}", 500)

    def do_PUT(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        try:
            match = TASK_ITEM_RE.match(path)
            if not match:
                return self._error("接口不存在", 404)
            task_id = int(match.group(1))
            task = self.ctx.store.update_task(task_id, self._body())
            if not task:
                return self._error("任务不存在", 404)
            return self._json({"ok": True, "task": task})
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return self._error(f"更新失败：{exc}", 500)

    def do_DELETE(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        try:
            match = TASK_ITEM_RE.match(path)
            if not match:
                return self._error("接口不存在", 404)
            self.ctx.store.delete_task(int(match.group(1)))
            return self._json({"ok": True})
        except Exception as exc:  # noqa: BLE001
            return self._error(f"删除失败：{exc}", 500)

    # ---------------------------------------------------------------- 动作
    def _create_task(self, data: dict) -> None:
        name = str(data.get("name") or "").strip()
        if not name:
            return self._error("任务名不能为空")
        keywords = data.get("keywords") or []
        if isinstance(keywords, str):
            keywords = [k.strip() for k in keywords.replace("，", ",").split(",") if k.strip()]
        if not keywords:
            return self._error("至少填一个监控关键词")
        task = self.ctx.store.create_task({**data, "name": name, "keywords": keywords})
        return self._json({"ok": True, "task": task})

    def _import(self, data: dict) -> None:
        items = data.get("items")
        if not isinstance(items, list):
            text = str(data.get("text") or "")
            items = [r for r in parse_any(text) if float(r.get("price") or 0) > 0]
        if not items:
            return self._error(
                "没有解析到有效商品。支持四种写法：一行一条「标题 价格 链接」、"
                "闲鱼分享文案（需含价格）、逗号/制表符表格（表头含 标题,价格）、JSON 数组"
            )
        task_ids = data.get("task_ids")
        if not isinstance(task_ids, list) or not task_ids:
            task_ids = None
        result = self.ctx.engine.import_items(items, task_ids)
        return self._json({"ok": True, **result})

    def _test_source(self, data: dict) -> None:
        task = None
        if data.get("task_id"):
            task = self.ctx.store.get_task(int(data["task_id"]))
        if not task:
            task = {**DEFAULT_TASK, "keywords": data.get("keywords") or ["测试"],
                    "max_price": 5000, "source": data.get("source") or "mock",
                    "source_conf": data.get("source_conf") or {}}
        try:
            source = get_source(task["source"], task.get("source_conf"))
        except Exception as exc:  # noqa: BLE001
            return self._json({"ok": True, "reachable": False, "message": str(exc)})
        ok, message = source.test(task)
        return self._json({"ok": True, "reachable": ok, "message": message})

    def _test_notify(self, data: dict) -> None:
        hook = {"enabled": True, "type": data.get("type") or "feishu",
                "url": data.get("url") or ""}
        fake = {
            "task_id": 0, "id": 0, "item_id": "test", "task_name": "测试任务",
            "title": "这是一条测试推送：iPhone 13 128G 95新", "price": 1999,
            "ref_price": 3200, "discount": 0.38, "grade": "A", "score": 76,
            "url": "https://example.com", "seller": "测试卖家", "region": "惠州",
            "published_at": "",
        }
        notifier = self.ctx.notifier
        result = notifier._webhook(hook, f"捡漏提醒 · A级{GRADE_LABELS['A']}",  # noqa: SLF001
                                   notifier.compose(fake)[1], fake)
        return self._json(
            {"ok": True, "reachable": bool(result.get("ok")), "message": result.get("error", "")}
        )

    # ---------------------------------------------------------------- SSE
    def _sse(self) -> None:
        engine = self.ctx.engine
        q = engine.hub.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    event = q.get(timeout=12)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                payload = (
                    f"event: {event['event']}\ndata: "
                    f"{json.dumps(event['data'], ensure_ascii=False, default=str)}\n\n"
                ).encode("utf-8")
                self.wfile.write(payload)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            engine.hub.unsubscribe(q)

    # ---------------------------------------------------------------- 静态
    def _static(self, path: str) -> None:
        name = "index.html" if path in ("", "/") else Path(path).name
        file = WEB_DIR / name
        if not file.is_file():
            return self._send(404, b"Not Found", "text/plain; charset=utf-8")
        ctype = mimetypes.guess_type(str(file))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        return self._send(200, file.read_bytes(), ctype)


def _int(value) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, ctx: Context):
        self.ctx = ctx
        super().__init__(address, Handler)


def create_server(host: str, port: int, ctx: Context) -> Server:
    return Server((host, port), ctx)


def serve(host: str, port: int, ctx: Context) -> Server:
    """在 8765-8775 之间找一个可用端口启动。"""
    last_error: Exception | None = None
    for candidate in range(port, port + 12):
        try:
            return create_server(host, candidate, ctx)
        except OSError as exc:
            last_error = exc
    raise OSError(f"端口 {port}-{port + 11} 都被占用，请用 --port 指定其它端口：{last_error}")
