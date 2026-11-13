"""通用 HTTP JSON 数据源适配器。

用途：你已拿到「有权访问」的接口（官方开放平台、公司内部网关、自己买的
合规数据服务），无需改代码，只在任务的 source_conf 里填地址与字段映射即可。

示例 conf：
{
  "url": "https://api.example.com/search",
  "method": "GET",
  "headers": {"Authorization": "Bearer <token>"},
  "params": {"q": "{keyword}", "maxPrice": "{max_price}"},
  "items_path": "data.items",
  "map": {
    "item_id": "id",
    "title": "title",
    "price": "price",
    "url": "url",
    "image": "pic",
    "seller": "seller.nick",
    "region": "area",
    "want": "wantCnt",
    "published_at": "gmtCreate",
    "market_price": "marketPrice"
  }
}
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from .base import RawItem, Source, _to_float

DEFAULT_MAP = {
    "item_id": "id",
    "title": "title",
    "price": "price",
    "url": "url",
    "image": "image",
    "seller": "seller",
    "region": "region",
    "want": "want",
    "published_at": "published_at",
    "market_price": "market_price",
}

CN_TZ = timezone(timedelta(hours=8))


def dig(data: object, path: str, default=None):
    """按 "a.b.0.c" 取值，取不到返回 default。"""
    if not path:
        return data
    cur = data
    for part in str(path).split("."):
        if cur is None:
            return default
        if isinstance(cur, dict):
            cur = cur.get(part, default)
        elif isinstance(cur, (list, tuple)):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return default
        else:
            return default
    return cur


def to_iso(value) -> str:
    """把秒/毫秒时间戳、常见日期串统一成 ISO 字符串。"""
    if value in (None, "", 0):
        return ""
    if isinstance(value, (int, float)):
        num = float(value)
        if num > 1e11:
            num /= 1000
        try:
            return datetime.fromtimestamp(num, CN_TZ).isoformat(timespec="seconds")
        except (OverflowError, OSError, ValueError):
            return ""
    text = str(value).strip()
    if text.isdigit() and len(text) >= 10:
        return to_iso(int(text))
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CN_TZ)
    return dt.astimezone(CN_TZ).isoformat(timespec="seconds")


class HttpJsonSource(Source):
    name = "http"
    label = "自定义 HTTP 接口（需自行授权）"
    polling = True

    def _render(self, value, task: dict):
        keywords = task.get("keywords") or []
        tokens = {
            "{keyword}": str(keywords[0]) if keywords else str(task.get("name") or ""),
            "{keywords}": ",".join(str(k) for k in keywords),
            "{max_price}": str(int(float(task.get("max_price") or 0))),
            "{min_price}": str(int(float(task.get("min_price") or 0))),
            "{task_name}": str(task.get("name") or ""),
        }
        if isinstance(value, str):
            out = value
            for key, val in tokens.items():
                out = out.replace(key, urllib.parse.quote(val) if key == "{keyword}" else val)
            return out
        if isinstance(value, dict):
            return {k: self._render(v, task) for k, v in value.items()}
        if isinstance(value, list):
            return [self._render(v, task) for v in value]
        return value

    def fetch(self, task: dict, limit: int = 30) -> list[RawItem]:
        conf = self.conf or {}
        url = str(conf.get("url") or "").strip()
        if not url:
            raise ValueError("未配置接口地址：请在任务的 source_conf.url 填写已授权的接口")
        url = self._render(url, task)
        method = str(conf.get("method") or "GET").upper()
        headers = {"User-Agent": "XianyuDealHunter/1.0", "Accept": "application/json"}
        headers.update(self._render(conf.get("headers") or {}, task))
        params = self._render(conf.get("params") or {}, task)
        body = self._render(conf.get("json_body"), task) if conf.get("json_body") else None
        timeout = float(conf.get("timeout") or 15)

        if method == "GET":
            if params:
                url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(
                    params, doseq=True
                )
            request = urllib.request.Request(url, headers=headers, method="GET")
        else:
            payload = json.dumps(body if body is not None else params, ensure_ascii=False).encode()
            headers["Content-Type"] = "application/json"
            request = urllib.request.Request(url, data=payload, headers=headers, method=method)

        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(f"接口返回 HTTP {exc.code} {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"无法连接接口：{exc.reason}") from exc

        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except ValueError as exc:
            raise RuntimeError("接口返回的不是合法 JSON") from exc

        row_list = dig(data, conf.get("items_path") or "data", [])
        if isinstance(row_list, dict):
            row_list = row_list.get("items") or row_list.get("list") or []
        if not isinstance(row_list, list):
            raise RuntimeError("items_path 没有取到数组，请检查字段路径")

        field_map = {**DEFAULT_MAP, **(conf.get("map") or {})}
        items: list[RawItem] = []
        for row in row_list[: max(1, int(limit)) * 3]:
            if not isinstance(row, dict):
                continue
            title = str(dig(row, field_map["title"], "") or "").strip()
            price = _to_float(dig(row, field_map["price"], 0))
            if not title or price <= 0:
                continue
            item_id = str(dig(row, field_map["item_id"], "") or "").strip()
            if not item_id:
                item_id = "h-" + hashlib.sha1(
                    f"{title}|{price}".encode("utf-8")
                ).hexdigest()[:16]
            market = _to_float(dig(row, field_map["market_price"], 0)) or None
            want_raw = dig(row, field_map["want"], 0)
            items.append(
                RawItem(
                    item_id=item_id,
                    title=title,
                    price=price,
                    url=str(dig(row, field_map["url"], "") or ""),
                    image=str(dig(row, field_map["image"], "") or ""),
                    seller=str(dig(row, field_map["seller"], "") or ""),
                    region=str(dig(row, field_map["region"], "") or ""),
                    want=int(_to_float(want_raw)),
                    published_at=to_iso(dig(row, field_map["published_at"], "")),
                    ref_price=market,
                    extra={
                        "market_price": market,
                        "free_shipping": bool(dig(row, "freeShipping", False)),
                        "seller_score": dig(row, "sellerScore", None),
                        "raw_row": row,
                    },
                )
            )
            if len(items) >= max(1, int(limit)):
                break
        return items
