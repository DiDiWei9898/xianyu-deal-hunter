"""手动导入数据源：不轮询，商品由 /api/import 粘贴或选文件进来。

这是完全合规、且立刻可用的真实数据通道：在闲鱼 App / 网页里复制商品信息，
粘贴到控制台（或导入导出的 JSON / CSV），立刻进入同样的过滤 / 评分 / 去重 / 提醒流程。

支持的输入形态（parse_any 自动识别）：
1. 一行一条：标题 价格 链接
2. 闲鱼分享文案：带 https://m.tb.cn/... 或 goofish 链接的整段文字
3. 表格文本（逗号 / 制表符分隔，带或不带表头，中英文列名均可）
4. JSON（数组、{items:[...]}、或单个对象）
"""

from __future__ import annotations

import json
import re

from .base import RawItem, Source

URL_RE = re.compile(r"https?://[^\s\"'）】\)]+")
NUM_RE = re.compile(r"(?:¥|￥)?\s*(\d[\d,，]*(?:\.\d+)?)\s*(?:元|块)?")
QUOTE_PAIRS = [("“", "”"), ('"', '"'), ("「", "」"), ("『", "』"), ("【", "】")]
NOISE = ("点击链接直接打开", "复制打开闲鱼", "打开闲鱼", "【闲鱼】", "闲鱼", "复制这条消息", "来闲鱼")

# JSON / 表格列名别名：中英文都认
ALIASES = {
    "title": "title", "标题": "title", "商品标题": "title", "商品名": "title", "name": "title",
    "price": "price", "价格": "price", "售价": "price", "现价": "price", "到手价": "price",
    "url": "url", "链接": "url", "商品链接": "url", "link": "url",
    "seller": "seller", "卖家": "seller", "卖家昵称": "seller", "卖家名": "seller",
    "region": "region", "地区": "region", "城市": "region", "location": "region",
    "want": "want", "想要": "want", "想要人数": "want", "浏览量": "want",
    "published_at": "published_at", "发布时间": "published_at", "时间": "published_at",
    "item_id": "item_id", "id": "item_id", "商品id": "item_id", "商品编号": "item_id",
    "market_price": "market_price", "行情价": "market_price", "参考价": "market_price",
    "原价": "market_price", "新机价": "market_price",
}


class ManualSource(Source):
    name = "manual"
    label = "手动粘贴导入（零抓取）"
    polling = False

    def fetch(self, task: dict, limit: int = 30) -> list[RawItem]:
        return []

    def test(self, task: dict) -> tuple[bool, str]:
        return True, "手动导入源无需连通测试，直接在控制台粘贴商品即可"


def _is_url(token: str) -> bool:
    token = token.strip().lower()
    return token.startswith("http://") or token.startswith("https://")


def _price_value(token: str) -> float | None:
    cleaned = (
        token.replace("¥", "").replace("￥", "").replace("元", "")
        .replace(",", "").replace("，", "").strip()
    )
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _looks_like_price(token: str) -> bool:
    """判断一个 token 能否当价格用。

    带货币符号的直接认；纯数字要求 >= 20，避免把「iPhone 14」里的 14 当成价格。
    """
    value = _price_value(token)
    if value is None:
        return False
    if any(symbol in token for symbol in ("¥", "￥", "元")):
        return value > 0
    return value >= 20


def _strip_quotes(text: str) -> str:
    text = text.strip()
    for left, right in QUOTE_PAIRS:
        if text.startswith(left) and text.endswith(right) and len(text) > 2:
            return text[1:-1].strip()
    return text


def _clean_line(line: str) -> str:
    out = line
    for word in NOISE:
        out = out.replace(word, " ")
    return re.sub(r"\s+", " ", out).strip()


def _row(title: str, price: float, url: str, extra: dict | None = None) -> dict:
    row = {"title": _strip_quotes(title), "price": price, "url": url}
    if extra:
        fields = dict(extra)
        market = fields.pop("market_price", None)
        row.update({k: v for k, v in fields.items() if v not in (None, "", 0)})
        if market:
            row.setdefault("extra", {})["market_price"] = market
    return {k: v for k, v in row.items() if v not in (None, "")}


def parse_pasted(text: str) -> list[dict]:
    """逐行解析：标题 价格 [链接]，同时兼容闲鱼分享文案。"""
    rows: list[dict] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue

        # 制表符分隔（从 Excel / 网页表格直接复制常见）
        if "\t" in line:
            parts = [p.strip() for p in line.split("\t") if p.strip()]
            url = ""
            if parts and _is_url(parts[-1]):
                url = parts.pop()
            if len(parts) >= 2:
                price = _price_value(parts[1]) or 0.0
                rows.append(_row(parts[0], price, url))
                continue
            if len(parts) == 1:
                line = parts[0]

        urls = URL_RE.findall(line)
        url = urls[0] if urls else ""
        rest = _clean_line(URL_RE.sub(" ", line))
        if not rest:
            continue

        tokens = rest.split()
        if tokens and _is_url(tokens[-1]):
            url = tokens.pop()

        price_idx = -1
        for idx in range(len(tokens) - 1, -1, -1):
            if _looks_like_price(tokens[idx]):
                price_idx = idx
                break

        # 分享文案里价格常写成「¥ 1,899」两个 token，单独兜一层
        if price_idx <= 0:
            match = NUM_RE.search(rest)
            if match:
                candidate = _price_value(match.group(1))
                if candidate is not None and candidate >= 20:
                    title = _strip_quotes((rest[: match.start()] + " " + rest[match.end():]).strip())
                    rows.append(_row(title, candidate, url))
                    continue

        if price_idx >= 1:
            title = " ".join(tokens[:price_idx])
            price = _price_value(tokens[price_idx]) or 0.0
            rows.append(_row(title, price, url))
        else:
            rows.append(_row(" ".join(tokens), 0.0, url))

    return [r for r in rows if r["title"]]


def _looks_like_table(text: str) -> bool:
    first = text.splitlines()[0] if text.splitlines() else ""
    return ("\t" in first) or (first.count(",") >= 2) or (first.count("，") >= 2)


def parse_table(text: str) -> list[dict]:
    """逗号 / 制表符分隔的表格文本，带表头时按列名映射，不带表头时按 标题,价格,链接，列序。"""
    lines = [line for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return []
    sep = "\t" if "\t" in lines[0] else ("," if "," in lines[0] else "，")
    grid = [[cell.strip().strip('"') for cell in line.split(sep)] for line in lines]
    header = [cell.lower() for cell in grid[0]]
    has_header = any(ALIASES.get(cell) in ("title", "price") for cell in header)

    if has_header:
        cols = {ALIASES.get(name, name): idx for idx, name in enumerate(header)}
        data_rows = grid[1:]
    else:
        cols = {"title": 0, "price": 1, "url": 2}
        data_rows = grid

    def cell(row: list[str], *names: str) -> str:
        for name in names:
            idx = cols.get(name)
            if idx is not None and idx < len(row):
                return row[idx]
        return ""

    rows: list[dict] = []
    for row in data_rows:
        title = cell(row, "title")
        if not title:
            continue
        extra = {}
        market = _price_value(cell(row, "market_price"))
        if market:
            extra["market_price"] = market
        rows.append(_row(
            title,
            _price_value(cell(row, "price")) or 0.0,
            cell(row, "url"),
            {
                **extra,
                "seller": cell(row, "seller"),
                "region": cell(row, "region"),
                "published_at": cell(row, "published_at"),
                "item_id": cell(row, "item_id"),
            },
        ))
    return rows


def normalize_item(item: dict) -> dict:
    """把一条 JSON 对象（中英文键均可）规整成引擎认的字段。"""
    out: dict = {}
    extra: dict = dict(item.get("extra") or {}) if isinstance(item.get("extra"), dict) else {}
    for key, value in item.items():
        mapped = ALIASES.get(str(key).strip().lower(), ALIASES.get(str(key).strip(), key))
        if mapped == "market_price":
            price = _price_value(str(value))
            if price:
                extra["market_price"] = price
            continue
        if mapped == "price":
            out["price"] = _price_value(str(value)) or 0.0
            continue
        if mapped == "want":
            out["want"] = int(_price_value(str(value)) or 0)
            continue
        out[mapped] = value
    if extra:
        out["extra"] = extra
    if not out.get("url"):
        out.pop("url", None)
    return out


def parse_json(text: str) -> list[dict]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        for key in ("items", "data", "list", "results", "result"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            data = [data]
    if not isinstance(data, list):
        return []
    rows = []
    for item in data:
        if isinstance(item, dict):
            normalized = normalize_item(item)
            if str(normalized.get("title") or "").strip():
                rows.append(normalized)
    return rows


def parse_any(text: str) -> list[dict]:
    """自动识别 JSON / 表格 / 逐行文本。"""
    raw = (text or "").strip().lstrip("\ufeff")
    if not raw:
        return []
    if raw[:1] in ("[", "{"):
        rows = parse_json(raw)
        if rows:
            return rows
    if _looks_like_table(raw):
        rows = parse_table(raw)
        if rows:
            return rows
    return parse_pasted(raw)
