"""数据源注册表：内置源 + plugins/ 目录下的自定义源。"""

from __future__ import annotations

import importlib.util
import inspect
import sys
import traceback
from pathlib import Path

from ..config import PLUGIN_DIR
from .base import RawItem, Source
from .http_json import HttpJsonSource
from .manual import ManualSource
from .mock import MockSource

_REGISTRY: dict[str, type[Source]] = {}
_LOADED = False


def register(cls: type[Source]) -> None:
    name = getattr(cls, "name", "")
    if not name:
        raise ValueError("数据源必须定义 name")
    _REGISTRY[name] = cls


def _load_plugins() -> None:
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    if not PLUGIN_DIR.exists():
        return
    for path in sorted(PLUGIN_DIR.glob("*.py")):
        if path.name.startswith("_"):
            continue
        module_name = f"xdh_plugin_{path.stem}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, path)
            if not spec or not spec.loader:
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            for _, obj in inspect.getmembers(module, inspect.isclass):
                if issubclass(obj, Source) and obj is not Source and obj.name:
                    register(obj)
        except Exception:  # noqa: BLE001 - 插件失败不能拖垮主程序
            print(f"[plugins] 加载失败 {path.name}:", file=sys.stderr)
            traceback.print_exc()


def _bootstrap() -> None:
    if _REGISTRY:
        return
    for cls in (MockSource, HttpJsonSource, ManualSource):
        register(cls)


def available() -> list[dict]:
    _bootstrap()
    _load_plugins()
    return [
        {"name": cls.name, "label": cls.label, "polling": bool(cls.polling)}
        for cls in _REGISTRY.values()
    ]


def get_source(name: str, conf: dict | None = None) -> Source:
    _bootstrap()
    _load_plugins()
    cls = _REGISTRY.get(name or "mock")
    if cls is None:
        raise ValueError(f"未知数据源 {name}，可用：{', '.join(_REGISTRY)}")
    return cls(conf or {})


__all__ = ["RawItem", "Source", "available", "get_source", "register"]
