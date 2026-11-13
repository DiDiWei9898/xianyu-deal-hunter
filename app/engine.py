"""轮询引擎：调度任务、过滤匹配、评分去重、触发推送、广播事件。"""

from __future__ import annotations

import queue
import sys
import threading
import traceback
from datetime import datetime

from .config import DEFAULT_SETTINGS
from .scoring import evaluate
from .sources import RawItem, get_source
from .store import TZ, median, now_iso, parse_dt


class Hub:
    """极简事件总线：每个 SSE 客户端一个队列，满了就丢最旧的。"""

    def __init__(self) -> None:
        self._clients: set[queue.Queue] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=100)
        with self._lock:
            self._clients.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._clients.discard(q)

    def publish(self, event: str, data: dict) -> None:
        with self._lock:
            clients = list(self._clients)
        for q in clients:
            try:
                if q.full():
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        pass
                q.put_nowait({"event": event, "data": data})
            except Exception:  # noqa: BLE001
                pass


class Engine:
    def __init__(self, store, notifier) -> None:
        self.store = store
        self.notifier = notifier
        self.hub = Hub()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._active: set[int] = set()
        self._active_lock = threading.Lock()
        self._gate = threading.Condition()
        self._inflight = 0

    # ---------------------------------------------------------------- 生命周期
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="engine", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._paused.is_set())

    def set_running(self, value: bool) -> bool:
        if value:
            self._paused.clear()
            self.start()
        else:
            self._paused.set()
        return self.running

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if not self._paused.is_set():
                    self.tick()
            except Exception:  # noqa: BLE001
                print("[engine] 调度异常：", file=sys.stderr)
                traceback.print_exc()
            self._stop.wait(1.0)

    def tick(self) -> list[int]:
        """把所有到期任务投出去，返回本次启动的任务 id。"""
        started: list[int] = []
        for task in self.store.list_tasks():
            if not task.get("enabled"):
                continue
            if self._paused.is_set():
                continue
            if self._due(task) and self.submit(task["id"]):
                started.append(task["id"])
        return started

    @staticmethod
    def _due(task: dict) -> bool:
        last = parse_dt(task.get("last_run_at") or "")
        if last is None:
            return True
        gap = (datetime.now(TZ) - last).total_seconds()
        return gap >= max(5, int(task.get("interval_sec") or 30))

    # ---------------------------------------------------------------- 执行
    def submit(self, task_id: int) -> bool:
        with self._active_lock:
            if task_id in self._active:
                return False
            self._active.add(task_id)
        thread = threading.Thread(target=self._run, args=(int(task_id),), daemon=True)
        thread.start()
        return True

    def run_sync(self, task_id: int) -> dict:
        """同步跑一次，测试用。"""
        return self._run(int(task_id))

    def _settings(self) -> dict:
        try:
            return self.store.get_settings()
        except Exception:  # noqa: BLE001
            return DEFAULT_SETTINGS

    def _limit(self) -> int:
        try:
            value = int((self._settings().get("engine") or {}).get("max_concurrent") or 3)
        except (TypeError, ValueError):
            value = 3
        return max(1, min(8, value))

    def _acquire_slot(self) -> None:
        """全局限并发：设置改了立即生效，不需要重启引擎。"""
        with self._gate:
            while self._inflight >= self._limit():
                self._gate.wait(1.0)
            self._inflight += 1

    def _release_slot(self) -> None:
        with self._gate:
            self._inflight = max(0, self._inflight - 1)
            self._gate.notify_all()

    def _run(self, task_id: int) -> dict:
        out = {"task_id": task_id, "fetched": 0, "hits": 0, "error": ""}
        self._acquire_slot()
        try:
            task = self.store.get_task(task_id)
            if not task:
                out["error"] = "任务不存在"
                return out
            if task["source"] == "manual":
                self.store.touch_task(task_id)
                return out
            source = get_source(task["source"], task.get("source_conf"))
            limit = int((self._settings().get("engine") or {}).get("fetch_limit") or 30)
            items = source.fetch(task, limit=limit)
            out["fetched"] = len(items)
            out["hits"] = self.ingest(items, [task_id])
            self.store.touch_task(task_id)
            self.hub.publish(
                "task_status",
                {"task_id": task_id, "ok": True, "fetched": len(items), "hits": out["hits"]},
            )
        except Exception as exc:  # noqa: BLE001
            message = f"{type(exc).__name__}: {exc}"
            out["error"] = message
            try:
                self.store.touch_task(task_id, message)
            except Exception:  # noqa: BLE001
                pass
            self.hub.publish("task_error", {"task_id": task_id, "error": message})
        finally:
            self._release_slot()
            with self._active_lock:
                self._active.discard(task_id)
        return out

    # ---------------------------------------------------------------- 匹配
    def ingest(self, raw_items: list[RawItem], task_ids: list[int] | None = None) -> int:
        tasks = self._targets(task_ids)
        hits = 0
        for raw in raw_items or []:
            if raw.price <= 0 or not raw.title:
                continue
            for task in tasks:
                hit = self._consider(task, raw)
                if hit:
                    hits += 1
        if hits:
            self.hub.publish("stats", self.store.stats())
        return hits

    def _targets(self, task_ids: list[int] | None) -> list[dict]:
        if task_ids:
            return [t for t in (self.store.get_task(i) for i in task_ids) if t]
        return [t for t in self.store.list_tasks() if t.get("enabled")]

    def _consider(self, task: dict, raw: RawItem) -> dict | None:
        price = float(raw.price)
        if price < float(task.get("min_price") or 0) or price > float(task.get("max_price") or 0):
            return None
        title_low = raw.title.lower()
        keywords = [str(k).lower() for k in (task.get("keywords") or [])]
        if keywords and not any(k in title_low for k in keywords):
            return None
        for word in task.get("exclude") or []:
            if str(word).strip() and str(word).strip().lower() in title_low:
                return None
        want_region = str(task.get("region") or "").strip()
        if want_region and raw.region and want_region not in raw.region:
            return None

        ref = self._ref_price(task, raw)
        discount = round((ref - price) / ref, 4) if ref and ref > 0 else 0.0

        item = {
            "item_id": raw.item_id,
            "title": raw.title,
            "price": price,
            "ref_price": ref,
            "url": raw.url,
            "image": raw.image,
            "seller": raw.seller,
            "region": raw.region,
            "want": raw.want,
            "published_at": raw.published_at,
            "extra": raw.extra or {},
        }
        self.store.upsert_item(item)
        self.store.add_snapshot(int(task["id"]), raw.item_id, price)

        if ref is None:
            # 没有基准价就无法判断「便宜」，只入库作为后续基准，不记命中
            return None
        if discount < float(task.get("min_discount") or 0):
            return None

        score, grade, reasons = evaluate(item, task, ref, discount)
        hit_id = self.store.record_hit(
            int(task["id"]), raw.item_id, price, ref, discount, score, grade, reasons
        )
        if not hit_id:
            return None

        self.store.bump_hits(int(task["id"]))
        hit = self.store.get_hit(hit_id) or {}
        hit["demo"] = bool((raw.extra or {}).get("demo"))
        self.hub.publish("hit", hit)

        threshold = float((self._settings().get("notify") or {}).get("min_score") or 68)
        if score >= threshold:
            try:
                self.notifier.notify(hit)
                self.store.mark_notified(hit_id)
                hit["notified"] = True
                self.hub.publish("alert", {"hit_id": hit_id, "title": hit.get("title", "")})
            except Exception as exc:  # noqa: BLE001
                self.hub.publish("notify_error", {"hit_id": hit_id, "error": str(exc)})
        return hit

    def _ref_price(self, task: dict, raw: RawItem) -> float | None:
        market = (raw.extra or {}).get("market_price") or raw.ref_price
        try:
            if market and float(market) > 0:
                return float(market)
        except (TypeError, ValueError):
            pass
        if task.get("ref_price"):
            return float(task["ref_price"])
        return median(self.store.recent_prices(int(task["id"]), raw.item_id, limit=400))

    # ---------------------------------------------------------------- 手动导入
    def import_items(self, items: list[dict], task_ids: list[int] | None = None) -> dict:
        raws: list[RawItem] = []
        for idx, item in enumerate(items):
            data = RawItem.from_dict(
                {**item, "item_id": item.get("item_id") or f"imp-{now_iso()}-{idx}"}
            )
            if data.published_at in ("", None):
                data.published_at = now_iso()
            raws.append(data)
        hits = self.ingest(raws, task_ids)
        return {"imported": len(raws), "hits": hits}
