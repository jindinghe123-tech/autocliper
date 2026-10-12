"""Entry point: `autostudio` or `python -m autostudio`."""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import webbrowser


def main() -> None:
    parser = argparse.ArgumentParser(prog="autostudio", description="AutoStudio — 开源 AI 视频创作 Agent")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认仅本机 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8765, help="端口（默认 8765）")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    args = parser.parse_args()

    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        os.environ["AUTOSTUDIO_ALLOW_ANY_HOST"] = "1"
        print("⚠️  你正在把 AutoStudio 暴露到非本机地址。它可以读写文件、执行命令，请确保只在可信网络中使用。")

    import uvicorn

    from .config import DATA_DIR, VERSION
    from .server import app

    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{args.port}"
    print(f"\n  AutoStudio v{VERSION}\n  ➜  {url}\n  数据目录：{DATA_DIR}\n  按 Ctrl+C 退出\n")
    if not args.no_browser:
        threading.Thread(target=lambda: (time.sleep(1.2), webbrowser.open(url)), daemon=True).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
