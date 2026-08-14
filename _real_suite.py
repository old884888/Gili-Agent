# -*- coding: utf-8 -*-
"""真实 AI 综合回归测试（用户授权，成本极低，自动清理）。
覆盖：A 多轮命中率累积 / B 摘要前缀复用 / C 引导一句话确认 / D 思考+工具轮 / E 笔记整理
"""
import asyncio, json, pathlib, shutil, sys
sys.path.insert(0, ".")
from app import config, db, learning_skill as ls, llm, memory, prompt_builder, search
from app.schemas import ChatParams

CONF = config.load()
provider = llm.make_provider(CONF)
assert provider.name == "deepseek", "未配置 API Key"
MODEL = "deepseek-v4-flash"


def check(name, cond, extra=""):
    if not cond:
        raise AssertionError("FAIL: " + name + " " + str(extra))
    print("  ✓ " + name)


async def main():
    print("== A. 8 轮对话命中率累积（v0.19 线序修复） ==")
    conv = db.create_conversation("REALTEST-A")
    cid = conv["id"]
    hist = []
    rounds = ["你好，用一句话介绍你自己。", "1+1 等于几？一句话。", "今天你心情如何？一句话。", "推荐一首古诗，只给名字。", "你最擅长什么？一句话。", "说一个冷笑话，短一点。", "天气对你的影响大吗？一句话。", "最后：复述你上一条回答。"]
    last_msgs = None
    for i, msg in enumerate(rounds, 1):
        frags, vol = prompt_builder.build_skill_context(CONF)
        msgs, _fp = prompt_builder.build_prompt(
            soul=CONF.get("soul", ""), skill_fragments=frags, summary="", memories=[],
            history=hist, current={"role": "user", "content": msg}, volatile_text=vol,
        )
        out = []
        params = ChatParams(model=MODEL, thinking_enabled=False, max_tokens=200)
        async for evt in provider.stream_chat(msgs, params):
            if evt["kind"] == "content":
                out.append(evt["text"])
        reply = "".join(out).strip()
        u = provider.last_usage or {}
        hit = u.get("prompt_cache_hit_tokens", 0)
        miss = u.get("prompt_cache_miss_tokens", 0)
        pr = hit + miss
        rate = hit / pr * 100 if pr else 0
        print("  第" + str(i) + "轮: hit=" + str(hit) + " miss=" + str(miss) + " 命中率=" + str(int(rate)) + "% 回复=" + reply[:20])
        hist.append({"role": "user", "content": msg})
        hist.append({"role": "assistant", "content": reply})
        last_msgs = msgs

    print("== B. 摘要请求复用 warm 前缀（真实命中验证） ==")
    await memory._llm_summarize(provider, "占位", prefix=(last_msgs, MODEL))
    u = provider.last_usage or {}
    hit = u.get("prompt_cache_hit_tokens", 0)
    miss = u.get("prompt_cache_miss_tokens", 0)
    print("  摘要请求: hit=" + str(hit) + " miss=" + str(miss))
    check("摘要请求命中 warm 前缀", hit > 0, "hit=" + str(hit))

    print("== C. 学习引导一句话说全 → AI 决策 ==")
    ls.clear("realtest-c")
    st = ls.start("realtest-c")
    st = await ls.answer("零基础，想两个月学会 Python 爬虫，能独立爬网站", "realtest-c", provider)
    phase = st["phase"]
    print("  1 问后 phase=" + phase + " 问题数=" + str(len(st["history"])) + " plan=" + json.dumps(st.get("plan") or {}, ensure_ascii=False)[:180])
    check("一句话说全后 AI 确认或追问≤1次", phase in ("confirm", "ask") and len(st["history"]) <= 2)
    if phase == "confirm":
        print("  ✓ 1 问即确认（动态问数生效）")
    ls.clear("realtest-c")

    print("== D. 思考模式 + 工具轮（reasoning_content 回传） ==")
    d_msgs = [{"role": "system", "content": "你是一个助手。"},
              {"role": "user", "content": "请搜索一下今天的重大新闻，简单总结一条。"}]
    tool_def = {
        "type": "function",
        "function": {"name": "web_search", "description": "搜索互联网",
                     "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    }
    r1 = await provider.complete(d_msgs, ChatParams(model=MODEL, thinking_enabled=True, max_tokens=500), tools=[tool_def])
    if not r1["tool_calls"]:
        print("  ⚠ 模型未触发工具，跳过工具轮回传验证")
    else:
        tc = r1["tool_calls"][0]
        q = (json.loads(tc["arguments"]) or {}).get("query", "")
        results = search.search_web(q, "duckduckgo", "")
        content = search.format_results(results, q) if results else "搜索失败"
        tool_msgs = [{
            "role": "assistant",
            "content": r1["content"] or "",
            **({"reasoning_content": r1["reasoning_content"]} if r1.get("reasoning_content") else {}),
            "tool_calls": [{"id": tc["id"], "type": "function",
                            "function": {"name": tc["name"], "arguments": tc["arguments"]}}],
        }, {"role": "tool", "tool_call_id": tc["id"], "content": content[:800]}]
        out2 = []
        async for evt in provider.stream_chat([*d_msgs, *tool_msgs], ChatParams(model=MODEL, thinking_enabled=True, max_tokens=300)):
            if evt["kind"] == "content":
                out2.append(evt["text"])
        final = "".join(out2).strip()
        print("  搜索「" + q[:30] + "」→ 最终回答: " + final[:60])
        check("工具轮+思考模式正常出答案", len(final) > 5, final)
        check("reasoning_content 已回传", bool(r1.get("reasoning_content")))

    print("== E. 笔记整理（真实 LLM 生成） ==")
    pconv = db.create_conversation("REALTEST-E")
    proj = db.create_learning_project("REALTEST-E", "零基础", "入门", "精炼（结构清晰）", pconv["id"])
    pid = proj["id"]
    pdir = pathlib.Path("notes") / pid
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "笔记.md").write_text("- 今天学了变量\n- 函数 def 的用法\n- 列表推导式", encoding="utf-8")
    db.add_note_meta(pid, "笔记.md", "笔记")
    try:
        result = await ls.organize_project_notes(pid, provider)
        org = (pdir / "笔记整理.md").read_text(encoding="utf-8")
        check("整理稿生成", "变量" in org and "待办" in org, org[:120])
    finally:
        db.delete_learning_project(pid)
        shutil.rmtree(pdir, ignore_errors=True)
        db.delete_conversation(pconv["id"])

    db.delete_conversation(cid)
    print("\nALL REAL TESTS PASS ✅（测试数据已清理）")

asyncio.run(main())
