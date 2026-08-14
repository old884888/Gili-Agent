"""三层记忆服务（真实实现，SQLite 持久化）。

- 核心记忆 core：人设级事实，稳定区
- 长期记忆 longterm：事实条目（fact）+ 每会话滚动摘要（summary），低频区
- 工作记忆 working：即近期对话（messages 表，summarized=0），波动区

摘要低频重写：
- 未摘录消息累计超过 summary_rewrite_threshold → 触发压缩
- 保留尾部 keep_tokens 原文作为工作窗口，其余交给 LLM 压成摘要
- 摘要存 memory_items（kind=summary），重写 = 低频区变化 = 缓存失效（记账）
- 原文保留（messages.summarized=1），绝不丢弃 —— 数据永远属于用户

TODO(下一阶段): Mem0 式事实抽取（对话中自动抽偏好/事实进长期记忆）
"""
from __future__ import annotations

from . import db, knowledge
from .schemas import ChatParams

# 压缩时保留的尾部原文预算（防误伤：近期对话保持逐字）。
# 自动压缩时由调用方按「预算 × 0.35」计算（预算越大保留越多）；
# 这里是直接调用时的默认兜底值。
DEFAULT_KEEP_WINDOW_TOKENS = 4000


def keep_window_for_budget(budget: int) -> int:
    """保留窗口随预算缩放：预算 × 0.35，下限 2k、上限 40k。"""
    return max(2000, min(40000, int(budget * 0.35)))


# 记忆注入预算（设计见 docs/05-记忆系统进化设计.md §4-5）
FIXED_FACT_BUDGET = 400     # 常驻：importance=3 的人格级事实（低频区）
RETRIEVE_FACT_BUDGET = 600  # 检索：按当前消息关键词命中的事实（波动区尾部）

_STOPWORDS = set(
    "的 了 吗 呢 吧 啊 呀 哦 嗯 我 你 他 她 它 们 是 在 有 就 都 也 很 还 又 和 与 或 什么 "
    "怎么 为什么 哪个 哪些 这个 那个 一下 可以 知道 记得 之前 以前 刚才 上次 说说 告诉 帮 "
    "想 要 会 能 没 不 别 请 问 的话 吗 呢 啊 啦 吧 呀 喔".split()
)


def _tokenize(text: str, max_words: int = 12) -> list[str]:
    """轻量分词（零依赖）：英文/数字词 + 连续 CJK 片段 + 2-gram，去停用词。"""
    import re

    words: set[str] = set()
    for m in re.finditer(r"[A-Za-z0-9]{2,}", text):
        w = m.group().lower()
        if w not in _STOPWORDS:
            words.add(w)
    for m in re.finditer(r"[一-鿿]{2,}", text):
        seg = m.group()
        if seg not in _STOPWORDS:
            words.add(seg)
        for i in range(len(seg) - 1):
            g = seg[i : i + 2]
            if g not in _STOPWORDS:
                words.add(g)
    return list(words)[:max_words]


def retrieve_facts(
    user_message: str, facts: list[dict], limit_tokens: int = RETRIEVE_FACT_BUDGET
) -> list[dict]:
    """关键词检索：命中词数 → importance → 新鲜度，预算截断。

    零依赖（LIKE 子串匹配）；事实数百条时毫秒级。
    jieba/FTS5/向量为事实上万条时的升级路径（docs/05 §5.2）。
    """
    words = _tokenize(user_message)
    if not words:
        return []
    scored: list[tuple[int, int, str, dict]] = []
    for f in facts:
        content = f["content"]
        hits = sum(1 for w in words if w in content)
        if hits:
            scored.append((hits, f.get("importance", 1), f["created_at"], f))
    scored.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
    out: list[dict] = []
    total = 0
    for _, _, _, f in scored:
        if total + f["tokens"] > limit_tokens:
            break
        out.append(f)
        total += f["tokens"]
    return out


def get_context(
    cid: str, user_message: str = "", recall: bool = True
) -> tuple[str, list[dict], list[dict]]:
    """返回 (摘要文本, 常驻事实, 检索事实)。

    - 常驻事实：importance=3 的人格级事实 → 低频区（几乎不动，缓存保住）
    - 检索事实：按当前消息关键词命中 → 波动区尾部（随问题变化，缓存友好）
    recall=False：不注入任何事实（常驻和检索都跳过）；摘要仍注入，
    因为摘要是当前会话自身被压缩的历史，不跨会话。
    """
    summary_item = db.get_summary_item(cid)
    summary = summary_item["content"] if summary_item else ""
    facts = db.list_memory(kind="fact")
    # 记忆作用域：核心记忆与无会话归属的手动记忆全局可见；
    # 普通事实只对本会话 / 同记忆组成员可见（默认每个对话独立）。
    scope = set(db.get_memory_scope(cid))
    facts = [
        f for f in facts
        if f.get("layer") == "core"
        or not f.get("conversation_id")
        or f.get("conversation_id") in scope
    ]

    # 常驻：importance=3，按新鲜度，预算截断（防手动加一堆 ⭐3）
    fixed = sorted(
        (f for f in facts if f.get("importance", 1) >= 3),
        key=lambda f: f["created_at"],
        reverse=True,
    )
    fixed_out: list[dict] = []
    total = 0
    for f in fixed:
        if total + f["tokens"] > FIXED_FACT_BUDGET:
            break
        fixed_out.append(f)
        total += f["tokens"]

    if not recall:
        return summary, [], []
    retrieved = retrieve_facts(user_message, facts)
    fixed_ids = {f["id"] for f in fixed_out}
    retrieved = [f for f in retrieved if f["id"] not in fixed_ids]
    return summary, fixed_out, retrieved


# 每 N 轮用户消息批量抽取一次（缓存友好：批量写 = 低频区少变 = 少失效）
EXTRACT_INTERVAL = 3


async def extract_facts(cid: str, provider) -> dict:
    """海马体：从对话提取事实/偏好/决定 → 与已有记忆去重合并（Mem0 式）。

    返回 {"added": n, "updated": n, "deleted": n}。
    批量触发（EXTRACT_INTERVAL 轮一次），LLM 用 flash + 思考关闭（廉价）。
    """
    pending = [m for m in db.get_messages(cid) if not m.get("extracted")]
    if len([m for m in pending if m["role"] == "user"]) < EXTRACT_INTERVAL:
        return {"added": 0, "updated": 0, "deleted": 0}

    # 只允许海马体看到并管理本会话（或共享记忆组）里的事实，
    # 否则“自动抽取”会把上一段对话的记忆悄悄变成全局记忆。
    scope = set(db.get_memory_scope(cid))
    existing = [
        f for f in db.list_memory(kind="fact")
        if f.get("conversation_id") in scope
    ]
    transcript = "\n".join(
        f"{'用户' if m['role'] == 'user' else 'Gili'}：{m['content'][:200]}"
        for m in pending[-10:]
    )[:6000]
    existing_block = "\n".join(
        f"- {f['content']} (id: {f['id']}, 重要度: {f.get('importance', 2)})"
        for f in existing[-30:]
    ) or "（暂无）"

    messages = [
        {
            "role": "system",
            "content": (
                "你是记忆整理器（海马体）。从对话中提取值得长期记住的信息，并与已有记忆去重合并。\n"
                "提取范围：用户偏好、重要事实、做出的决定、未完成的任务。琐碎闲聊不提取。\n"
                "与已有记忆同主题用 update 强化；明确过时/错误的用 delete；无变化用 noop。\n"
                "重要度：3=长期稳定且重要（如职业/核心偏好），2=一般事实，1=细节/次要。\n"
                "只输出 JSON，不要任何解释：\n"
                '{"actions": [{"op": "add", "content": "...", "importance": 1|2|3}, '
                '{"op": "update", "match_id": "m...", "content": "新内容", "importance": 2}, '
                '{"op": "delete", "match_id": "m..."}, {"op": "noop"}]}'
            ),
        },
        {
            "role": "user",
            "content": f"已有记忆：\n{existing_block}\n\n对话：\n{transcript}",
        },
    ]
    params = ChatParams(thinking_enabled=False, temperature=0.0, max_tokens=800)
    chunks = []
    async for evt in provider.stream_chat(messages, params):
        if evt["kind"] == "content":
            chunks.append(evt["text"])
    raw = "".join(chunks).strip()
    actions = _parse_actions(raw)
    if actions is None:
        return {"added": 0, "updated": 0, "deleted": 0}

    added = updated = deleted = 0
    for a in actions:
        op = a.get("op")
        if op == "add" and a.get("content"):
            row = db.add_memory_item(
                "longterm",
                a["content"].strip()[:500],
                conversation_id=cid,
                importance=a.get("importance", 2),
            )
            knowledge.reindex_memory(row["id"])
            added += 1
        elif op == "update" and a.get("match_id") and a.get("content"):
            if not any(f["id"] == a["match_id"] for f in existing):
                continue
            db.update_memory_item(
                a["match_id"], a["content"].strip()[:500], a.get("importance")
            )
            knowledge.reindex_memory(a["match_id"])
            updated += 1
        elif op == "delete" and a.get("match_id"):
            if not any(f["id"] == a["match_id"] for f in existing):
                continue
            db.delete_memory_item(a["match_id"])
            knowledge.delete_source("memory", a["match_id"])
            deleted += 1

    db.mark_extracted([m["id"] for m in pending])
    return {"added": added, "updated": updated, "deleted": deleted}


def _parse_actions(raw: str) -> list[dict] | None:
    """解析 LLM 输出的 JSON（容忍代码围栏/前后缀噪音）。"""
    import json
    import re

    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
        actions = data.get("actions", [])
        return actions if isinstance(actions, list) else None
    except json.JSONDecodeError:
        return None


def should_summarize(cid: str, threshold: int) -> bool:
    unsm = db.get_messages(cid, summarized=0)
    return sum(m["tokens"] for m in unsm) > threshold


async def summarize_conversation(
    cid: str, provider, keep_window: int = DEFAULT_KEEP_WINDOW_TOKENS, force: bool = False
) -> str | None:
    """压缩对话：尾部留工作窗口，其余 LLM 摘要 → 标记 summarized → 写摘要。

    force=False（自动触发）：保留尾部 keep_window token 原文，只压更旧的
    （keep_window 由调用方按预算 × 0.35 计算，预算越大保留越多）；
    force=True（手动按钮）：只保留最近 2 条，其余全部压缩 —— 点了就有反馈。
    返回摘要文本；无内容可压缩时返回 None。
    """
    msgs = db.get_messages(cid, summarized=0)
    if not msgs:
        return None

    if force:
        old = msgs[:-2] if len(msgs) > 2 else []
    else:
        # 尾部保留工作窗口，其余压缩
        keep: list[dict] = []
        total = 0
        for m in reversed(msgs):
            if total + m["tokens"] > keep_window:
                break
            keep.insert(0, m)
            total += m["tokens"]
        old = msgs[: len(msgs) - len(keep)]
    if not old:
        return None

    transcript = "\n".join(
        f"{'用户' if m['role'] == 'user' else 'Gili'}：{m['content']}" for m in old
    )
    summary_text = await _llm_summarize(provider, transcript)
    if not summary_text:
        return None

    db.mark_summarized([m["id"] for m in old])
    db.upsert_summary(cid, summary_text)
    return summary_text


async def _llm_summarize(provider, transcript: str) -> str:
    """用 LLM 压缩对话（思考关闭 + 低温，保证确定性输出）。"""
    messages = [
        {
            "role": "system",
            "content": (
                "你是记忆整理器。把用户的对话压缩成中文摘要（150-300 字），"
                "保留：用户偏好、重要事实、做出的决定、未完成的事。"
                "只输出摘要正文，不要任何解释。"
            ),
        },
        {"role": "user", "content": transcript[:12000]},
    ]
    params = ChatParams(thinking_enabled=False, temperature=0.0, max_tokens=600)
    chunks = []
    async for evt in provider.stream_chat(messages, params):
        if evt["kind"] == "content":
            chunks.append(evt["text"])
    return "".join(chunks).strip()
