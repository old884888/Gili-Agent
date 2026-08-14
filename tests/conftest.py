# -*- coding: utf-8 -*-
"""测试基础设施：隔离数据库/目录 + FakeProvider。

所有测试都在 tests/.tmp 下的独立 SQLite 与 notes 目录运行，
绝不触碰真实 data/ 数据；fixture 自管清理（绕开 pytest tmp 机制的
扩展路径清理，兼容沙箱环境）。
"""
import json
import os
import pathlib
import shutil
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

_TMP = pathlib.Path(__file__).resolve().parent / ".tmp"


class FakeProvider:
    """模拟 DeepSeek：complete 按序返回预设 JSON；stream_chat 返回预设文本。"""

    name = "deepseek"

    def __init__(self, responses=None, stream_text=""):
        self.responses = list(responses or [])
        self.stream_text = stream_text
        self.last_messages = None
        self.last_stream_messages = None

    async def complete(self, messages, params=None, tools=None):
        self.last_messages = messages
        if not self.responses:
            return {"content": json.dumps({"action": "confirm", "plan": {}}, ensure_ascii=False),
                    "tool_calls": [], "usage": {}, "reasoning_content": ""}
        return {"content": json.dumps(self.responses.pop(0), ensure_ascii=False),
                "tool_calls": [], "usage": {}, "reasoning_content": ""}

    async def stream_chat(self, messages, params=None):
        self.last_stream_messages = messages
        yield {"kind": "content", "text": self.stream_text}


class FallbackProvider:
    name = "mock"


@pytest.fixture
def test_env(monkeypatch):
    """隔离环境：tests/.tmp 下独立 DB + notes 目录 + 清空内存态。"""
    from app import db, knowledge, learning_skill, search

    _TMP.mkdir(parents=True, exist_ok=True)
    current = os.environ.get("PYTEST_CURRENT_TEST", "test")
    tag = current.split("::")[-1].replace("/", "_").replace("[", "_").replace("]", "_")
    db_path = _TMP / f"{tag}.db"

    monkeypatch.setattr(db, "DB_PATH", db_path)
    monkeypatch.setattr(db, "_initialized", False)
    db.init()

    notes = _TMP / f"{tag}-notes"
    monkeypatch.setattr(learning_skill, "NOTES_DIR", notes)
    monkeypatch.setattr(knowledge, "NOTES_DIR", notes)

    learning_skill._onboard.clear()
    learning_skill._pending_updates.clear()
    learning_skill._user_turns.clear()
    learning_skill._last_proposal_turn.clear()
    learning_skill._choose_chapter.clear()

    yield _TMP

    db._initialized = False
    # 自管清理（普通路径，避免 pytest tmp 的扩展路径问题）
    for p in (db_path, pathlib.Path(str(db_path) + "-wal"), pathlib.Path(str(db_path) + "-shm")):
        try:
            if p.exists():
                p.unlink()
        except OSError:
            pass
    try:
        shutil.rmtree(notes, ignore_errors=True)
    except OSError:
        pass
