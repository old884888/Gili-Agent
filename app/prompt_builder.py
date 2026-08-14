"""PromptBuilder —— 前缀命中率的核心模块（三区完整实现）。

分区顺序永不变（逐字节稳定是缓存命中的前提）：
  稳定区 [System/人设 + 技能静态片段]
  低频区 [长期记忆摘要 + 核心/长期记忆条目]（阈值触发才重写）
  波动区 [近期历史（确定性裁剪）] [环境信息（技能动态注入）] [当前消息]

前缀指纹：对 稳定区+低频区 做 SHA-256。指纹变化 = 缓存失效，
由调用方记录 cache_invalidated 指标（技能切换 / 摘要重写 / 记忆编辑都会触发）。

裁剪原则：只从开头整条丢，不截断半句（保护块对齐）；裁剪确定性，
两次裁剪之间前缀稳定。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime

from . import skills as skills_mod


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数（中文≈1字、英文≈4字符）。用于预算裁剪，够用即可。"""
    if not text:
        return 0
    ascii_n = sum(1 for ch in text if ord(ch) < 128)
    return (len(text) - ascii_n) + ascii_n // 4


def build_skill_context(settings: dict) -> tuple[list[str], str]:
    """返回 (稳定区技能片段, 波动区动态注入文本)。

    技能片段按注册表顺序确定性合并；volatile 部分（时间戳等）
    注入波动区，随当前用户消息一起发送 —— 这是缓存稳定性的关键。
    """
    fragments: list[str] = []
    volatile: list[str] = []
    for s in skills_mod.enabled_skills(settings):
        if s.get("prompt_fragment"):
            fragments.append(s["prompt_fragment"])
        v = s.get("volatile")
        if v:
            volatile.append(
                v["prompt"].format(now=datetime.now().strftime("%Y-%m-%d %H:%M"))
            )
    return fragments, "\n".join(volatile)


def build_prompt(
    *,
    soul: str,
    skill_fragments: list[str],
    summary: str,
    memories: list[dict],
    history: list[dict],
    current: dict,
    volatile_text: str = "",
    lowfreq_text: str = "",
    retrieved: list[dict] | None = None,
) -> tuple[list[dict], str]:
    """组装三区消息序列，返回 (messages, 前缀指纹)。

    前缀指纹 = 稳定区+低频区的 SHA-256。指纹不变 → 缓存可命中。
    retrieved（检索记忆）进波动区尾部 —— 随问题变化但不影响缓存前缀主体
    （设计见 docs/05-记忆系统进化设计.md §4）。
    """
    # 稳定区
    stable_content = soul
    if skill_fragments:
        stable_content += "\n\n" + "\n".join(skill_fragments)
    stable = [{"role": "system", "content": stable_content}]

    # 低频区（变化频率低：仅摘要重写/记忆编辑时变）
    lowfreq_parts: list[str] = []
    if summary:
        lowfreq_parts.append("【长期记忆摘要】\n" + summary)
    if lowfreq_text:
        lowfreq_parts.append(lowfreq_text)
    facts = [m for m in memories if m["layer"] in ("core", "longterm")]
    if facts:
        lowfreq_parts.append(
            "【记忆】\n" + "\n".join(f"- {m['content']}" for m in facts)
        )
    lowfreq = (
        [{"role": "system", "content": "\n\n".join(lowfreq_parts)}] if lowfreq_parts else []
    )

    fp = hashlib.sha256(
        json.dumps(stable + lowfreq, ensure_ascii=False).encode("utf-8")
    ).hexdigest()

    # 波动区：环境信息 → 检索记忆 → 当前消息（全在尾部，缓存友好）
    tail: list[dict] = []
    if volatile_text:
        tail.append({"role": "system", "content": f"（环境信息）{volatile_text}"})
    if retrieved:
        tail.append(
            {
                "role": "system",
                "content": "【相关记忆】\n" + "\n".join(f"- {f['content']}" for f in retrieved),
            }
        )
    tail.append(current)

    return [*stable, *lowfreq, *history, *tail], fp
