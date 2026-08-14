"""桌面模式：pywebview 原生窗口 + 后台 FastAPI。

用法：
    python desktop.py
    或双击「启动桌面版.bat」

说明：
- 界面仍是网页技术渲染（WebView2 内核），但显示在原生窗口里，
  有自己的任务栏图标，不占用浏览器标签页。
- 后端在后台线程启动，窗口关闭即退出。
- 浏览器模式（uvicorn app.main:app）依然可用，两者并存。

数据位置：所有数据存项目目录（SQLite / notes），整个文件夹拷走即"绿色版"。
后续可用 PyInstaller 打成独立可执行文件/便携文件夹。
"""
from __future__ import annotations

import socket
import threading
import time

import uvicorn


def _free_port() -> int:
    """找一个空闲端口，避免和别的程序冲突。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _enable_dpi_awareness() -> float:
    """声明高 DPI 感知（修复整体模糊），返回显示缩放系数。

    不声明时 Windows 会把整个窗口位图拉伸（糊）；声明后按物理像素
    渲染，窗口尺寸乘上缩放系数保持视觉大小不变。
    """
    scale = 1.0
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
        try:
            scale = ctypes.windll.shcore.GetScaleFactorForDevice(0) / 100.0
        except Exception:
            scale = 1.0
    except Exception:
        pass
    return scale


def _start_server(port: int) -> uvicorn.Server:
    config = uvicorn.Config(
        "app.main:app", host="127.0.0.1", port=port, log_level="warning"
    )
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 30
    while not server.started:  # 等后端就绪再开窗口，避免白屏
        if time.time() > deadline:
            raise RuntimeError("后端启动超时（30 秒）——请把控制台里的报错发给我")
        time.sleep(0.05)
    return server


def main() -> None:
    scale = _enable_dpi_awareness()  # 必须先于任何窗口创建
    try:
        import webview
    except ImportError:
        print("缺少 pywebview：pip install pywebview")
        print("或继续用浏览器模式：uvicorn app.main:app --reload")
        raise SystemExit(1)

    port = _free_port()
    print(f"[1/3] 启动后端… (http://127.0.0.1:{port})")
    server = _start_server(port)
    print("[2/3] 后端就绪，打开窗口…")
    url = f"http://127.0.0.1:{port}"

    try:
        webview.create_window(
            "Gili Agent",
            url,
            width=int(1280 * scale),
            height=int(820 * scale),
            min_size=(int(960 * scale), int(640 * scale)),
            background_color="#0f1115",
        )
        print("[3/3] 窗口已创建，等待关闭…")
        webview.start()
        print("窗口已关闭，再见！")
    except Exception as e:  # 常见原因：缺少 WebView2 运行时
        print(f"桌面窗口启动失败（{e}）")
        print(f"1) 安装 Microsoft Edge WebView2 Runtime 后重试；")
        print(f"2) 或直接用浏览器打开：{url}（服务仍在运行）")
        server.should_exit = True
        raise SystemExit(1)
    finally:
        server.should_exit = True


if __name__ == "__main__":
    main()
