"""学习模式：对话式前置问答状态机 + 项目创建（LLM 生成章节 → md 文档 + DB 落库）。

交互（v0.15）：点顶栏「🎓 学习模式」→ 当前对话变成学习专用，
AI 在对话里逐条发问（学什么/当前水平/目标水平/风格），用户点选项卡或打字回答；
问答完成后 AI 实时分析生成 4 份文档，入口在顶栏「📖 学习文档」。

阶段 B（后续）：打勾 / 笔记编辑 / AI 整理 / 进度百分比更新。
阶段 C（后续）：疑问挂起 / 分支归档 / 锚点。

存储：SQLite 管结构化（项目/章节/疑问/笔记元数据），
      notes/<project_id>/ 存 md 内容（Obsidian 可打开）。
"""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path

from . import db
from .schemas import ChatParams

BASE_DIR = Path(__file__).resolve().parent.parent
NOTES_DIR = BASE_DIR / "notes"

# 4 步流水线（第 5 段 = 确认）
STEPS: list[dict] = [
    {"key": "subject", "question": "想学什么？", "input": True, "placeholder": "Python、英语、摄影…"},
    {"key": "level_from", "question": "当前水平？", "options": ["零基础", "入门", "进阶"]},
    {"key": "level_to", "question": "目标水平？", "options": ["入门", "进阶", "精通"]},
    {"key": "style", "question": "计划风格？", "options": ["详细（章节拆细）", "精炼（结构清晰）"]},
]

# 固定产物
_DOC_SPECS = [("大纲.md", "学习大纲"), ("进度表.md", "进度跟踪表"), ("笔记.md", "知识笔记"), ("疑问本.md", "疑问本")]

_onboard: dict[str, dict] = {}  # 会话 id → {idx, answers}（学习模式按会话跟踪）

# 第 1 问（自由输入）的快捷选项卡
SUBJECT_CHIPS = ["Python", "英语", "写作", "摄影", "数据分析", "心理学"]


def _fmt_date() -> str:
    return time.strftime("%Y-%m-%d")


def _step_data(st: dict) -> dict:
    idx = st["idx"]
    base = {"idx": idx, "answers": st["answers"]}  # idx 透传给 question_message
    if idx >= len(STEPS):
        return {**base, "step": len(STEPS) + 1, "total": len(STEPS), "done": True,
                "summary": {**st["answers"], "confirm": True}}
    s = STEPS[idx]
    return {
        **base, "step": idx + 1, "total": len(STEPS), "done": False,
        "question": s["question"], "input": s.get("input", False),
        "placeholder": s.get("placeholder", ""), "options": s.get("options", []),
    }


def start(key: str) -> dict:
    """开始学习引导（key = 会话 id）。已有状态则原样返回，不重置。"""
    st = _onboard.get(key)
    if st is None:
        st = {"idx": 0, "answers": {}}
        _onboard[key] = st
    return _step_data(st)


def answer(answer: str, key: str) -> dict:
    """记录用户回答并推进到下一步。confirm 阶段（idx 越界）再回答 = 确认创建。"""
    st = _onboard.get(key) or start(key)
    if st["idx"] < len(STEPS):
        s = STEPS[st["idx"]]
        st["answers"][s["key"]] = answer.strip()
        st["idx"] += 1
    return _step_data(st)


def get(key: str) -> dict | None:
    st = _onboard.get(key)
    return _step_data(st) if st else None


def has(key: str) -> bool:
    return key in _onboard


def clear(key: str) -> None:
    _onboard.pop(key, None)


def question_message(st: dict) -> dict:
    """由状态机生成 AI 引导消息（模板文案，不调 LLM）：{content, quick_options}。"""
    if not st["done"]:
        s = STEPS[st["idx"]]
        opts = list(s.get("options", []))
        if s.get("input"):
            opts = list(SUBJECT_CHIPS)  # 自由输入步也提供快捷选项卡
        content = (
            f"🎓 **学习模式 · 第 {st['step']}/{st['total']} 问**\n\n"
            f"**{s['question']}**"
            + (f"\n\n比如：{s['placeholder']}" if s.get("placeholder") else "")
        )
        return {"content": content, "quick_options": opts}
    # 4 问完成 → 确认摘要（点选项卡即创建）
    a = st["answers"]
    content = (
        "🎓 学习目标已确认，看看对不对：\n\n"
        f"- 📚 想学：**{a.get('subject', '')}**\n"
        f"- 🎚 当前水平：{a.get('level_from', '—')}\n"
        f"- 🎯 目标水平：{a.get('level_to', '—')}\n"
        f"- 📐 计划风格：{a.get('style', '—')}\n\n"
        "确认后我会**实时分析**并生成：**大纲 / 进度表 / 笔记 / 疑问本**。"
    )
    return {"content": content, "quick_options": ["✅ 确认创建"]}


def _parse_json(raw: str) -> dict | None:
    text = raw.strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


async def create_project(provider, key: str) -> dict:
    """确认创建：LLM 生成章节（JSON）→ DB 落库 → 写 4 份 md 文档。
    key = 会话 id：当前对话就是学习专用对话（不再新建会话）。"""
    st = _onboard.get(key)
    if not st or st["idx"] < len(STEPS):
        raise ValueError("学习引导未完成，请先回答完问题")
    a = st["answers"]
    subject = a["subject"]
    style_detailed = "详细" in a.get("style", "")

    prompt = (
        f"你是课程设计师。为「{subject}」设计学习计划。\n"
        f"用户当前水平：{a.get('level_from', '零基础')}；目标水平：{a.get('level_to', '入门')}。\n"
        f"风格：{'详细，拆成 8-12 个章节，每章一个明确主题' if style_detailed else '精炼，4-6 个章节，每章一个主题'}。\n"
        '只输出 JSON（不要 markdown 代码围栏）：{"summary": "一句话计划概述", "chapters": [{"title": "章节标题", "objective": "本章学习目标（一句话）"}]}'
    )
    params = ChatParams(thinking_enabled=False, temperature=0.3, max_tokens=2500)
    chunks: list[str] = []
    async for evt in provider.stream_chat([{"role": "system", "content": prompt}], params):
        if evt["kind"] == "content":
            chunks.append(evt["text"])
    data = _parse_json("".join(chunks))
    if not data or not data.get("chapters"):
        raise ValueError("章节生成失败，请重试")

    # 当前对话就是学习专用对话：改名 + 绑定项目
    cid = key
    db.rename_conversation(cid, f"{subject}学习笔记")
    project = db.create_learning_project(
        subject, a.get("level_from", ""), a.get("level_to", ""), a.get("style", ""), cid
    )
    for i, ch in enumerate(data["chapters"], 1):
        db.add_learning_chapter(project["id"], ch.get("title", f"第{i}章")[:60], ch.get("objective", "")[:120], i)

    # 写 4 份 md 文档
    pdir = NOTES_DIR / project["id"]
    pdir.mkdir(parents=True, exist_ok=True)
    summary = data.get("summary", "")

    # 大纲
    outline_lines = [
        f"# {subject} 学习大纲",
        "", f"> {summary}", "",
        f"> 创建：{_fmt_date()}｜水平：{a.get('level_from', '')} → {a.get('level_to', '')}｜风格：{a.get('style', '')}", "",
        "## 章节", "",
    ]
    outline_lines += [f"{i}. **{ch.get('title', '')}** —— {ch.get('objective', '')}" for i, ch in enumerate(data["chapters"], 1)]

    # 进度表
    progress_lines = [
        f"# {subject} 进度跟踪表", "",
        "| # | 章节 | 状态 | 日期 | 备注 |", "|---|------|------|------|------|",
    ]
    progress_lines += [f"| {i} | {ch.get('title', '')} | ☐ | | |" for i, ch in enumerate(data["chapters"], 1)]

    # 笔记
    notes_lines = [
        f"# {subject} 知识笔记", "",
        "> 学习过程中的笔记写在这里（支持 Markdown）。右上角 🤖 可让 AI 帮你整理。", "",
        "## 笔记", "",
    ]

    # 疑问本
    question_lines = [
        f"# {subject} 疑问本", "",
        "> 学习中被带偏的旁支概念挂在这里，主线不被打断。", "",
        "| 概念 | 一句话解释 | 出处 | 状态 |", "|------|-----------|------|------|",
    ]

    files = {"大纲.md": "\n".join(outline_lines), "进度表.md": "\n".join(progress_lines),
             "笔记.md": "\n".join(notes_lines), "疑问本.md": "\n".join(question_lines)}
    for fname, content in files.items():
        (pdir / fname).write_text(content, encoding="utf-8")
        db.add_note_meta(project["id"], fname, fname.removesuffix(".md"))

    clear(key)
    return db.get_learning_project(project["id"])
