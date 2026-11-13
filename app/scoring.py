"""捡漏评分：把「便宜多少 / 多新 / 卖家可信度 / 附加条件」折算成 0-100 分。

分数完全可解释：每个命中都会带上命中的理由，前端直接展示。
"""

from __future__ import annotations

from datetime import datetime

from .store import TZ, parse_dt

GRADE_LABELS = {
    "S": "必抢",
    "A": "值得看",
    "B": "一般",
    "C": "观望",
}

WEIGHTS = {"discount": 0.48, "fresh": 0.22, "seller": 0.16, "bonus": 0.14}


def grade_of(score: float) -> str:
    if score >= 82:
        return "S"
    if score >= 68:
        return "A"
    if score >= 52:
        return "B"
    return "C"


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def freshness_score(published_at: str, now: datetime | None = None) -> tuple[float, str]:
    """新鲜度：6 小时内满额，之后线性衰减，超过 4 天基本无分。"""
    now = now or datetime.now(TZ)
    dt = parse_dt(published_at)
    if not dt:
        return 0.45, ""
    minutes = (now - dt).total_seconds() / 60
    if minutes < 0:
        minutes = 0
    hours = minutes / 60
    if hours <= 6:
        return 1.0, f"发布 {int(minutes)} 分钟" if minutes < 60 else f"发布 {int(hours)} 小时"
    score = _clamp(1 - (hours - 6) / 90)
    if hours < 24:
        return score, f"发布 {int(hours)} 小时"
    return score, f"发布 {int(hours / 24)} 天"


def seller_score(item: dict) -> tuple[float, str]:
    raw = item.get("extra") or {}
    value = raw.get("seller_score")
    try:
        num = float(value)
    except (TypeError, ValueError):
        num = -1
    if num < 0:
        return 0.55, ""
    if num > 1:
        num = num / 100
    return _clamp(num), f"卖家信用 {int(num * 100)}"


def bonus_score(item: dict, task: dict) -> tuple[float, list[str]]:
    extra = item.get("extra") or {}
    score = 0.0
    tags: list[str] = []
    if extra.get("free_shipping"):
        score += 0.4
        tags.append("包邮")
    region = (item.get("region") or "").strip()
    if region and task.get("region") and region in task["region"]:
        score += 0.35
        tags.append(f"同城 {region}")
    want = int(item.get("want") or 0)
    if want >= 100:
        score += 0.25
        tags.append(f"想要 {want} 人")
    elif want >= 30:
        score += 0.1
    return _clamp(score), tags


def evaluate(item: dict, task: dict, ref_price: float | None, discount: float,
             now: datetime | None = None) -> tuple[float, str, list[str]]:
    """返回 (score, grade, reasons)。"""
    reasons: list[str] = []
    discount_part = _clamp(discount / 0.45) if ref_price else 0.0
    if ref_price and discount > 0:
        reasons.append(f"比参考价低 {discount * 100:.0f}%")

    fresh_part, fresh_text = freshness_score(item.get("published_at", ""), now)
    if fresh_text:
        reasons.append(fresh_text)

    seller_part, seller_text = seller_score(item)
    if seller_text:
        reasons.append(seller_text)

    bonus_part, bonus_tags = bonus_score(item, task)
    reasons.extend(bonus_tags)

    score = (
        WEIGHTS["discount"] * discount_part
        + WEIGHTS["fresh"] * fresh_part
        + WEIGHTS["seller"] * seller_part
        + WEIGHTS["bonus"] * bonus_part
    ) * 100
    score = round(_clamp(score, 0, 100), 1)
    return score, grade_of(score), reasons
