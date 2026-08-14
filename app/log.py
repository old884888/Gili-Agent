"""日志系统（零依赖，开发者可见）。

- 控制台：INFO 级别（桌面/浏览器模式都能看到）
- 文件：data/logs/gili.log 滚动（1MB × 5 份，UTF-8），DEBUG 级别
- uvicorn 错误日志并入文件（access 噪音不进）

约定（docs/11-日志系统设计.md）：
  1. 绝不记录 API Key / 密钥等敏感信息（红线，靠约定 + 代码自查）
  2. 后台任务（create_task）异常用 log.exception（自动带堆栈）
  3. 模块内用法：log = logging.getLogger(__name__)
  4. 用户可见的启动进度仍用 print（desktop.py）

读取：data/logs/gili.log（最新）→ gili.log.1（上一轮，依次类推）。
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
LOG_DIR = DATA_DIR / "logs"
LOG_FILE = LOG_DIR / "gili.log"

_FMT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATEFMT = "%m-%d %H:%M:%S"

_setup_done = False


def setup_logging() -> None:
    """幂等初始化：控制台 INFO + 滚动文件 DEBUG；uvicorn 错误并入文件。"""
    global _setup_done
    if _setup_done:
        return
    _setup_done = True
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        root = logging.getLogger()
        root.setLevel(logging.DEBUG)

        console = logging.StreamHandler(sys.stdout)
        console.setLevel(logging.INFO)
        console.setFormatter(logging.Formatter(_FMT, _DATEFMT))

        fileh = RotatingFileHandler(
            LOG_FILE, maxBytes=1_000_000, backupCount=5, encoding="utf-8"
        )
        fileh.setLevel(logging.DEBUG)
        fileh.setFormatter(logging.Formatter(_FMT, _DATEFMT))

        root.addHandler(console)
        root.addHandler(fileh)

        # uvicorn 的错误（启动失败/未捕获异常）进文件；access 日志保持只在控制台
        err = logging.getLogger("uvicorn.error")
        err.addHandler(fileh)
        err.propagate = False
        logging.getLogger("uvicorn").propagate = False
    except OSError:
        # 日志目录不可写时不阻塞应用启动（控制台 print 仍可用）
        pass
