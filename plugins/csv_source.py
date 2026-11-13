"""示例插件：把本地 CSV 当成数据源。

用法：把有权使用的商品数据导出成 CSV，放进 data/watch.csv，然后新建任务时
选择「本地 CSV 文件」数据源即可。表头支持：

    title,price,url,seller,region,want,published_at,market_price

放在 plugins/ 目录下的任何 .py 文件都会被自动加载，只要里面定义了
Source 的子类并设置 name，就会出现在控制台的数据源下拉框里。
"""

from __future__ import annotations

import csv
import hashlib
from pathlib import Path

from app.config import DATA_DIR
from app.sources.base import RawItem, Source, _maybe_float, _to_float


class CsvSource(Source):
    name = "csv"
    label = "本地 CSV 文件（data/watch.csv）"
    polling = True

    def fetch(self, task: dict, limit: int = 30) -> list[RawItem]:
        path = Path(self.conf.get("path") or (DATA_DIR / "watch.csv"))
        if not path.is_absolute():
            path = DATA_DIR / path
        if not path.is_file():
            return []
        keywords = [str(k).lower() for k in (task.get("keywords") or [])]
        items: list[RawItem] = []
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                title = (row.get("title") or "").strip()
                if not title:
                    continue
                if keywords and not any(k in title.lower() for k in keywords):
                    continue
                price = _to_float(row.get("price"))
                if price <= 0:
                    continue
                item_id = (row.get("item_id") or "").strip() or "csv-" + hashlib.sha1(
                    f"{title}|{price}".encode("utf-8")
                ).hexdigest()[:14]
                items.append(
                    RawItem(
                        item_id=item_id,
                        title=title,
                        price=price,
                        url=(row.get("url") or "").strip(),
                        image=(row.get("image") or "").strip(),
                        seller=(row.get("seller") or "").strip(),
                        region=(row.get("region") or "").strip(),
                        want=int(_to_float(row.get("want"))),
                        published_at=(row.get("published_at") or "").strip(),
                        ref_price=_maybe_float(row.get("market_price")),
                        extra={
                            "market_price": _maybe_float(row.get("market_price")),
                            "free_shipping": (row.get("free_shipping") or "").lower()
                            in ("1", "true", "yes"),
                        },
                    )
                )
                if len(items) >= max(1, int(limit)):
                    break
        return items
