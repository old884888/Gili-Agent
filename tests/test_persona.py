# -*- coding: utf-8 -*-
"""人格画像（docs/13）：开关/轮数/分析引擎/降级/级联。"""
import asyncio
import json

from app import db, persona
from conftest import FakeProvider

GOOD = {
    "dims": {"openness": 72, "conscientiousness": 55, "extraversion": 40, "agreeableness": 63,
             "stability": 58, "verbosity": 66, "questioning": 47, "humor": 30},
    "evidence": [{"dim": "openness", "quote": "我想试试新的框架"}, {"dim": "humor", "quote": "哈哈"}],
    "summary": "爱探索、表达详尽的务实派。",
}


def test_toggle_on_off(test_env):
    conv = db.create_conversation("P1")
    cid = conv["id"]
    v = persona.toggle(cid, True)
    assert v["enabled"] and v["turns"] == 0
    # 关闭即删
    db.add_persona_snapshot(cid, 20, "{}", "[]", "", "m")
    assert db.list_persona_snapshots(cid)
    v = persona.toggle(cid, False)
    assert not v["enabled"]
    assert not db.get_persona_state(cid)
    assert not db.list_persona_snapshots(cid)


def test_count_turn_only_when_enabled(test_env):
    conv = db.create_conversation("P2")
    cid = conv["id"]
    assert persona.count_turn(cid) == 0  # 未开启不计数
    persona.toggle(cid, True)
    assert persona.count_turn(cid) == 1
    assert persona.count_turn(cid) == 2
    assert persona.should_analyze(cid) is False
    for _ in range(18):
        persona.count_turn(cid)
    assert persona.should_analyze(cid) is True  # 20 轮触发


def test_analyze_good(test_env):
    conv = db.create_conversation("P3")
    cid = conv["id"]
    for i in range(3):
        db.add_message(cid, "user", f"用户消息{i}")
        db.add_message(cid, "assistant", f"回复{i}")
    persona.toggle(cid, True)
    for _ in range(20):
        persona.count_turn(cid)
    settings = {"api_key": "", "persona_model": "", "persona_api_key": "", "persona_base_url": ""}
    p = FakeProvider([GOOD])
    snap = asyncio.run(persona.analyze(cid, settings, _providers=[(p, "test-model")]))
    assert snap and snap["dims"]["openness"] == 72
    assert snap["summary"] == GOOD["summary"]
    snaps = db.list_persona_snapshots(cid)
    assert len(snaps) == 1 and snaps[0]["turn_no"] == 20
    # 证据落库
    ev = json.loads(snaps[0]["evidence"])
    assert any(e["dim"] == "openness" for e in ev)


def test_analyze_bad_json_no_snapshot(test_env):
    conv = db.create_conversation("P4")
    cid = conv["id"]
    db.add_message(cid, "user", "随便聊聊")
    persona.toggle(cid, True)
    for _ in range(20):
        persona.count_turn(cid)
    settings = {"api_key": "", "persona_model": "", "persona_api_key": "", "persona_base_url": ""}
    p = FakeProvider(["这不是JSON"])
    snap = asyncio.run(persona.analyze(cid, settings, _providers=[(p, "test-model")]))
    assert snap is None
    assert not db.list_persona_snapshots(cid)


def test_analyze_missing_dims_default_50(test_env):
    conv = db.create_conversation("P5")
    cid = conv["id"]
    db.add_message(cid, "user", "hi")
    persona.toggle(cid, True)
    for _ in range(20):
        persona.count_turn(cid)
    settings = {"api_key": "", "persona_model": "", "persona_api_key": "", "persona_base_url": ""}
    p = FakeProvider([{"dims": {"openness": 999, "humor": "abc"}, "evidence": [], "summary": "x"}])
    snap = asyncio.run(persona.analyze(cid, settings, _providers=[(p, "test-model")]))
    assert snap["dims"]["openness"] == 100  # 截断
    assert snap["dims"]["humor"] == 50     # 非法 → 中性
    assert snap["dims"]["agreeableness"] == 50  # 缺维 → 中性


def test_prior_in_prompt(test_env):
    conv = db.create_conversation("P6")
    cid = conv["id"]
    db.add_message(cid, "user", "聊点东西")
    persona.toggle(cid, True)
    for _ in range(20):
        persona.count_turn(cid)
    db.add_persona_snapshot(cid, 20, json.dumps(GOOD["dims"]), "[]", "旧摘要", "m")
    settings = {"api_key": "", "persona_model": "", "persona_api_key": "", "persona_base_url": ""}
    p = FakeProvider([GOOD])
    asyncio.run(persona.analyze(cid, settings, _providers=[(p, "test-model")]))
    assert p.last_messages is not None
    user_text = p.last_messages[-1]["content"]
    assert "先验" in user_text and "旧摘要" in user_text


def test_cascade_delete(test_env):
    conv = db.create_conversation("P7")
    cid = conv["id"]
    persona.toggle(cid, True)
    db.add_persona_snapshot(cid, 20, "{}", "[]", "", "m")
    db.delete_conversation(cid)
    assert not db.get_persona_state(cid)
    assert not db.list_persona_snapshots(cid)


def test_provider_chain(test_env):
    # 独立 key → 两个 provider（独立优先）；无独立 key → 主 provider
    out = persona._make_providers({"api_key": "main", "persona_api_key": "pkey",
                                   "persona_base_url": "", "persona_model": "m"})
    assert len(out) == 2
    out2 = persona._make_providers({"api_key": "main", "persona_api_key": "",
                                    "persona_base_url": "", "persona_model": "m"})
    assert len(out2) == 1
