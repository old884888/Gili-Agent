# -*- coding: utf-8 -*-
"""v0.16-0.18 学习闭环核心路径防回归（全部 FakeProvider，零 API 成本）。"""
import asyncio
import json
import pathlib

from app import db, learning_skill as ls
from conftest import FakeProvider, FallbackProvider


def test_dynamic_confirm_in_one_question(test_env):
    ls.clear("k1")
    st = ls.start("k1")
    p = FakeProvider([{
        "action": "confirm",
        "updates": {"subject": "Python爬虫", "level_from": "零基础", "level_to": "进阶",
                    "goal": "两个月内能独立爬网站"},
        "assume": ["style"],
        "plan": {},
    }])
    st = asyncio.run(ls.answer("零基础想两个月学会Python爬虫，能独立爬网站", "k1", p))
    assert st["phase"] == "confirm"
    assert len(st["history"]) == 1
    assert st["plan"]["goal"]
    assert "style" in st["assumed"]


def test_onboard_persistence(test_env):
    ls.clear("k2")
    ls.start("k2")
    # 模拟重启：内存清空，DB 恢复
    ls._onboard.pop("k2", None)
    st = ls.start("k2")
    assert st["phase"] == "ask"


def test_stop_words(test_env):
    ls.clear("k3")
    st = ls.start("k3")
    p = FakeProvider([{"action": "ask", "updates": {}, "assume": [], "question": "水平？",
                       "options": ["零基础"], "input": False, "placeholder": ""}])
    st = asyncio.run(ls.answer("学英语", "k3", p))
    st = asyncio.run(ls.answer("就这样吧", "k3", p))
    assert st["phase"] == "confirm"
    assert set(st["assumed"]) == {"level_from", "level_to", "style"}


def test_fallback_skip(test_env):
    ls.clear("k4")
    st = ls.start("k4")
    fb = FallbackProvider()
    st = asyncio.run(ls.answer("写作", "k4", fb))
    st = asyncio.run(ls.answer("⏭ 跳过（用默认）", "k4", fb))
    assert "level_from" in st["assumed"]
    st = asyncio.run(ls.answer("⏭ 跳过（用默认）", "k4", fb))
    st = asyncio.run(ls.answer("⏭ 跳过（用默认）", "k4", fb))
    assert st["phase"] == "confirm"
    assert set(st["assumed"]) == {"level_from", "level_to", "style"}


def test_edit_plan(test_env):
    ls.clear("k5")
    st = ls.start("k5")
    st["phase"] = "confirm"
    st["plan"] = ls._normalize_plan({"subject": "X", "level_from": "入门", "style": "详细（章节拆细）"}, st)
    ls._save("k5", st)
    st = ls.edit_plan("k5", "style", "精炼（结构清晰）")
    assert st["plan"]["style"].startswith("精炼")
    assert "style" not in st["assumed"]


def test_archive_to_kb(test_env):
    out = ls.archive_to_kb("心理学TEST", "锚定效应是指…")
    assert out["status"] == "ok"
    f = ls.KB_DIR / "心理学TEST.md"
    assert f.exists() and "锚定效应" in f.read_text(encoding="utf-8")
    out = ls.archive_to_kb("心理学TEST", "")
    assert out["status"] == "error"


def test_sidebar_park(test_env):
    conv = db.create_conversation("P3")
    cid = conv["id"]
    proj = db.create_learning_project("TEST", "零基础", "入门", "精炼（结构清晰）", cid)
    ls._pending_updates[cid] = {"kind": "sidebar", "concept": "Docker", "brief": "容器",
                                "source_ref": "对话", "project_id": proj["id"]}
    out = ls.handle_update_answer(cid, "📌 挂起到疑问本")
    assert out["status"] == "parked"
    qs = db.list_learning_questions(proj["id"])
    assert any(q["concept"] == "Docker" for q in qs)


def test_organize_with_history(test_env):
    conv = db.create_conversation("P4")
    cid = conv["id"]
    proj = db.create_learning_project("TEST", "零基础", "入门", "精炼（结构清晰）", cid)
    pid = proj["id"]
    pdir = ls.NOTES_DIR / pid
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "笔记.md").write_text("- 变量\n- 函数", encoding="utf-8")
    db.add_note_meta(pid, "笔记.md", "笔记")

    p1 = FakeProvider(stream_text="# 整理稿 v1\n\n## 待办与疑问\n- 待查")
    asyncio.run(ls.organize_project_notes(pid, p1))
    assert db.get_note_version(pid, "笔记整理.md") == 1
    p2 = FakeProvider(stream_text="# 整理稿 v2")
    asyncio.run(ls.organize_project_notes(pid, p2))
    hist = sorted((pdir / ".history").glob("笔记整理.v*.md"))
    assert len(hist) == 1 and "v1" in hist[0].name
    ls.rollback_organized(pid)
    assert "整理稿 v1" in (pdir / "笔记整理.md").read_text(encoding="utf-8")
