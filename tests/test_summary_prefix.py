# -*- coding: utf-8 -*-
"""摘要请求复用 warm 前缀（DSH prefix-extension，v0.19 二批防回归）。"""
import asyncio

from app import db, memory
from conftest import FakeProvider


def test_summary_prefix_replay(test_env):
    conv = db.create_conversation("P")
    cid = conv["id"]
    for i in range(3):
        db.add_message(cid, "user", f"问题{i}")
        db.add_message(cid, "assistant", f"回答{i}")

    routed = [
        {"role": "system", "content": "人设+技能"},
        {"role": "user", "content": "问题0"},
        {"role": "assistant", "content": "回答0"},
        {"role": "system", "content": "（环境信息）时间"},
    ]
    prefix = (routed, "deepseek-v4-flash")
    p = FakeProvider(stream_text="这是一份测试摘要。")

    text = asyncio.run(memory.summarize_conversation(cid, p, keep_window=10, force=True, prefix=prefix))
    assert text == "这是一份测试摘要。"
    msgs = p.last_stream_messages
    # 前缀逐字节一致 + 指令尾部化 + 同 model
    assert msgs[: len(routed)] == routed
    assert msgs[-1]["role"] == "user" and "压缩" in msgs[-1]["content"]


def test_summary_fallback_without_prefix(test_env):
    conv = db.create_conversation("P2")
    cid = conv["id"]
    for i in range(3):
        db.add_message(cid, "user", f"问题{i}")
        db.add_message(cid, "assistant", f"回答{i}")
    p = FakeProvider(stream_text="摘要")
    asyncio.run(memory.summarize_conversation(cid, p, keep_window=10, force=True))
    msgs = p.last_stream_messages
    assert msgs[0]["role"] == "system" and "记忆整理器" in msgs[0]["content"]
    # 落库：摘要 + summarized 标记
    assert db.get_summary_item(cid) is not None
