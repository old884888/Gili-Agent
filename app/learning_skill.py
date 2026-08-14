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
    {"question": "现在的基础怎么样？", "input": False, "options": ["零基础", "入门", "进阶"]},
    {"question": "想达到什么水平？", "input": False, "options": ["入门", "进阶", "精通"]},
    {"question": "更想要哪种学习计划？", "input": False, "options": ["详细（章节拆细）", "精炼（结构清晰）"]},
]

_onboard: dict[str, dict] = {}  # 会话 id → 引导状态


def _fmt_date() -> str:
    return time.strftime("%Y-%m-%d")


def _new_state() -> dict:
    return {
        "history": [],   # [{"question": str, "answer": str}]
        "pending": None,  # 下一问 dict（AI 决策结果）
        "phase": "ask",   # ask / confirm / done
        "plan": None,     # {"subject","level_from","level_to","style"}
        "subject": None,
    }


def start(key: str) -> dict:
    """开始学习引导（key = 会话 id）。已有状态则原样返回，不重置。"""
    if key not in _onboard:
        _onboard[key] = _new_state()
    return _onboard[key]


def get(key: str) -> dict | None:
    st = _onboard.get(key)
    return _step_data(st) if st else None


def has(key: str) -> bool:
    return key in _onboard


def clear(key: str) -> None:
    _onboard.pop(key, None)


def _pending_question(st: dict) -> dict:
    """当前该展示的问题：首问 → pending → 回退序列。"""
    if not st["history"]:
        return FIRST_QUESTION
    if st.get("pending"):
        return st["pending"]
    idx = min(len(st["history"]) - 1, len(FALLBACK_QUESTIONS) - 1)
    return FALLBACK_QUESTIONS[idx]


def _step_data(st: dict) -> dict:
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
            },
        }
    q = _pending_question(st)
    return {
        "done": False,
        "step": len(st["history"]) + 1,
        "question": q["question"],
        "input": bool(q.get("input", False)),
        "placeholder": q.get("placeholder", "") or "",
        "options": list(q.get("options", [])),
    }


def snapshot(st: dict) -> dict:
    """原始状态 → API 快照（供路由层使用）。"""
    return _step_data(st)


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
        return st
    except Exception:
        return None


async def answer(answer: str, key: str, provider) -> dict:
    """记录用户回答并让 AI 决策：下一问 or 确认创建。"""
    st = _onboard.get(key) or start(key)
    text = (answer or "").strip()
    if not text:
        return st

    asked = _pending_question(st)["question"]
    st["history"].append({"question": asked, "answer": text})
    if not st.get("subject"):
        st["subject"] = text

    # 用户在确认摘要页继续回答 = 确认创建
    if st["phase"] == "confirm":
        st["phase"] = "done"
        return st
    # 推荐确认：点「确认创建」直接建项目；点「重新填写」回到自定义提问
    if st["phase"] == "recommend":
        if "确认" in text:
            st["phase"] = "done"
        else:
            _onboard[key] = _new_state()
            st = _onboard[key]
        return st

    decision = await _decide_next(provider, st)
    if decision.get("action") == "confirm":
        st["plan"] = _normalize_plan(decision.get("plan") or {}, st)
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
        st["phase"] = "ask"

    # 安全阀：问题数量封顶，避免无限追问
    if len(st["history"]) >= 6:
        st["plan"] = _normalize_plan(st.get("plan") or {}, st)
        st["pending"] = None
        st["phase"] = "confirm"
    return st


async def _decide_next(provider, st: dict) -> dict:
    """让 LLM 决定：继续追问还是信息已足够。失败时回退固定问题。"""
    if getattr(provider, "name", "") != "deepseek":
        return _fallback_decision(st)

    transcript = "\n".join(
        f"- 问：{h['question']}\n  答：{h['answer']}" for h in st["history"]
    )
    system = (
        "你是 Gili 的学习规划师，正在用对话帮用户设计一份学习计划。\n"
        "规则：\n"
        "1. 一次只问一个最关键的问题；问题必须针对用户刚说的内容，不要重复已经问过的。\n"
        "2. 能用选项就用选项（2-4 个短选项）；信息无法选项化时让用户自由输入（input=true）。\n"
        "3. 当已经能确定「学科、当前水平、目标水平、计划风格」时，立即输出 confirm，不要继续问。\n"
        "4. 总共不要超过 4-5 个问题。\n"
        "只输出 JSON，不要多余文字：\n"
        '{"action":"ask","question":"...","options":["..."],"input":false,"placeholder":""}\n'
        '或 {"action":"confirm","plan":{"subject":"...","level_from":"零基础/入门/进阶","level_to":"入门/进阶/精通","style":"详细（章节拆细）/精炼（结构清晰）"}}'
    )
    user = (
        f"用户想学：{st.get('subject') or '（待确认）'}\n"
        f"已收集的信息：\n{transcript}\n\n"
        "请决定：继续问一个最关键的问题，还是信息已经足够、准备生成计划？"
    )
    try:
        resp = await provider.complete(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            ChatParams(thinking_enabled=False, temperature=0.25, max_tokens=500),
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
    }


def question_message(st: dict) -> dict:
    """由状态生成 AI 引导消息：{content, quick_options, input, placeholder}。"""
    d = _step_data(st)
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
        content = (
            "🎓 学习目标已确认，看看对不对：\n\n"
            f"- 📚 想学：**{plan.get('subject', '')}**\n"
            f"- 🎚 当前水平：{plan.get('level_from', '—')}\n"
            f"- 🎯 目标水平：{plan.get('level_to', '—')}\n"
            f"- 🧭 计划风格：{plan.get('style', '—')}\n\n"
            "确认后我会**实时分析**并生成：**大纲 / 进度表 / 笔记 / 疑问本**。"
        )
        return {"content": content, "quick_options": ["✅ 确认创建"], "input": False}

    content = f"🎓 学习模式 · 第 {d['step']} 问\n\n**{d['question']}**"
    if d.get("placeholder"):
        content += f"\n\n{d['placeholder']}"
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

    prompt = (
        f"你是课程设计师。为「{subject}」设计学习计划。\n"
        f"用户当前水平：{plan.get('level_from', '零基础')}；目标水平：{plan.get('level_to', '入门')}。\n"
        f"风格：{'详细，拆成 8-12 个章节，每章一个明确主题' if style_detailed else '精炼，4-6 个章节，每章一个主题'}。\n"
        '只输出 JSON（不要 markdown 代码围栏）：{"summary": "一句话计划概述", '
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
        subject, plan.get("level_from", ""), plan.get("level_to", ""), plan.get("style", ""), cid
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


def context_text(ctx: dict, limit: int = 2000) -> str:
    """把项目上下文压成一段给聊天模型看的紧凑文本。"""
    p = ctx["project"]
    chapters = ctx.get("chapters") or []
    done = sum(1 for c in chapters if c.get("status") == "done")
    lines = [
        f"当前对话已绑定学习项目「{p.get('subject', '')}」："
        f"水平 {p.get('level_from') or '零基础'} → {p.get('level_to') or '入门'}，"
        f"风格 {p.get('style') or '精炼'}，进度 {done}/{len(chapters)} 章。"
    ]
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
    notes = ctx.get("notes") or []
    if notes:
        remaining = limit - len("\n".join(lines))
        if remaining > 80:
            note_lines = ["笔记摘要："]
            for n in notes[:4]:
                fpath = NOTES_DIR / str(p.get("id", "")) / str(n.get("filename", ""))
                try:
                    excerpt = fpath.read_text(encoding="utf-8")[:500].strip()
                except OSError:
                    excerpt = ""
                if excerpt:
                    title = n.get("title") or n.get("filename", "")
                    note_lines.append(f"- [{title}] {excerpt}")
            if len(note_lines) > 1:
                return "\n".join(lines + ["", *note_lines])[:limit]
    return "\n".join(lines)[:limit]


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
