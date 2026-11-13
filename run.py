"""启动入口：python run.py

默认只监听 127.0.0.1，启动后自动打开浏览器控制台。
"""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser

from app.config import APP_NAME, APP_VERSION, DATA_DIR, DB_FILE
from app.engine import Engine
from app.notifier import Notifier
from app.server import Context, serve
from app.store import Store


def _pin_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def build_context(db_path=None):
    store = Store(db_path or DB_FILE)
    notifier = Notifier(store)
    engine = Engine(store, notifier)
    return Context(store, engine, notifier)


def main(argv: list[str] | None = None) -> int:
    _pin_utf8()
    parser = argparse.ArgumentParser(description=f"{APP_NAME} v{APP_VERSION}")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认仅本机")
    parser.add_argument("--port", type=int, default=8765, help="监听端口，默认 8765")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--db", default=None, help="指定数据库文件路径")
    args = parser.parse_args(argv)

    ctx = build_context(args.db)
    server = serve(args.host, args.port, ctx)
    port = server.server_address[1]
    url = f"http://{args.host}:{port}/"

    ctx.engine.start()

    print("-" * 58)
    print(f"  {APP_NAME}  v{APP_VERSION}")
    print(f"  控制台：{url}")
    print(f"  数据目录：{DATA_DIR if not args.db else args.db}")
    print(f"  数据库：{DB_FILE if not args.db else args.db}")
    print("  按 Ctrl+C 停止")
    print("-" * 58)

    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever(poll_interval=0.4)
    except KeyboardInterrupt:
        print("\n正在停止……")
    finally:
        ctx.engine.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
