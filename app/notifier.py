"""推送通道：站内、桌面通知、提示音、飞书/钉钉/企业微信 Webhook、Bark。

所有通道的成败都会写进 alerts 表，控制台里能看到每一次推送的结果。
"""

from __future__ import annotations

import json
import platform
import urllib.error
import urllib.request

from .scoring import GRADE_LABELS


def _money(value) -> str:
    try:
        num = float(value or 0)
    except (TypeError, ValueError):
        return "0"
    return f"{num:.0f}" if num >= 100 else f"{num:.1f}"


class Notifier:
    def __init__(self, store) -> None:
        self.store = store

    def settings(self) -> dict:
        return (self.store.get_settings().get("notify") or {})

    def compose(self, hit: dict) -> tuple[str, str]:
        """返回 (标题, 正文) 纯文本。"""
        discount = float(hit.get("discount") or 0)
        grade = hit.get("grade", "C")
        label = GRADE_LABELS.get(grade, "")
        title = f"捡漏提醒 · {grade}级{label} · {hit.get('task_name', '')}"
        lines = [
            f"商品：{hit.get('title', '')}",
            f"现价 ¥{_money(hit.get('price'))}",
        ]
        if hit.get("ref_price"):
            lines.append(f"参考 ¥{_money(hit.get('ref_price'))}（低 {discount * 100:.0f}%）")
        meta = []
        if hit.get("seller"):
            meta.append(f"卖家 {hit['seller']}")
        if hit.get("region"):
            meta.append(str(hit["region"]))
        if hit.get("published_at"):
            meta.append(f"发布 {str(hit['published_at'])[:16]}")
        if meta:
            lines.append(" | ".join(meta))
        if hit.get("url"):
            lines.append(f"链接 {hit['url']}")
        lines.append(f"评分 {hit.get('score', 0)}")
        return title, "\n".join(lines)

    def notify(self, hit: dict) -> list[dict]:
        conf = self.settings()
        title, body = self.compose(hit)
        results: list[dict] = []

        if conf.get("desktop", True):
            results.append(self._desktop(title, body))
        if conf.get("sound", True):
            results.append(self._sound())

        hook = conf.get("webhook") or {}
        if hook.get("enabled") and hook.get("url"):
            results.append(self._webhook(hook, title, body, hit))

        bark = conf.get("bark") or {}
        if bark.get("enabled") and bark.get("url"):
            results.append(self._bark(bark, title, body, hit))

        for item in results:
            self.store.log_alert(
                hit.get("task_id", 0),
                hit.get("id", 0),
                hit.get("item_id", ""),
                hit.get("title", ""),
                float(hit.get("price") or 0),
                item["channel"],
                item["ok"],
                item.get("error", ""),
            )
        return results

    # ---------------------------------------------------------------- 通道
    def _desktop(self, title: str, body: str) -> dict:
        try:
            if platform.system() == "Windows":
                try:
                    from winotify import Notification  # 可选依赖
                except ImportError:
                    return {"channel": "desktop", "ok": False,
                            "error": "未安装 winotify（可选：pip install winotify）"}
                toast = Notification(app_id="闲鱼低价捕捉机", title=title, msg=body[:240])
                last = body.rsplit(" ", 1)[-1]
                if last.startswith("http"):
                    toast.add_actions(label="打开商品", launch=last)
                toast.show()
                return {"channel": "desktop", "ok": True}
            if platform.system() == "Darwin":
                import subprocess

                script = (
                    f"display notification {json.dumps(body[:200])} "
                    f"with title {json.dumps(title)}"
                )
                subprocess.run(["osascript", "-e", script], check=False, timeout=10)
                return {"channel": "desktop", "ok": True}
            return {"channel": "desktop", "ok": False, "error": "当前系统不支持桌面通知"}
        except Exception as exc:  # noqa: BLE001
            return {"channel": "desktop", "ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def _sound(self) -> dict:
        try:
            if platform.system() == "Windows":
                import winsound

                winsound.Beep(880, 120)
                winsound.Beep(1240, 160)
                return {"channel": "sound", "ok": True}
            return {"channel": "sound", "ok": False, "error": "仅 Windows 支持提示音"}
        except Exception as exc:  # noqa: BLE001
            return {"channel": "sound", "ok": False, "error": str(exc)}

    def _webhook(self, conf: dict, title: str, body: str, hit: dict) -> dict:
        url = str(conf.get("url") or "").strip()
        kind = str(conf.get("type") or "feishu").lower()
        if kind == "feishu":
            payload = {"msg_type": "text", "content": {"text": f"{title}\n{body}"}}
        elif kind in ("dingtalk", "dingding"):
            payload = {
                "msgtype": "markdown",
                "markdown": {"title": title,
                             "text": f"### {title}\n\n{body.replace(chr(10), chr(10) + chr(10))}"},
            }
        elif kind in ("wecom", "qywx", "wechat"):
            payload = {"msgtype": "text", "text": {"content": f"{title}\n{body}"}}
        else:
            payload = {"title": title, "text": body, "hit": hit}
        return self._post(url, payload, "webhook")

    def _bark(self, conf: dict, title: str, body: str, hit: dict) -> dict:
        url = str(conf.get("url") or "").strip().rstrip("/")
        payload = {
            "title": title,
            "body": body,
            "url": hit.get("url") or "",
            "group": "闲鱼捡漏",
            "level": "active",
        }
        return self._post(url, payload, "bark")

    @staticmethod
    def _post(url: str, payload: dict, channel: str) -> dict:
        try:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            request = urllib.request.Request(
                url, data=data, headers={"Content-Type": "application/json"}, method="POST"
            )
            with urllib.request.urlopen(request, timeout=8) as resp:
                text = resp.read().decode("utf-8", "replace")[:200]
            if '"code":0' in text or '"errcode":0' in text or '"StatusCode":0' in text:
                return {"channel": channel, "ok": True}
            if text and ("error" in text.lower() or "invalid" in text.lower()):
                return {"channel": channel, "ok": False, "error": text}
            return {"channel": channel, "ok": True}
        except urllib.error.HTTPError as exc:
            return {"channel": channel, "ok": False, "error": f"HTTP {exc.code}"}
        except urllib.error.URLError as exc:
            return {"channel": channel, "ok": False, "error": f"网络错误：{exc.reason}"}
        except Exception as exc:  # noqa: BLE001
            return {"channel": channel, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
