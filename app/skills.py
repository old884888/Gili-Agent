"""Skill 注册表 —— 可开关的能力模块。

缓存设计（见 docs/02-技术文档.md 3.5）：
- 开启后静态片段（prompt_fragment）进【稳定区】→ 开关 = 缓存失效，需记账
- 动态内容（volatile）由 PromptBuilder 注入【波动区】，禁止进稳定区
- 合并顺序 = 本注册表顺序（确定性，不随开关顺序变化）
"""
from __future__ import annotations

import json
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
USER_SKILLS_FILE = DATA_DIR / "user_skills.json"

SKILLS: list[dict] = [
    {
        "id": "time",
        "name": "时间感知",
        "desc": "知道当前日期时间，回答时间相关问题时可用",
        "prompt_fragment": "你了解当前日期时间（由系统在对话中提供）。",
        "volatile": {"prompt": "（当前时间：{now}）"},
    },
    {
        "id": "coder",
        "name": "程序员模式",
        "desc": "编程问答更严谨：简洁可运行的示例 + 运行环境 + 常见坑",
        "prompt_fragment": "用户会问编程问题。回答时给出简洁正确的代码示例，注明运行环境，指出常见坑。",
        "volatile": None,
    },
    {
        "id": "search",
        "name": "联网搜索",
        "desc": "实时信息、新闻、事实查证（DuckDuckGo 免费 / Tavily 可选）",
        "prompt_fragment": "你可以调用 web_search 工具获取实时信息。当用户需要最新资讯、新闻、实时数据、或需要查证的事实性问题时，主动使用它。",
        "volatile": None,
        "tool": "web_search",
    },
]


def _user_skills() -> dict[str, dict]:
    if not USER_SKILLS_FILE.exists():
        return {}
    try:
        data = json.loads(USER_SKILLS_FILE.read_text(encoding="utf-8"))
        return data.get("skills") or {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_user_skill(skill: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    data = {"skills": _user_skills()}
    data["skills"][skill["id"]] = skill
    USER_SKILLS_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def remove_user_skill(skill_id: str) -> None:
    data = {"skills": _user_skills()}
    data["skills"].pop(skill_id, None)
    USER_SKILLS_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _registry_with_user() -> list[dict]:
    base = list(SKILLS)
    for sid, s in _user_skills().items():
        base.append({
            "id": sid,
            "name": s.get("name", sid),
            "desc": s.get("desc", ""),
            "prompt_fragment": s.get("prompt_fragment", ""),
            "volatile": None,
            "user": True,
        })
    return base


def enabled_skills(settings: dict) -> list[dict]:
    """当前开启的技能（注册表顺序）。"""
    on = settings.get("skills") or {}
    return [s for s in _registry_with_user() if on.get(s["id"])]


def to_public() -> list[dict]:
    """供前端渲染的公开信息（不含提示词片段）。"""
    return [
        {
            "id": s["id"], "name": s["name"], "desc": s["desc"],
            "future": s.get("future", False), "user": bool(s.get("user")),
        }
        for s in _registry_with_user()
    ]
