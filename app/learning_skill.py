"""学习模式专属 Skill：AI 决策式引导 + 项目创建。

设计目标：
- 第一问固定为「想学什么？」（自由输入 + 快捷学科）。
- 后续每一问由 AI 根据已有回答动态决定：追问、给选项，或判断信息已足够。
- 信息足够后输出确认摘要，用户确认后 LLM 实时生成章节 → 4 份 md + DB 落库。
- 未配置 DeepSeek Key 时自动回退到固定三问，原型阶段不会卡死。

存储：SQLite（项目/章节/疑问/笔记元数据）+ notes/<project_id>/ 下 *.md（Obsidian 可打开）。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import db, knowledge
from .schemas import ChatParams

BASE_DIR = Path(__file__).resolve().parent.parent
NOTES_DIR = BASE_DIR / "notes"

# 第一问（固定主题，允许完全自由输入）+ 快捷学科
FIRST_QUESTION = {
    "question": "想学什么？",
    "input": True,
    "placeholder": "例如：Python、英语口语、产品经理、摄影后期……",
    "options": ["Python", "英语", "写作", "摄影", "数据分析", "心理学"],
}

SUBJECT_CHIPS = FIRST_QUESTION["options"]

# 未配置 LLM 时的固定回退提问（顺序推进，最终确认）
FALLBACK_QUESTIONS = [
    {"question": "现在的基础怎么样？", "field": "level_from", "input": False, "options": ["零基础", "入门", "进阶"]},
    {"question": "想达到什么水平？", "field": "level_to", "input": False, "options": ["入门", "进阶", "精通"]},
    {"question": "更想要哪种学习计划？", "field": "style", "input": False, "options": ["详细（章节拆细）", "精炼（结构清晰）"]},
]

SKIP_LABEL = "⏭ 跳过（用默认）"
STOP_WORDS = ("就这样", "就这些", "不用问", "不问了", "直接开始", "直接生成", "开始吧", "可以了", "够了", "差不多了")

_onboard: dict[str, dict] = {}  # 会话 id → 引导状态


def _fmt_date() -> str:
    return time.strftime("%Y-%m-%d")


def _new_state() -> dict:
    return {
        "history": [],   # [{"question": str, "answer": str, "field": str?}]
        "pending": None,  # 下一问 dict（AI 决策结果）
        "phase": "ask",   # ask / confirm / done
        "plan": None,     # {"subject","level_from","level_to","style","goal"}
        "subject": None,
        "assumed": [],    # 被默认填充的字段（确认摘要标注「假设」）
        "provided": [],   # 用户明确提供过的字段（区别于默认填充）
        "summary_msg_id": None,  # 确认摘要消息 id（点改字段时原位更新）
    }


def _save(key: str, st: dict) -> None:
    """引导状态持久化（SQLite）——问到一半重启应用也不丢。"""
    try:
        db.save_onboard_state(key, json.dumps(st, ensure_ascii=False))
    except Exception:  # noqa: BLE001 —— 持久化失败不阻塞引导（内存态仍可用）
        pass


def start(key: str) -> dict:
    """开始学习引导（key = 会话 id）。已有状态（含上次未完成的持久化状态）则原样返回。"""
    if key not in _onboard:
        _onboard[key] = db.load_onboard_state(key) or _new_state()
    return _onboard[key]


def get(key: str) -> dict | None:
    st = _onboard.get(key) or db.load_onboard_state(key)
    if st is not None:
        _onboard[key] = st
    return _step_data(st) if st else None


def has(key: str) -> bool:
    return key in _onboard or db.load_onboard_state(key) is not None


def clear(key: str) -> None:
    _onboard.pop(key, None)
    db.delete_onboard_state(key)


def forget(cid: str) -> None:
    """清理会话级内存态（删除会话时调用，防泄漏）。"""
    clear(cid)
    _pending_updates.pop(cid, None)
    _user_turns.pop(cid, None)
    _last_proposal_turn.pop(cid, None)
    _choose_chapter.pop(cid, None)


def note_summary_msg(key: str, mid: str) -> None:
    """记录确认摘要消息 id（点改字段时原位更新该消息）。"""
    st = _onboard.get(key) or start(key)
    st["summary_msg_id"] = mid
    _save(key, st)


def edit_plan(key: str, field: str, value: str) -> dict:
    """确认阶段修改计划字段（前端摘要卡片点改）。改完该字段脱离「假设」。"""
    st = _onboard.get(key) or db.load_onboard_state(key)
    if not st or st["phase"] not in ("confirm", "done"):
        raise ValueError("当前不在确认阶段")
    if field == "subject":
        st["plan"] = _normalize_plan({**(st.get("plan") or {}), "subject": value}, st)
        st["subject"] = str(value).strip()[:60]
    elif field == "goal":
        st["plan"] = _normalize_plan({**(st.get("plan") or {}), "goal": value}, st)
    elif field in ("level_from", "level_to", "style"):
        st["plan"] = _normalize_plan({**(st.get("plan") or {}), field: value}, st)
    else:
        raise ValueError("未知字段")
    if field in st["assumed"]:
        st["assumed"].remove(field)
    if field in ("level_from", "level_to", "style", "goal", "subject") and field not in st["provided"]:
        st["provided"].append(field)
    _onboard[key] = st
    _save(key, st)
    return st


def _pending_question(st: dict) -> dict:
    """当前该展示的问题：首问 → pending → 回退序列。"""
    if not st["history"]:
        return FIRST_QUESTION
    if st.get("pending"):
        return st["pending"]
    idx = min(len(st["history"]) - 1, len(FALLBACK_QUESTIONS) - 1)
    return FALLBACK_QUESTIONS[idx]


def _step_data(st: dict, fallback: bool = False) -> dict:
    """给 API / 消息模板用的状态快照。"""
    if st["phase"] == "recommend":
        plan = st.get("plan") or {}
        return {
            "done": False,
            "step": 0,
            "recommend": True,
            "summary": {
                **plan,
                "subject": (plan.get("subject") or st.get("subject") or "").strip(),
            },
        }
    if st["phase"] in ("confirm", "done"):
        plan = st.get("plan") or {}
        return {
            "done": True,
            "summary": {
                **plan,
                "subject": (plan.get("subject") or st.get("subject") or "").strip(),
                "confirm": True,
                "assumed": list(st.get("assumed") or []),
                "goal": plan.get("goal") or "",
            },
        }
    q = _pending_question(st)
    opts = list(q.get("options", []))
    if fallback and q.get("field"):
        opts.append(SKIP_LABEL)  # 无 key 模式也可跳过（用默认 + 标假设）
    return {
        "done": False,
        "step": len(st["history"]) + 1,
        "question": q["question"],
        "input": bool(q.get("input", False)),
        "placeholder": q.get("placeholder", "") or "",
        "options": opts,
    }


def snapshot(st: dict, fallback: bool = False) -> dict:
    """原始状态 → API 快照（供路由层使用）。"""
    return _step_data(st, fallback)


async def recommend(key: str, provider, transcript: str) -> dict | None:
    """分析普通对话，推荐想学的东西；信息不足则返回 None（走常规提问）。"""
    if getattr(provider, "name", "") != "deepseek":
        return None
    prompt = (
        "你是学习规划师。下面是用户的一段对话。判断用户可能在学或想学什么，"
        "并推断当前水平、目标水平与计划风格。信息不足就输出空 subject。\n"
        "只输出 JSON："
        '{"subject":"","level_from":"零基础/入门/进阶","level_to":"入门/进阶/精通","style":"详细（章节拆细）/精炼（结构清晰）","reason":"一句话推荐理由"}'
        f"\n\n对话：\n{transcript[:3000]}"
    )
    try:
        resp = await provider.complete(
            [{"role": "system", "content": "只输出合法 JSON，不要解释。"},
             {"role": "user", "content": prompt}],
            ChatParams(thinking_enabled=False, temperature=0.25, max_tokens=500),
        )
        data = _parse_json(resp.get("content") or "")
        if not isinstance(data, dict) or not str(data.get("subject", "")).strip():
            return None
        st = _new_state()
        st["phase"] = "recommend"
        st["subject"] = str(data["subject"]).strip()[:60]
        st["plan"] = _normalize_plan(data, st)
        _onboard[key] = st
        _save(key, st)
        return st
    except Exception:
        return None


async def answer(answer: str, key: str, provider) -> dict:
    """记录用户回答并让 AI 决策：下一问 or 确认创建。

    问题数动态：信息说全了 1 问即可确认；次要缺口默认填充并标「假设」；
    用户随时喊停（"就这样吧"）；fallback 模式支持「跳过」。
    """
    st = _onboard.get(key) or start(key)
    text = (answer or "").strip()
    if not text:
        return st

    asked_q = _pending_question(st)
    asked_text = asked_q["question"]
    field = asked_q.get("field")
    skip = text in ("跳过", "略过", SKIP_LABEL)

    # fallback 模式的「跳过」：字段记默认 + 标假设，直接进下一问；问完收尾确认
    if skip and field:
        st["history"].append({"question": asked_text, "answer": "（跳过）", "field": field})
        if field not in st["assumed"]:
            st["assumed"].append(field)
        st["pending"] = None
        if len(st["history"]) - 1 >= len(FALLBACK_QUESTIONS):
            _finalize(st)
            st["phase"] = "confirm"
        else:
            st["phase"] = "ask"
        _save(key, st)
        return st

    st["history"].append({"question": asked_text, "answer": text})
    if field and field not in st["provided"]:
        st["provided"].append(field)
    if not st.get("subject"):
        st["subject"] = text

    # 用户在确认摘要页继续回答 = 确认创建
    if st["phase"] == "confirm":
        st["phase"] = "done"
        _save(key, st)
        return st
    # 推荐确认：点「确认创建」直接建项目；点「重新填写」回到自定义提问
    if st["phase"] == "recommend":
        if "确认" in text:
            st["phase"] = "done"
        else:
            _onboard[key] = _new_state()
            st = _onboard[key]
        _save(key, st)
        return st

    # 用户喊停：立即用现有信息收尾（缺失字段默认 + 标假设）
    if _is_stop(text):
        _finalize(st)
        st["pending"] = None
        st["phase"] = "confirm"
        _save(key, st)
        return st

    decision = await _decide_next(provider, st)
    # 先吸收 LLM 从本轮回答里抽取到的信息（增量更新 plan）
    if isinstance(decision.get("updates"), dict):
        merged = {**(st.get("plan") or {}), **decision["updates"]}
        st["plan"] = _normalize_plan(merged, st)
        if str((decision["updates"].get("goal") or "")).strip():
            st["plan"]["goal"] = str(decision["updates"]["goal"]).strip()[:200]
        for f, v in decision["updates"].items():
            if f in ("level_from", "level_to", "style", "goal") and str(v or "").strip():
                if f not in st["provided"]:
                    st["provided"].append(f)
        for f in decision.get("assume") or []:
            if f in ("level_from", "level_to", "style") and f not in st["assumed"]:
                st["assumed"].append(f)

    if decision.get("action") == "confirm":
        st["plan"] = _normalize_plan(decision.get("plan") or st.get("plan") or {}, st)
        for f in decision.get("assume") or []:
            if f in ("level_from", "level_to", "style") and f not in st["assumed"]:
                st["assumed"].append(f)
        _finalize(st)
        st["pending"] = None
        st["phase"] = "confirm"
    else:
        q = decision.get("question") if decision.get("action") == "ask" else None
        st["pending"] = {
            "question": str(q or "还有什么想补充的？")[:140],
            "options": [str(x)[:40] for x in (decision.get("options") or [])][:4],
            "input": bool(decision.get("input")),
            "placeholder": str(decision.get("placeholder") or "")[:90],
        }
        if decision.get("field"):
            st["pending"]["field"] = decision["field"]
        st["phase"] = "ask"

    # 安全阀：问题数量封顶，收尾确认（不会无限追问）
    if len(st["history"]) >= 8:
        _finalize(st)
        st["pending"] = None
        st["phase"] = "confirm"
    _save(key, st)
    return st


def _is_stop(text: str) -> bool:
    """用户明确表示"别再问了，直接生成"。"""
    t = (text or "").strip()
    return len(t) <= 12 and any(w in t for w in STOP_WORDS)


def _finalize(st: dict) -> None:
    """用现有信息收尾：吸收 fallback 已答字段，未提供的字段填默认并标「假设」。"""
    raw = dict(st.get("plan") or {})
    for h in st["history"]:
        f = h.get("field")
        if f and h.get("answer") and h["answer"] != "（跳过）":
            raw.setdefault(f, h["answer"])
            if f not in st["provided"]:
                st["provided"].append(f)
    plan = _normalize_plan(raw, st)
    for f in ("level_from", "level_to", "style"):
        if f not in st["provided"] and f not in st["assumed"]:
            st["assumed"].append(f)
    st["plan"] = plan


async def _decide_next(provider, st: dict) -> dict:
    """让 LLM 一次完成两件事：从回答里抽取信息更新计划，并决定追问还是确认。

    问题数动态（没有下限）：字段齐全立即 confirm，哪怕只有 1 问；
    次要缺口（风格等）允许用 assume 默认填充，不再追问。失败时回退固定问题。
    """
    if getattr(provider, "name", "") != "deepseek":
        return _fallback_decision(st)

    transcript = "\n".join(
        f"- 问：{h['question']}\n  答：{h['answer']}" for h in st["history"]
    )
    plan_now = st.get("plan") or {}
    system = (
        "你是 Gili 的学习规划师，正在用对话帮用户设计一份学习计划。\n"
        "计划字段：subject（想学的学科/技能，必填）、level_from（当前水平：零基础/入门/进阶）、"
        "level_to（目标水平：入门/进阶/精通）、style（计划风格：详细（章节拆细）/精炼（结构清晰））、"
        "goal（真实目标场景，自由文本：多久、要能做什么、为什么学；可选，但能抽取就抽取）。\n"
        "规则：\n"
        "1. 先从用户全部回答里抽取信息：能确定的字段放进 updates；没有说的不要编造。\n"
        "2. 字段齐全就立即 confirm——问题数没有下限，用户一句话说全了一个多余的问题都别问；"
        "goal 没提到不算缺字段，不要为它追问。\n"
        "3. 需要追问时一次只问一个最关键的问题；能用选项就用选项（2-4 个短选项），否则 input=true；不要重复问已经问过的。\n"
        "4. 次要字段缺失时直接放进 assume 用默认值，放进 assume 的字段不再追问："
        "level_from 默认「零基础」，level_to 默认「入门」，style 默认「精炼（结构清晰）」。\n"
        "5. 总共不超过 5 个问题。\n"
        "只输出 JSON，不要多余文字：\n"
        '{"action":"ask","updates":{...},"assume":["..."],"question":"...","options":["..."],"input":false,"placeholder":""}\n'
        '或 {"action":"confirm","updates":{...},"assume":["..."],"plan":{"subject":"...","level_from":"...","level_to":"...","style":"...","goal":"..."}}'
    )
    user = (
        f"用户想学：{st.get('subject') or '（待确认）'}\n"
        f"已收集的信息：\n{transcript}\n"
        f"当前已确定的计划字段：{json.dumps(plan_now, ensure_ascii=False)}\n"
        f"已按默认假设的字段：{st.get('assumed') or '无'}\n\n"
        "请决定：confirm 还是再问一个最关键的问题？"
    )
    try:
        resp = await provider.complete(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            ChatParams(thinking_enabled=False, temperature=0.25, max_tokens=700),
        )
        data = _parse_json(resp.get("content") or "")
        if isinstance(data, dict) and data.get("action") in ("ask", "confirm"):
            return data
    except Exception:
        pass
    return _fallback_decision(st)


def _fallback_decision(st: dict) -> dict:
    answered_fallback = len(st["history"]) - 1  # 第一问（学科）不算回退问题
    if answered_fallback >= len(FALLBACK_QUESTIONS):
        return {"action": "confirm", "plan": {}}
    q = FALLBACK_QUESTIONS[answered_fallback]
    return {"action": "ask", **q}


def _normalize_plan(plan: dict, st: dict) -> dict:
    def level_from(raw):
        s = str(raw or "")
        if any(k in s for k in ("进阶", "中级", "有基础")):
            return "进阶"
        if any(k in s for k in ("零", "初", "没有")):
            return "零基础"
        return "入门"

    def level_to(raw):
        s = str(raw or "")
        if any(k in s for k in ("精通", "高级", "专业", "专家")):
            return "精通"
        if any(k in s for k in ("进阶", "中级")):
            return "进阶"
        return "入门"

    style_raw = str(plan.get("style") or "").lower()
    style = (
        "详细（章节拆细）"
        if any(k in style_raw for k in ("详细", "细", "拆", "detailed", "detail"))
        else "精炼（结构清晰）"
    )
    return {
        "subject": (str(plan.get("subject") or st.get("subject") or "")).strip()[:60] or "未命名",
        "level_from": level_from(plan.get("level_from")),
        "level_to": level_to(plan.get("level_to")),
        "style": style,
        "goal": str(plan.get("goal") or "").strip()[:200],
    }


def question_message(st: dict, fallback: bool = False) -> dict:
    """由状态生成 AI 引导消息：{content, quick_options, input, placeholder}。

    确认阶段返回 kind="plan_edit"：前端渲染可点击修改的摘要卡片。
    """
    d = _step_data(st, fallback)
    if d.get("recommend"):
        plan = d["summary"]
        content = (
            "🎓 我分析了你最近的对话，觉得你可能想学：**"
            f"{plan.get('subject', '')}**\n\n"
            f"- 🎚 当前水平：{plan.get('level_from', '—')}\n"
            f"- 🎯 目标水平：{plan.get('level_to', '—')}\n"
            f"- 🧭 计划风格：{plan.get('style', '—')}\n\n"
            "确认后我会生成**大纲 / 进度表 / 笔记 / 疑问本**；也可以点「重新填写」自己说想学什么。"
        )
        return {"content": content, "quick_options": ["✅ 确认创建", "✍️ 重新填写"], "input": False}
    if d["done"]:
        plan = d["summary"]
        assumed = plan.get("assumed") or []
        lines = [
            "🎓 学习目标已确认，看看对不对：",
            "",
            f"- 📚 想学：**{plan.get('subject', '')}**",
            f"- 🎚 当前水平：{plan.get('level_from', '—')}",
            f"- 🎯 目标水平：{plan.get('level_to', '—')}",
            f"- 🧭 计划风格：{plan.get('style', '—')}",
        ]
        if plan.get("goal"):
            lines.append(f"- 🎯 真实目标：{plan.get('goal')}")
        if assumed:
            lines += [
                "",
                "> 📌 虚线字段是我没问清、先按默认值**假设**的——点下方卡片可直接改。",
            ]
        lines += ["", "确认后我会**实时分析**并生成：**大纲 / 进度表 / 笔记 / 疑问本**。"]
        return {
            "content": "\n".join(lines),
            "quick_options": ["✅ 确认创建"],
            "input": False,
            "kind": "plan_edit",
            "plan": plan,
        }

    content = f"🎓 学习模式 · 第 {d['step']} 问\n\n**{d['question']}**"
    if d.get("placeholder"):
        content += f"\n\n{d['placeholder']}"
    if d["step"] == 1:
        content += (
            "\n\n> 💡 一次说全信息（比如「零基础，想两个月入门数据分析」）我可以少问甚至直接生成；"
            "随时说「就这样吧」立即确认。"
        )
    return {
        "content": content,
        "quick_options": d.get("options", []),
        "input": bool(d.get("input", False)),
        "placeholder": d.get("placeholder", ""),
    }


def _parse_json(raw: str) -> dict | None:
    text = (raw or "").strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


async def create_project(provider, key: str) -> dict:
    """确认创建：LLM 生成章节（JSON）→ DB 落库 → 写 4 份 md 文档。"""
    st = _onboard.get(key)
    if not st or st["phase"] not in ("confirm", "done"):
        raise ValueError("学习引导未完成，请先回答完问题")

    plan = st.get("plan") or _normalize_plan({}, st)
    subject = plan["subject"]
    style_detailed = "详细" in plan.get("style", "")

    goal = plan.get("goal") or ""
    prompt = (
        f"你是课程设计师。为「{subject}」设计学习计划。\n"
        f"用户当前水平：{plan.get('level_from', '零基础')}；目标水平：{plan.get('level_to', '入门')}。\n"
        f"风格：{'详细，拆成 8-12 个章节，每章一个明确主题' if style_detailed else '精炼，4-6 个章节，每章一个主题'}。\n"
        + (f"用户真实目标场景：{goal}。章节设计必须服务于这个目标。\n" if goal else "")
        + '只输出 JSON（不要 markdown 代码围栏）：{"summary": "一句话计划概述", '
          '"chapters": [{"title": "章节标题", "objective": "本章学习目标（一句话）"}]}'
    )
    params = ChatParams(thinking_enabled=False, temperature=0.3, max_tokens=2500)
    chunks: list[str] = []
    async for evt in provider.stream_chat([{"role": "system", "content": prompt}], params):
        if evt["kind"] == "content":
            chunks.append(evt["text"])
    data = _parse_json("".join(chunks))
    if not data or not data.get("chapters"):
        raise ValueError("章节生成失败，请重试")

    cid = key
    db.rename_conversation(cid, f"{subject}学习笔记")
    project = db.create_learning_project(
        subject, plan.get("level_from", ""), plan.get("level_to", ""), plan.get("style", ""), cid,
        goal=goal,
    )
    for i, ch in enumerate(data["chapters"], 1):
        db.add_learning_chapter(
            project["id"],
            str(ch.get("title", f"第{i}章"))[:60],
            str(ch.get("objective", ""))[:120],
            i,
        )

    pdir = NOTES_DIR / project["id"]
    pdir.mkdir(parents=True, exist_ok=True)
    summary = data.get("summary", "")

    outline_lines = [
        f"# {subject} 学习大纲",
        "",
        f"> {summary}",
        "",
        f"> 创建：{_fmt_date()}｜水平：{plan.get('level_from', '')} → {plan.get('level_to', '')}｜风格：{plan.get('style', '')}",
        "",
        "## 章节",
        "",
    ]
    outline_lines += [
        f"{i}. **{ch.get('title', '')}** —— {ch.get('objective', '')}"
        for i, ch in enumerate(data["chapters"], 1)
    ]

    progress_lines = [
        f"# {subject} 进度跟踪表",
        "",
        "| # | 章节 | 状态 | 日期 | 备注 |",
        "|---|------|------|------|------|",
    ]
    progress_lines += [
        f"| {i} | {ch.get('title', '')} | ☐ | | |" for i, ch in enumerate(data["chapters"], 1)
    ]

    notes_lines = [
        f"# {subject} 知识笔记",
        "",
        "> 学习过程中的笔记写在这里（支持 Markdown）。右上角 ✨ 可让 AI 帮你整理。",
        "",
        "## 笔记",
        "",
    ]

    question_lines = [
        f"# {subject} 疑问本",
        "",
        "> 学习中被带偏的旁支概念挂在这里，主线不被打断。",
        "",
        "| 概念 | 一句话解释 | 出处 | 状态 |",
        "|------|-----------|------|------|",
    ]

    files = {
        "大纲.md": "\n".join(outline_lines),
        "进度表.md": "\n".join(progress_lines),
        "笔记.md": "\n".join(notes_lines),
        "疑问本.md": "\n".join(question_lines),
    }
    for fname, content in files.items():
        (pdir / fname).write_text(content, encoding="utf-8")
        db.add_note_meta(project["id"], fname, fname.removesuffix(".md"))

    # 学习文档与章节进入知识检索索引（新项目即刻可被问答引用）
    knowledge.reindex_project(project["id"])

    clear(key)
    return db.get_learning_project(project["id"])


def project_context(cid: str) -> dict | None:
    """取会话绑定项目的完整上下文（大纲/章节状态/疑问/文档元数据）。"""
    project = db.get_learning_project_by_conv(cid)
    if not project:
        return None
    return {
        "project": project,
        "chapters": db.list_learning_chapters(project["id"]),
        "questions": db.list_learning_questions(project["id"]),
        "notes": db.list_note_meta(project["id"]),
    }


def overview_text(ctx: dict, limit: int = 1600) -> str:
    """低频区版本：只含大纲/章节状态/疑问，不含笔记正文（变化少，可进缓存指纹）。"""
    p = ctx["project"]
    chapters = ctx.get("chapters") or []
    done = sum(1 for c in chapters if c.get("status") == "done")
    lines = [
        f"当前对话已绑定学习项目「{p.get('subject', '')}」："
        f"水平 {p.get('level_from') or '零基础'} → {p.get('level_to') or '入门'}，"
        f"风格 {p.get('style') or '精炼'}，进度 {done}/{len(chapters)} 章。"
    ]
    if p.get("goal"):
        lines.append(f"用户真实目标：{p.get('goal')}。")
    if chapters:
        lines.append("大纲与章节状态：")
        for i, ch in enumerate(chapters, 1):
            mark = "已完成" if ch.get("status") == "done" else "待学习"
            objective = f" —— {ch.get('objective', '')}" if ch.get("objective") else ""
            lines.append(f"{i}. {ch.get('title', '')}【{mark}】{objective}")
    questions = ctx.get("questions") or []
    if questions:
        lines.append("挂起疑问：")
        for q in questions[:8]:
            lines.append(f"- {q.get('question', '')}")
    return "\n".join(lines)[:limit]


def notes_excerpts(ctx: dict, limit: int = 700) -> str:
    """波动区版本：笔记正文摘要，随笔记编辑变化。"""
    p = ctx["project"]
    notes = ctx.get("notes") or []
    if not notes:
        return ""
    out = ["笔记摘要："]
    for n in notes[:3]:
        fpath = NOTES_DIR / str(p.get("id", "")) / str(n.get("filename", ""))
        try:
            excerpt = fpath.read_text(encoding="utf-8")[:300].strip()
        except OSError:
            excerpt = ""
        if excerpt:
            out.append(f"- [{n.get('title') or n.get('filename', '')}] {excerpt}")
    return ("\n".join(out))[:limit] if len(out) > 1 else ""


def refresh_project_progress(pid: str) -> tuple[int, int]:
    """重算完成进度并落库，返回 (done, total)。"""
    chapters = db.list_learning_chapters(pid)
    done = sum(1 for c in chapters if c.get("status") == "done")
    pct = round(done / len(chapters) * 100) if chapters else 0
    db.update_learning_project_progress(pid, pct)
    return done, len(chapters)


# ---------- 学习档案更新建议（对话中确认后自动打勾） ----------

_pending_updates: dict[str, dict] = {}
_user_turns: dict[str, int] = {}
_last_proposal_turn: dict[str, int] = {}


def has_pending_update(cid: str) -> bool:
    return cid in _pending_updates


async def propose_update(provider, cid: str) -> dict | None:
    """判断最近学习是否完成某章，返回建议消息数据；不合适则 None。"""
    if getattr(provider, "name", "") != "deepseek":
        return None
    project = db.get_learning_project_by_conv(cid)
    if not project or has_pending_update(cid):
        return None
    turns = _user_turns.get(cid, 0)
    if turns - _last_proposal_turn.get(cid, -10) < 2:
        return None
    chapters = [c for c in db.list_learning_chapters(project["id"]) if c.get("status") != "done"]
    if not chapters:
        return None

    history = db.get_ui_messages(cid)[-6:]
    transcript = "\n".join(
        f"{m['role']}: {str(m['content'])[:400]}" for m in history if m["role"] in ("user", "assistant")
    )
    numbered = "\n".join(f"{i+1}. {c['title']}" for i, c in enumerate(chapters[:20]))
    prompt = (
        "判断用户最近是否完整学完了某个章节。若明显完成，输出要标记的章节序号；否则 none。\n"
        "只输出 JSON：{\"action\":\"chapter_done\",\"chapter_index\":1,\"note\":\"一句话笔记（可为空）\"} "
        "或 {\"action\":\"none\"}。\n"
        f"待学章节：\n{numbered}\n\n最近对话：\n{transcript[:2500]}"
    )
    try:
        resp = await provider.complete(
            [{"role": "system", "content": "只输出合法 JSON，不要解释。"},
             {"role": "user", "content": prompt}],
            ChatParams(thinking_enabled=False, temperature=0.1, max_tokens=300),
        )
        data = _parse_json(resp.get("content") or "")
        if not isinstance(data, dict) or data.get("action") != "chapter_done":
            return None
        idx = int(data.get("chapter_index") or 0)
        if not (1 <= idx <= len(chapters)):
            return None
        chapter = chapters[idx - 1]
        _pending_updates[cid] = {
            "chapter_id": str(chapter["id"]),
            "chapter_title": str(chapter["title"]),
            "chapter_index": idx,
            "note": str(data.get("note") or ""),
            "project_id": str(project["id"]),
        }
        _last_proposal_turn[cid] = turns
        return {
            "content": (
                f"📌 学习档案建议：看起来你已经学完了 **第 {idx} 章 {chapter['title']}**。\n\n"
                "要我把这一章勾掉吗？也可以补充一句笔记。"
            ),
            "quick_options": ["✅ 标记完成", "📖 我学的是别的章节", "↩ 暂不"],
            "input": True,
            "placeholder": "补充一句笔记，或直接确认…",
        }
    except Exception:
        return None


_choose_chapter: dict[str, list[dict]] = {}


def _finish_chapter_update(pending: dict, chapter: dict, extra_note: str = "") -> dict:
    db.set_chapter_status(chapter["id"], "done")
    if extra_note:
        db.update_learning_chapter_note(chapter["id"], extra_note)
    done, total = refresh_project_progress(pending["project_id"])
    knowledge.reindex_project(pending["project_id"])
    note_line = f"\n\n> 备注：{extra_note}" if extra_note else ""
    return {
        "status": "updated",
        "chapter_title": chapter["title"],
        "chapter_index": 0,
        "content": (
            f"✅ 学习档案已更新：**{chapter['title']}** 已勾选完成"
            f"（进度 {done}/{total}）。{note_line}"
        ),
    }


def handle_update_answer(cid: str, text: str) -> dict:
    """处理建议气泡的回答：确认 / 换章节 / 暂不 / 自定义笔记。"""
    if cid in _choose_chapter:
        pending = _pending_updates.get(cid)
        t = (text or "").strip()
        if "暂不" in t or "不用" in t:
            _choose_chapter.pop(cid, None)
            _pending_updates.pop(cid, None)
            return {"status": "skipped", "content": "好的，先不动。继续学吧。"}
        m = re.search(r"\d+", t)
        idx = int(m.group(0)) if m else 0
        chapters = _choose_chapter.pop(cid, None) or []
        chapter = next((c for i, c in enumerate(chapters, 1) if i == idx), None)
        if not chapter:
            return {"status": "choose", "content": "发章节序号即可，比如：3"}
        _pending_updates.pop(cid, None)
        return _finish_chapter_update(pending or {}, chapter, "")

    pending = _pending_updates.get(cid)
    if not pending:
        return {"status": "ok", "content": "好的。"}
    t = (text or "").strip()
    if "暂不" in t or "跳过" in t or "不用" in t:
        _pending_updates.pop(cid, None)
        return {"status": "skipped", "content": "好的，先不动。继续学吧。"}
    if "别的章节" in t or "其他章节" in t:
        chapters = db.list_learning_chapters(pending["project_id"])
        _choose_chapter[cid] = chapters
        listing = "\n".join(f"{i}. {c['title']}" for i, c in enumerate(chapters, 1))
        return {"status": "choose", "content": f"学的是哪一章？发序号即可：\n{listing}"}

    # 自定义文本里带「第 N 章」→ 直接改打勾目标
    m = re.search(r"第\s*(\d+)\s*章", t)
    idx = int(m.group(1)) if m else 0
    chapters = db.list_learning_chapters(pending["project_id"])
    chapter = next((c for i, c in enumerate(chapters, 1) if i == idx), None) if idx else None
    if chapter:
        pending["chapter_id"] = chapter["id"]
        pending["chapter_title"] = chapter["title"]
        pending["chapter_index"] = idx
        _pending_updates[cid] = pending
        extra_note = ""
    else:
        chapter = db.get_learning_chapter(pending["chapter_id"]) or {}
        extra_note = "" if ("确认" in t or "标记" in t or "完成" in t or t == "") else t
    _pending_updates.pop(cid, None)
    return _finish_chapter_update(pending, chapter, extra_note)


# ---------- 学习对话显式命令 ----------

def handle_command(cid: str, text: str) -> dict | None:
    """识别学习命令并直接更新档案；未命中返回 None（走正常 LLM）。"""
    project = db.get_learning_project_by_conv(cid)
    if not project:
        return None
    t = (text or "").strip()

    m = re.search(r"(?:开始|学)\s*第\s*(\d+)\s*章", t)
    if m:
        return _start_chapter(project, int(m.group(1)))
    if re.search(r"(?:开始|学)\s*下一?章", t):
        return _start_next_chapter(project)
    m = re.search(r"第\s*(\d+)\s*章\s*(?:学完|完成|学好了|搞定)", t)
    if m:
        return _finish_chapter_command(project, int(m.group(1)))
    if re.search(r"(?:本章|这章)\s*(?:学完|完成|搞定)", t):
        return _finish_chapter_command(project, None)
    m = re.match(r"(?:记一下|笔记)\s*[：:]\s*(.+)", t, re.S)
    if m:
        return _add_note_command(project, m.group(1).strip(), kind="note")
    m = re.match(r"(?:问一下|疑问)\s*[：:]\s*(.+)", t, re.S)
    if m:
        return _add_note_command(project, m.group(1).strip(), kind="question")
    return None


def _chapter_by_index(project: dict, idx: int) -> dict | None:
    chapters = db.list_learning_chapters(project["id"])
    return chapters[idx - 1] if 1 <= idx <= len(chapters) else None


def _start_chapter(project: dict, idx: int) -> dict:
    chapter = _chapter_by_index(project, idx)
    if not chapter:
        return {"status": "ok", "command": "start", "content": "没有这个章节，看一下大纲里的编号。"}
    db.set_current_chapter(project["id"], chapter["id"])
    objective = f"\n\n本章目标：{chapter['objective']}" if chapter.get("objective") else ""
    return {
        "status": "ok",
        "command": "start",
        "content": f"📖 好的，开始学习 **第 {idx} 章 {chapter['title']}**。{objective}",
    }


def _start_next_chapter(project: dict) -> dict:
    chapters = db.list_learning_chapters(project["id"])
    current = db.get_current_chapter(project["id"])
    start = 0
    if current:
        start = next((i for i, c in enumerate(chapters) if c["id"] == current["id"]), -1) + 1
    nxt = next((c for c in chapters[start:] if c.get("status") != "done"), None)
    if not nxt:
        return {"status": "ok", "command": "start_next", "content": "🎉 所有章节都已完成。"}
    idx = next(i for i, c in enumerate(chapters, 1) if c["id"] == nxt["id"])
    db.set_current_chapter(project["id"], nxt["id"])
    objective = f"\n\n本章目标：{nxt['objective']}" if nxt.get("objective") else ""
    return {
        "status": "ok",
        "command": "start_next",
        "content": f"📖 开始下一章：**第 {idx} 章 {nxt['title']}**。{objective}",
    }


def _finish_chapter_command(project: dict, idx: int | None) -> dict:
    chapter = _chapter_by_index(project, idx) if idx else db.get_current_chapter(project["id"])
    if not chapter:
        return {"status": "ok", "command": "finish", "content": "告诉我第几章（例如：第 2 章学完）。"}
    db.set_chapter_status(chapter["id"], "done")
    done, total = refresh_project_progress(project["id"])
    knowledge.reindex_project(project["id"])
    chapters = db.list_learning_chapters(project["id"])
    real_idx = next(i for i, c in enumerate(chapters, 1) if c["id"] == chapter["id"])
    nxt = next((c for c in chapters[real_idx:] if c.get("status") != "done"), None)
    nxt_line = ""
    if nxt:
        nxt_i = next(i for i, c in enumerate(chapters, 1) if c["id"] == nxt["id"])
        nxt_line = f"\n\n下一章建议：**第 {nxt_i} 章 {nxt['title']}**，可以说「开始第 {nxt_i} 章」。"
    return {
        "status": "updated",
        "command": "finish",
        "content": f"✅ **第 {real_idx} 章 {chapter['title']}** 已勾选完成（进度 {done}/{total}）。{nxt_line}",
    }


def _add_note_command(project: dict, content: str, kind: str) -> dict:
    if kind == "question":
        db.add_learning_question(project["id"], content, concept="", source_ref="对话")
        knowledge.reindex_project(project["id"])
        return {"status": "ok", "command": "question", "content": "❓ 已挂到疑问本，主线继续。"}
    chapter = db.get_current_chapter(project["id"])
    if chapter:
        old = chapter.get("note") or ""
        db.update_learning_chapter_note(chapter["id"], f"{old}\n- {content}".strip())
    else:
        fpath = NOTES_DIR / project["id"] / "笔记.md"
        try:
            existing = fpath.read_text(encoding="utf-8") if fpath.exists() else ""
        except OSError:
            existing = ""
        fpath.write_text(existing.rstrip() + f"\n- {content}\n", encoding="utf-8")
    knowledge.reindex_project(project["id"])
    where = f"第 {chapter['title']} 章" if chapter else "项目笔记"
    return {"status": "ok", "command": "note", "content": f"📝 已记到{where}。"}
