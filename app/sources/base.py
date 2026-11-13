"""数据源基类。

一条硬规则：本项目不实现任何绕过平台风控、伪造签名或未授权抓取的逻辑。
所有真实数据都必须来自用户自己有权访问的接口。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RawItem:
    item_id: str
    title: str
    price: float
    url: str = ""
    image: str = ""
    seller: str = ""
    region: str = ""
    want: int = 0
    published_at: str = ""
    ref_price: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "item_id": self.item_id,
            "title": self.title,
            "price": self.price,
            "url": self.url,
            "image": self.image,
            "seller": self.seller,
            "region": self.region,
            "want": self.want,
            "published_at": self.published_at,
            "ref_price": self.ref_price,
            "extra": self.extra,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RawItem":
        return cls(
            item_id=str(data.get("item_id") or "").strip(),
            title=str(data.get("title") or "").strip(),
            price=_to_float(data.get("price")),
            url=str(data.get("url") or "").strip(),
            image=str(data.get("image") or "").strip(),
            seller=str(data.get("seller") or "").strip(),
            region=str(data.get("region") or "").strip(),
            want=int(_to_float(data.get("want"))),
            published_at=str(data.get("published_at") or "").strip(),
            ref_price=_maybe_float(data.get("ref_price")),
            extra=data.get("extra") if isinstance(data.get("extra"), dict) else {},
        )


def _to_float(value: Any) -> float:
    try:
        return float(str(value).replace("¥", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return 0.0


def _maybe_float(value: Any) -> float | None:
    if value in (None, "", 0, "0", "0.0"):
        return None
    num = _to_float(value)
    return num if num > 0 else None


class Source:
    """数据源接口。子类只需实现 fetch()。"""

    name: str = "base"
    label: str = "未知数据源"
    polling: bool = True

    def __init__(self, conf: dict | None = None) -> None:
        self.conf = conf or {}

    def fetch(self, task: dict, limit: int = 30) -> list[RawItem]:
        raise NotImplementedError

    def describe(self) -> dict:
        return {"name": self.name, "label": self.label, "polling": self.polling}

    def test(self, task: dict) -> tuple[bool, str]:
        try:
            items = self.fetch(task, limit=3)
        except Exception as exc:  # noqa: BLE001 - 探测用，错误直接回显
            return False, f"{type(exc).__name__}: {exc}"
        if not items:
            return True, "连通正常，但本次没有取到商品（可能无结果或未到更新时间）"
        sample = items[0]
        return True, f"连通正常，取到 {len(items)} 条，示例：{sample.title[:40]} ¥{sample.price:g}"
