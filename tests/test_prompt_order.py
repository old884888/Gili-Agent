# -*- coding: utf-8 -*-
"""v0.19 线序防回归：动态内容必须在当前消息之后的绝对尾部（append-only 原则）。"""
from app import prompt_builder as pb


def test_tail_order_and_prefix_chain():
    soul = "人设ABC"
    frags = ["技能片段"]
    summary = "摘要XYZ"
    memories = [{"layer": "longterm", "content": "事实1"}]

    m1, fp1 = pb.build_prompt(
        soul=soul, skill_fragments=frags, summary=summary, memories=memories,
        history=[], current={"role": "user", "content": "用户第1问"},
        volatile_text="（当前时间：X）",
    )
    # 首轮：稳定 → 低频 → current → volatile（绝对尾部）
    assert m1[0]["role"] == "system" and "人设ABC" in m1[0]["content"]
    assert m1[1]["role"] == "system" and "摘要XYZ" in m1[1]["content"]
    assert m1[-2]["role"] == "user"
    assert "（当前时间" in m1[-1]["content"]

    hist = [
        {"role": "user", "content": "用户第1问"},
        {"role": "assistant", "content": "助手回复1"},
    ]
    m2, fp2 = pb.build_prompt(
        soul=soul, skill_fragments=frags, summary=summary, memories=memories,
        history=hist, current={"role": "user", "content": "用户第2问"},
        volatile_text="（当前时间：Y）",
    )
    # 第二轮前缀 = 首轮的稳定+低频+current（前缀链成立）
    assert m2[0]["content"] == m1[0]["content"]
    assert m2[1]["content"] == m1[1]["content"]
    assert m2[2]["role"] == "user" and m2[2]["content"] == "用户第1问"
    assert m2[3]["role"] == "assistant" and m2[3]["content"] == "助手回复1"
    assert m2[4]["role"] == "user" and m2[4]["content"] == "用户第2问"
    assert "（当前时间" in m2[5]["content"]


def test_fingerprint_stable():
    soul = "人设"
    a = pb.build_prompt(soul=soul, skill_fragments=[], summary="", memories=[],
                        history=[], current={"role": "user", "content": "x"})[1]
    b = pb.build_prompt(soul=soul, skill_fragments=[], summary="", memories=[],
                        history=[], current={"role": "user", "content": "y"})[1]
    # 波动区（current）变化不影响指纹（前缀不变）
    assert a == b
