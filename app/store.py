"""SQLite 存储层：任务 / 商品 / 命中 / 价格快照 / 推送记录 / 设置。

所有写操作按需开连接，配合 WAL 模式，能在 HTTP 线程与调度线程间安全共享。
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from .config import DEFAULT_SETTINGS, DEFAULT_TASK, deep_merge

TZ = timezone(timedelta(hours=8))

TASK_COLUMNS = (
    "name",
    "keywords",
    "exclude",
    "source",
    "source_conf",
    "max_price",
    "min_price",
    "min_discount",
    "ref_price",
    "region",
    "interval_sec",
    "enabled",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    keywords      TEXT    NOT NULL DEFAULT '[]',
    exclude       TEXT    NOT NULL DEFAULT '[]',
    source        TEXT    NOT NULL DEFAULT 'mock',
    source_conf   TEXT    NOT NULL DEFAULT '{}',
    max_price     REAL    NOT NULL DEFAULT 1000,
    min_price     REAL    NOT NULL DEFAULT 0,
    min_discount  REAL    NOT NULL DEFAULT 0.15,
    ref_price     REAL,
    region        TEXT    NOT NULL DEFAULT '',
    interval_sec  INTEGER NOT NULL DEFAULT 30,
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT    NOT NULL DEFAULT '',
    last_run_at   TEXT,
    last_error    TEXT,
    hit_count     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS items (
    item_id       TEXT PRIMARY KEY,
    title         TEXT NOT NULL DEFAULT '',
    price         REAL NOT NULL DEFAULT 0,
    ref_price     REAL,
    url           TEXT NOT NULL DEFAULT '',
    image         TEXT NOT NULL DEFAULT '',
    seller        TEXT NOT NULL DEFAULT '',
    region        TEXT NOT NULL DEFAULT '',
    want          INTEGER NOT NULL DEFAULT 0,
    published_at  TEXT NOT NULL DEFAULT '',
    raw           TEXT NOT NULL DEFAULT '{}',
    first_seen    TEXT NOT NULL DEFAULT '',
    last_updated  TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS hits (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER NOT NULL,
    item_id     TEXT    NOT NULL,
    price       REAL    NOT NULL,
    ref_price   REAL,
    discount    REAL    NOT NULL DEFAULT 0,
    score       REAL    NOT NULL DEFAULT 0,
    grade       TEXT    NOT NULL DEFAULT 'C',
    reasons     TEXT    NOT NULL DEFAULT '[]',
    status      TEXT    NOT NULL DEFAULT 'new',
    notified    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL DEFAULT '',
    UNIQUE (task_id, item_id)
);

CREATE TABLE IF NOT EXISTS price_snapshots (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id   INTEGER NOT NULL,
    item_id   TEXT    NOT NULL,
    price     REAL    NOT NULL,
    ts        TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER NOT NULL,
    hit_id      INTEGER NOT NULL,
    item_id     TEXT    NOT NULL DEFAULT '',
    title       TEXT    NOT NULL DEFAULT '',
    price       REAL    NOT NULL DEFAULT 0,
    channel     TEXT    NOT NULL DEFAULT '',
    ok          INTEGER NOT NULL DEFAULT 0,
    error       TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_hits_created ON hits (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_hits_task ON hits (task_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_snapshot_task ON price_snapshots (task_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts (created_at DESC);
"""


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt


def median(values: list[float]) -> float | None:
    nums = sorted(v for v in values if v and v > 0)
    if not nums:
        return None
    mid = len(nums) // 2
    if len(nums) % 2:
        return float(nums[mid])
    return float((nums[mid - 1] + nums[mid]) / 2)


def _loads(text: str, fallback: Any) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return fallback


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.init_db()

    # ------------------------------------------------------------------ 基础
    @contextmanager
    def db(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_db(self) -> None:
        with self.db() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(SCHEMA)
        with self.db() as conn:
            seeded = conn.execute("SELECT 1 FROM meta WHERE key = 'seeded'").fetchone()
        if not seeded:
            self.seed()
            with self.db() as conn:
                conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('seeded', '1')")

    def seed(self) -> None:
        """首次启动时放入两个演示任务，保证打开就能看到效果。"""
        self.create_task(
            {
                "name": "iPhone 13/14 捡漏",
                "keywords": ["iphone 13", "iphone 14"],
                "exclude": ["配件", "壳", "膜", "维修", "主板"],
                "source": "mock",
                "max_price": 3200,
                "min_discount": 0.25,
                "interval_sec": 20,
            }
        )
        self.create_task(
            {
                "name": "显卡 / 相机 捡漏",
                "keywords": ["显卡", "相机"],
                "exclude": ["散热器", "支架", "贴纸"],
                "source": "mock",
                "max_price": 2600,
                "min_discount": 0.2,
                "interval_sec": 30,
            }
        )

    # ------------------------------------------------------------------ 任务
    def _task_out(self, row: sqlite3.Row) -> dict:
        out = dict(row)
        out["keywords"] = _loads(out.get("keywords") or "[]", [])
        out["exclude"] = _loads(out.get("exclude") or "[]", [])
        out["source_conf"] = _loads(out.get("source_conf") or "{}", {})
        out["enabled"] = bool(out.get("enabled"))
        return out

    def list_tasks(self) -> list[dict]:
        with self.db() as conn:
            rows = conn.execute("SELECT * FROM tasks ORDER BY id ASC").fetchall()
        return [self._task_out(r) for r in rows]

    def get_task(self, task_id: int) -> dict | None:
        with self.db() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (int(task_id),)).fetchone()
        return self._task_out(row) if row else None

    @staticmethod
    def _normalize(data: dict) -> dict:
        out: dict[str, Any] = {}
        for key in TASK_COLUMNS:
            if key in data:
                out[key] = data[key]
        if "keywords" in out:
            out["keywords"] = json.dumps(_to_list(out["keywords"]), ensure_ascii=False)
        if "exclude" in out:
            out["exclude"] = json.dumps(_to_list(out["exclude"]), ensure_ascii=False)
        if "source_conf" in out:
            out["source_conf"] = json.dumps(out["source_conf"] or {}, ensure_ascii=False)
        if "enabled" in out:
            out["enabled"] = 1 if out["enabled"] else 0
        if "interval_sec" in out:
            out["interval_sec"] = max(5, int(out["interval_sec"]))
        if "max_price" in out:
            out["max_price"] = max(0.0, float(out["max_price"]))
        if "min_price" in out:
            out["min_price"] = max(0.0, float(out["min_price"]))
        if "min_discount" in out:
            out["min_discount"] = min(0.98, max(0.0, float(out["min_discount"])))
        if "ref_price" in out:
            value = out["ref_price"]
            out["ref_price"] = float(value) if value not in (None, "", 0, "0") else None
        return out

    def create_task(self, data: dict) -> dict:
        payload = self._normalize({**DEFAULT_TASK, **data})
        payload["created_at"] = now_iso()
        cols = list(payload.keys())
        placeholders = ",".join("?" for _ in cols)
        with self.db() as conn:
            cur = conn.execute(
                f"INSERT INTO tasks ({','.join(cols)}) VALUES ({placeholders})",
                [payload[c] for c in cols],
            )
            task_id = cur.lastrowid
        return self.get_task(task_id) or {}

    def update_task(self, task_id: int, data: dict) -> dict | None:
        payload = self._normalize(data)
        if not payload:
            return self.get_task(task_id)
        cols = list(payload.keys())
        sets = ",".join(f"{c} = ?" for c in cols)
        with self.db() as conn:
            conn.execute(
                f"UPDATE tasks SET {sets} WHERE id = ?",
                [payload[c] for c in cols] + [int(task_id)],
            )
        return self.get_task(task_id)

    def delete_task(self, task_id: int) -> None:
        with self.db() as conn:
            conn.execute("DELETE FROM tasks WHERE id = ?", (int(task_id),))
            conn.execute("DELETE FROM hits WHERE task_id = ?", (int(task_id),))
            conn.execute("DELETE FROM price_snapshots WHERE task_id = ?", (int(task_id),))

    def touch_task(self, task_id: int, error: str | None = None) -> None:
        with self.db() as conn:
            conn.execute(
                "UPDATE tasks SET last_run_at = ?, last_error = ? WHERE id = ?",
                (now_iso(), (error or "")[:500], int(task_id)),
            )

    def bump_hits(self, task_id: int) -> None:
        with self.db() as conn:
            conn.execute("UPDATE tasks SET hit_count = hit_count + 1 WHERE id = ?", (int(task_id),))

    # ------------------------------------------------------------------ 商品
    def upsert_item(self, item: dict) -> bool:
        """写入/更新商品，返回 True 表示首次出现。"""
        now = now_iso()
        item_id = str(item["item_id"])
        with self.db() as conn:
            row = conn.execute("SELECT price FROM items WHERE item_id = ?", (item_id,)).fetchone()
            if row is None:
                conn.execute(
                    """INSERT INTO items (item_id, title, price, ref_price, url, image, seller,
                       region, want, published_at, raw, first_seen, last_updated)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        item_id,
                        item.get("title", ""),
                        float(item.get("price") or 0),
                        item.get("ref_price"),
                        item.get("url", ""),
                        item.get("image", ""),
                        item.get("seller", ""),
                        item.get("region", ""),
                        int(item.get("want") or 0),
                        item.get("published_at", ""),
                        json.dumps(item.get("raw") or {}, ensure_ascii=False)[:4000],
                        now,
                        now,
                    ),
                )
                return True
            conn.execute(
                """UPDATE items SET title=?, price=?, ref_price=?, url=?, image=?, seller=?,
                   region=?, want=?, published_at=?, raw=?, last_updated=? WHERE item_id=?""",
                (
                    item.get("title", ""),
                    float(item.get("price") or 0),
                    item.get("ref_price"),
                    item.get("url", ""),
                    item.get("image", ""),
                    item.get("seller", ""),
                    item.get("region", ""),
                    int(item.get("want") or 0),
                    item.get("published_at", ""),
                    json.dumps(item.get("raw") or {}, ensure_ascii=False)[:4000],
                    now,
                    item_id,
                ),
            )
            return False

    def get_item(self, item_id: str) -> dict | None:
        with self.db() as conn:
            row = conn.execute("SELECT * FROM items WHERE item_id = ?", (item_id,)).fetchone()
        return dict(row) if row else None

    def add_snapshot(self, task_id: int, item_id: str, price: float) -> None:
        """记录价格快照；同一商品价格没变就不重复写，避免轮询把表撑爆。"""
        if price <= 0:
            return
        with self.db() as conn:
            last = conn.execute(
                "SELECT price FROM price_snapshots WHERE task_id = ? AND item_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (int(task_id), str(item_id)),
            ).fetchone()
            if last is not None and abs(float(last["price"]) - float(price)) < 0.01:
                return
            conn.execute(
                "INSERT INTO price_snapshots (task_id, item_id, price, ts) VALUES (?,?,?,?)",
                (int(task_id), str(item_id), float(price), now_iso()),
            )

    def recent_prices(self, task_id: int, exclude_item_id: str = "", limit: int = 400) -> list[float]:
        with self.db() as conn:
            rows = conn.execute(
                "SELECT price FROM price_snapshots WHERE task_id = ? AND item_id != ? "
                "ORDER BY id DESC LIMIT ?",
                (int(task_id), exclude_item_id, int(limit)),
            ).fetchall()
        return [float(r["price"]) for r in rows]

    # ------------------------------------------------------------------ 命中
    def record_hit(
        self,
        task_id: int,
        item_id: str,
        price: float,
        ref_price: float | None,
        discount: float,
        score: float,
        grade: str,
        reasons: list[str],
    ) -> int | None:
        try:
            with self.db() as conn:
                cur = conn.execute(
                    """INSERT INTO hits (task_id, item_id, price, ref_price, discount, score,
                       grade, reasons, status, notified, created_at)
                       VALUES (?,?,?,?,?,?,?,?, 'new', 0, ?)""",
                    (
                        int(task_id),
                        str(item_id),
                        float(price),
                        ref_price,
                        float(discount),
                        float(score),
                        grade,
                        json.dumps(reasons, ensure_ascii=False),
                        now_iso(),
                    ),
                )
                return int(cur.lastrowid)
        except sqlite3.IntegrityError:
            return None

    def _hit_out(self, row: sqlite3.Row) -> dict:
        out = dict(row)
        out["reasons"] = _loads(out.get("reasons") or "[]", [])
        out["notified"] = bool(out.get("notified"))
        return out

    def get_hit(self, hit_id: int) -> dict | None:
        with self.db() as conn:
            row = conn.execute(
                """SELECT h.*, t.name AS task_name, i.title, i.url, i.image, i.seller, i.region,
                          i.want, i.published_at, i.price AS current_price
                   FROM hits h
                   JOIN tasks t ON t.id = h.task_id
                   LEFT JOIN items i ON i.item_id = h.item_id
                   WHERE h.id = ?""",
                (int(hit_id),),
            ).fetchone()
        return self._hit_out(row) if row else None

    def list_hits(self, task_id: int | None = None, grade: str | None = None,
                  limit: int = 60, offset: int = 0) -> list[dict]:
        sql = [
            "SELECT h.*, t.name AS task_name, i.title, i.url, i.image, i.seller, i.region,",
            "i.want, i.published_at, i.price AS current_price",
            "FROM hits h JOIN tasks t ON t.id = h.task_id",
            "LEFT JOIN items i ON i.item_id = h.item_id WHERE 1 = 1",
        ]
        args: list[Any] = []
        if task_id:
            sql.append("AND h.task_id = ?")
            args.append(int(task_id))
        if grade:
            sql.append("AND h.grade = ?")
            args.append(grade)
        sql.append("ORDER BY h.id DESC LIMIT ? OFFSET ?")
        args.extend([max(1, min(200, int(limit))), max(0, int(offset))])
        with self.db() as conn:
            rows = conn.execute(" ".join(sql), args).fetchall()
        return [self._hit_out(r) for r in rows]

    def mark_hit(self, hit_id: int, status: str = "ignored") -> None:
        with self.db() as conn:
            conn.execute("UPDATE hits SET status = ? WHERE id = ?", (status, int(hit_id)))

    def mark_notified(self, hit_id: int) -> None:
        with self.db() as conn:
            conn.execute("UPDATE hits SET notified = 1 WHERE id = ?", (int(hit_id),))

    # ------------------------------------------------------------------ 提醒
    def log_alert(self, task_id: int, hit_id: int, item_id: str, title: str,
                  price: float, channel: str, ok: bool, error: str = "") -> None:
        with self.db() as conn:
            conn.execute(
                """INSERT INTO alerts (task_id, hit_id, item_id, title, price, channel, ok,
                   error, created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                (int(task_id), int(hit_id), item_id, title[:120], float(price), channel,
                 1 if ok else 0, error[:300], now_iso()),
            )

    def list_alerts(self, limit: int = 40) -> list[dict]:
        with self.db() as conn:
            rows = conn.execute(
                "SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (max(1, int(limit)),)
            ).fetchall()
        return [{**dict(r), "ok": bool(r["ok"])} for r in rows]

    # ------------------------------------------------------------------ 设置
    def get_settings(self) -> dict:
        with self.db() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        stored = {r["key"]: _loads(r["value"], {}) for r in rows}
        return deep_merge(DEFAULT_SETTINGS, stored)

    def save_settings(self, data: dict) -> dict:
        merged = deep_merge(self.get_settings(), data)
        with self.db() as conn:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES ('main', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (json.dumps(merged, ensure_ascii=False),),
            )
        return merged

    # ------------------------------------------------------------------ 统计
    def stats(self) -> dict:
        today = datetime.now(TZ).date().isoformat()
        with self.db() as conn:
            total_tasks = conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
            active_tasks = conn.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE enabled = 1"
            ).fetchone()["n"]
            today_hits = conn.execute(
                "SELECT COUNT(*) AS n FROM hits WHERE substr(created_at,1,10) = ?", (today,)
            ).fetchone()["n"]
            today_alerts = conn.execute(
                "SELECT COUNT(*) AS n FROM hits WHERE notified = 1 AND substr(created_at,1,10) = ?",
                (today,),
            ).fetchone()["n"]
            avg = conn.execute(
                "SELECT AVG(discount) AS d, MAX(score) AS s FROM hits WHERE substr(created_at,1,10) = ?",
                (today,),
            ).fetchone()
            total_items = conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
            best = conn.execute(
                "SELECT MAX(discount) AS d FROM hits WHERE substr(created_at,1,10) = ?", (today,)
            ).fetchone()
        return {
            "total_tasks": total_tasks,
            "active_tasks": active_tasks,
            "today_hits": today_hits,
            "today_alerts": today_alerts,
            "avg_discount": round(float(avg["d"] or 0), 4),
            "max_score": round(float(avg["s"] or 0), 1),
            "total_items": total_items,
            "best_discount": round(float(best["d"] or 0), 4),
        }

    def grade_distribution(self) -> dict:
        with self.db() as conn:
            rows = conn.execute("SELECT grade, COUNT(*) AS n FROM hits GROUP BY grade").fetchall()
        out = {"S": 0, "A": 0, "B": 0, "C": 0}
        for r in rows:
            out[r["grade"]] = int(r["n"])
        return out

    def browsing_history(self, task_id: int, limit: int = 20) -> list[dict]:
        with self.db() as conn:
            rows = conn.execute(
                """SELECT item_id, price, ts FROM price_snapshots
                   WHERE task_id = ? ORDER BY id DESC LIMIT ?""",
                (int(task_id), int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ 导出
    def export_csv(self, hits: Iterable[dict] | None = None) -> str:
        rows = hits if hits is not None else self.list_hits(limit=200)
        buf = io.StringIO()
        buf.write("\ufeff")
        writer = csv.writer(buf)
        writer.writerow(
            ["命中时间", "任务", "等级", "评分", "标题", "现价", "参考价",
             "折扣", "卖家", "地区", "想要", "发布时间", "链接"]
        )
        for h in rows:
            writer.writerow(
                [
                    h.get("created_at", ""),
                    h.get("task_name", ""),
                    h.get("grade", ""),
                    h.get("score", 0),
                    h.get("title", ""),
                    h.get("price", 0),
                    h.get("ref_price") or "",
                    f"{float(h.get('discount') or 0) * 100:.1f}%",
                    h.get("seller", ""),
                    h.get("region", ""),
                    h.get("want", 0),
                    h.get("published_at", ""),
                    h.get("url", ""),
                ]
            )
        return buf.getvalue()


def _to_list(value: Any) -> list[str]:
    if isinstance(value, list):
        items = value
    elif isinstance(value, str):
        items = value.replace("，", ",").split(",")
    else:
        items = []
    return [str(v).strip() for v in items if str(v).strip()]
