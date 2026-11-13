"""全局配置与默认值。

数据目录可通过环境变量 XDH_DATA_DIR 覆盖，默认放在项目下的 data/。
打包成 exe 后，data/ 落在 exe 同级目录。
"""

from __future__ import annotations

import copy
import os
import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))

if FROZEN:
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent.parent

DATA_DIR = Path(os.environ.get("XDH_DATA_DIR") or (BASE_DIR / "data"))
WEB_DIR = Path(__file__).resolve().parent / "static"
PLUGIN_DIR = BASE_DIR / "plugins"
DB_FILE = DATA_DIR / "hunter.db"

APP_NAME = "闲鱼低价捕捉机"
APP_VERSION = "1.0.0"

DEFAULT_TASK = {
    "name": "未命名监控",
    "keywords": [],
    "exclude": [],
    "source": "mock",
    "source_conf": {},
    "max_price": 1000.0,
    "min_price": 0.0,
    "min_discount": 0.15,
    "ref_price": None,
    "region": "",
    "interval_sec": 30,
    "enabled": True,
}

DEFAULT_SETTINGS = {
    "notify": {
        "min_score": 68,
        "desktop": True,
        "sound": True,
        "webhook": {"enabled": False, "type": "feishu", "url": ""},
        "bark": {"enabled": False, "url": ""},
    },
    "engine": {"max_concurrent": 3, "request_timeout": 15, "fetch_limit": 30},
}


def deep_merge(base: dict, extra: dict) -> dict:
    """把 extra 合并进 base 的副本，嵌套 dict 递归合并。"""
    out = copy.deepcopy(base)
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out
