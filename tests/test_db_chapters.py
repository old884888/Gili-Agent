# -*- coding: utf-8 -*-
"""v0.17 章节增删改/移动 + 级联删除 + 引导状态持久化（DB 层）。"""
import json

from app import db, learning_skill as ls


def test_chapter_crud_and_move(test_env):
    conv = db.create_conversation("C")
    proj = db.create_learning_project("T", "零基础", "入门", "精炼（结构清晰）", conv["id"])
    pid = proj["id"]
    for i, t in enumerate(["甲", "乙", "丙"], 1):
        db.add_learning_chapter(pid, t, "目标", i)

    db.insert_learning_chapter(pid, "中插", "目标", 2)
    chs = db.list_learning_chapters(pid)
    assert [c["title"] for c in chs] == ["甲", "中插", "乙", "丙"]

    db.update_learning_chapter(chs[0]["id"], title="甲改", objective="新目标")
    assert db.list_learning_chapters(pid)[0]["title"] == "甲改"

    db.move_learning_chapter(chs[0]["id"], "down")
    assert [c["title"] for c in db.list_learning_chapters(pid)] == ["中插", "甲改", "乙", "丙"]

    chs2 = db.list_learning_chapters(pid)
    db.delete_learning_chapter(chs2[0]["id"])  # 删除当前第一位「中插」
    chs = db.list_learning_chapters(pid)
    assert [c["order_no"] for c in chs] == [1, 2, 3]
    assert [c["title"] for c in chs] == ["甲改", "乙", "丙"]


def test_cascade_delete_conversation(test_env):
    conv = db.create_conversation("C2")
    cid = conv["id"]
    db.add_message(cid, "user", "hi")
    db.add_request_log("deepseek-v4-flash", 100, 80, 20, 50, 1000, 0.001, False, conversation_id=cid)
    db.delete_conversation(cid)
    assert db.get_conversation(cid) is None
    assert not db.get_messages(cid)
    assert not db.list_request_logs(conversation_id=cid)


def test_onboard_state_roundtrip(test_env):
    ls.clear("k9")
    st = ls.start("k9")
    st["history"].append({"question": "q", "answer": "a"})
    ls._save("k9", st)
    ls._onboard.pop("k9", None)
    restored = db.load_onboard_state("k9")
    assert restored and restored["history"][0]["answer"] == "a"
    ls.clear("k9")
    assert db.load_onboard_state("k9") is None
