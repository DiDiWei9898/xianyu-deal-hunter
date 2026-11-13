"""演示数据源：不联网，按关键词生成价格分布合理的模拟商品。

用途是让整套流程（过滤 → 评分 → 提醒 → 看板）开箱即可跑通，
不代表真实行情，界面上会标记为「演示数据」。
"""

from __future__ import annotations

import hashlib
import random
import uuid
from datetime import datetime, timedelta

from ..store import TZ
from .base import RawItem, Source

# (关键词, 市场价下限, 上限, 常见规格)
CATALOG: list[tuple[tuple[str, ...], float, float, list[str]]] = [
    (("iphone", "苹果手机"), 2200, 5600, ["128G 国行", "256G 蓝色", "Pro 256G",
                                         "Pro Max 128G", "128G 双卡"]),
    (("手机", "小米", "华为", "安卓"), 700, 4200, ["12G+256G", "8G+128G", "Pro 版",
                                                "99 新电池健康 90"]),
    (("显卡", "rtx", "rx"), 900, 4800, ["RTX 3060 12G", "RTX 3070", "RX 6700XT",
                                       "RTX 2060S"]),
    (("相机", "微单", "单反"), 1400, 7600, ["A7M3 单机身", "EOS R 套机", "X-T4 18-55",
                                          "Z5 24-50"]),
    (("镜头",), 800, 5200, ["24-70 F4", "50 1.8", "70-200 F4", "35 1.4"]),
    (("switch", "游戏机"), 700, 1900, ["OLED 白色", "续航版 国行", "lite 全套"]),
    (("平板", "ipad"), 900, 4300, ["Air 4 64G", "Pro 11 128G", "mini 6 64G"]),
    (("笔记本", "macbook", "thinkpad"), 1500, 7200, ["Air M1 8+256", "X1 Carbon",
                                                    "小新 Pro 14"]),
    (("键盘", "机械键盘"), 200, 1300, ["87 键 茶轴", "65% 三模", "客制化 铝坨坨"]),
    (("耳机", "airpods", "降噪"), 150, 1600, ["AirPods Pro 2", "XM5", "Bose QC45"]),
    (("手表", "apple watch"), 300, 2800, ["S8 45mm", "SE 44mm", "Garmin 245"]),
    (("无人机", "dji"), 900, 6200, ["Mini 3 Pro", "Air 2S", "Mavic 2"]),
    (("显示器", "屏"), 400, 3200, ["27 寸 2K 165Hz", "4K 27 寸", "34 寸带鱼屏"]),
    (("主机", "台式", "整机"), 1000, 6500, ["12400F+3060", "5600+6600", "ITX 小钢炮"]),
]

CONDITIONS = ["99新", "95新", "9成新", "8成新", "全新未拆", "自用无磕碰"]
SELLERS = ["小鱼的二手仓", "阿凯的数码铺", "南山旧物", "惠州老张", "数码回收站",
           "学生党出闲置", "深夜数码", "海风闲置铺"]
REGIONS = ["惠州", "深圳", "广州", "东莞", "佛山", "上海", "北京", "杭州", "成都", "武汉"]
PREFIXES = ["个人闲置 ", "自用 ", "急出 ", "搬家清仓 ", "换新出 "]
SUFFIXES = ["可小刀", "当天发", "支持验机", "箱说全", "无拆无修"]


def _market_of(keyword: str) -> tuple[float, float, list[str]]:
    low = keyword.lower()
    for keys, lo, hi, specs in CATALOG:
        for key in keys:
            if key in low:
                return lo, hi, specs
    digest = int(hashlib.md5(low.encode("utf-8")).hexdigest(), 16)
    lo = float(80 + digest % 900)
    hi = lo * (1.6 + (digest % 7) / 10)
    return lo, hi, ["标准版", "高配", "全套", "轻奢款"]


class MockSource(Source):
    name = "mock"
    label = "演示数据（模拟行情）"
    polling = True

    def fetch(self, task: dict, limit: int = 30) -> list[RawItem]:
        rng = random
        keywords = task.get("keywords") or ["闲置好物"]
        max_price = float(task.get("max_price") or 1000)
        count = rng.choice([0, 1, 1, 2, 2, 3, 4])
        items: list[RawItem] = []
        for _ in range(count):
            keyword = rng.choice(list(keywords))
            lo, hi, specs = _market_of(str(keyword))
            market = round(lo + (hi - lo) * (rng.random() ** 0.7), -1)
            if rng.random() < 0.16:  # 深水价，制造 S 级命中
                factor = rng.uniform(0.32, 0.55)
            else:
                factor = rng.uniform(0.58, 1.06)
            price = max(9.0, round(market * factor, 0))
            if price > max_price and rng.random() < 0.65:
                price = round(max_price * rng.uniform(0.55, 0.95), 0)
            spec = rng.choice(specs)
            title = (
                f"{rng.choice(PREFIXES)}{keyword} · {spec} · {rng.choice(CONDITIONS)} "
                f"{rng.choice(SUFFIXES)}"
            )
            minutes = rng.choice([2, 5, 9, 14, 23, 40, 75, 130, 260, 480])
            published = (datetime.now(TZ) - timedelta(minutes=minutes)).isoformat(timespec="seconds")
            items.append(
                RawItem(
                    item_id=f"mock-{uuid.uuid4().hex[:10]}",
                    title=title,
                    price=float(price),
                    url="",
                    seller=rng.choice(SELLERS),
                    region=rng.choice(REGIONS),
                    want=rng.choice([3, 8, 15, 26, 41, 88, 150, 320]),
                    published_at=published,
                    ref_price=float(round(market, 0)),
                    extra={
                        "market_price": float(round(market, 0)),
                        "free_shipping": rng.random() < 0.55,
                        "seller_score": rng.choice([62, 71, 84, 92, 97, 100]),
                        "demo": True,
                    },
                )
            )
        return items[: max(1, int(limit))]
