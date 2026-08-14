"""知识检索与引用（M1：FTS5 BM25 + 可选 DeepSeek 重排，零新依赖）。

知识来源：
- note    学习文档 notes/<project_id>/*.md（大纲/进度表/笔记/疑问本）
- memory  长期记忆（kind=fact）
- chapter 学习章节（标题 + 目标 + 笔记）
- question 挂起疑问

检索结果只注入波动区尾部，不进入缓存前缀。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from . import db
from .schemas import ChatParams

BASE_DIR = Path(__file__).resolve().parent.parent
NOTES_DIR = BASE_DIR / "notes"

RETRIEVE_BUDGET = 1200  # 注入提示词的知识 token 预算
TOP_K = 15              # 重排候选数
CHUNK_TARGET = 300      # 分块目标长度（字符）
CHUNK_OVERLAP = 40      # 块间重叠

_LAYER_NAMES = {"core": "核心记忆", "longterm": "长期记忆", "working": "工作记忆"}


def _terms(text: str, max_terms: int = 16) -> list[str]:
    """查询分词：英文/数字词 + CJK 段 + 2-gram，去重。"""
    words: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r"[A-Za-z0-9]{2,}", text):
        w = m.group().lower()
        if w not in seen:
            seen.add(w)
            words.append(w)
    for m in re.finditer(r"[\u4e00-\u9fff]{2,}", text):
        seg = m.group()
        if seg not in seen:
            seen.add(seg)
            words.append(seg)
        for i in range(len(seg) - 1):
            g = seg[i : i + 2]
            if g not in seen:
                seen.add(g)
                words.append(g)
    return words[:max_terms]


def _split_sentences(para: str, target: int) -> list[str]:
    out: list[str] = []
    buf = ""
    for sent in re.split(r"(?<=[。！？.!?])\s*", para):
        sent = sent.strip()
        if not sent:
            continue
        if len(buf) + len(sent) <= target:
            buf += sent
        else:
            if buf:
                out.append(buf)
            while len(sent) > target:
                out.append(sent[:target])
                sent = sent[target:]
            buf = sent
    if buf:
        out.append(buf)
    return out


def split_text(text: str, target: int = CHUNK_TARGET, overlap: int = CHUNK_OVERLAP) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= target:
        return [text]
    raw: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= target:
            raw.append(para)
        else:
            raw.extend(_split_sentences(para, target))
    # 把段落合并到接近目标长度，再补重叠
    chunks: list[str] = []
    buf = ""
    for p in raw:
        if len(buf) + len(p) + 1 <= target:
            buf = f"{buf}\n{p}" if buf else p
        else:
            if buf:
                chunks.append(buf)
            buf = p
    if buf:
        chunks.append(buf)
    out: list[str] = []
    for c in chunks:
        if out and overlap:
            c = out[-1][-overlap:] + "\n" + c
        out.append(c)
    return out


def split_markdown(
    text: str, target: int = CHUNK_TARGET, overlap: int = CHUNK_OVERLAP
) -> list[dict]:
    """按标题切段再分块，返回 [{"heading","content"}]。"""
    text = text or ""
    matches = list(re.finditer(r"^(#{1,4})\s+(.+)$", text, re.M))
    sections: list[dict] = []
    if not matches:
        return [{"heading": "", "content": c} for c in split_text(text, target, overlap)]
    if matches and text[: matches[0].start()].strip():
        for c in split_text(text[: matches[0].start()].strip(), target, overlap):
            sections.append({"heading": "", "content": c})
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        heading = m.group(2).strip()
        for c in split_text(text[start:end].strip(), target, overlap):
            sections.append({"heading": heading, "content": c})
    return sections


# ---------- 索引 ----------

def reindex_notes(project_id: str) -> None:
    for meta in db.list_note_meta(project_id):
        fpath = NOTES_DIR / project_id / str(meta.get("filename", ""))
        try:
            text = fpath.read_text(encoding="utf-8")
        except OSError:
            text = ""
        chunks = [
            {
                "project_id": project_id,
                "title": meta.get("title") or meta.get("filename", ""),
                "heading": sec["heading"],
                "content": sec["content"],
                "ordinal": i,
            }
            for i, sec in enumerate(split_markdown(text))
        ]
        db.replace_knowledge_chunks("note", str(meta["id"]), chunks)


def reindex_memory(item_id: str) -> None:
    item = db.get_memory_item(item_id)
    if not item or item.get("kind") != "fact":
        db.delete_knowledge_source("memory", item_id)
        return
    db.replace_knowledge_chunks("memory", item_id, [{
        "project_id": None,
        "conversation_id": item.get("conversation_id"),
        "title": _LAYER_NAMES.get(item.get("layer", ""), item.get("layer", "")),
        "heading": "",
        "content": item.get("content", ""),
    }])


def reindex_project(project_id: str) -> None:
    reindex_notes(project_id)
    for ch in db.list_learning_chapters(project_id):
        parts = [str(ch.get("title", ""))]
        if ch.get("objective"):
            parts.append(str(ch["objective"]))
        if ch.get("note"):
            parts.append(str(ch["note"]))
        db.replace_knowledge_chunks("chapter", str(ch["id"]), [{
            "project_id": project_id,
            "title": str(ch.get("title", "")),
            "heading": "",
            "content": "\n".join(parts),
        }])
    for q in db.list_learning_questions(project_id):
        parts = [str(q.get("question", ""))]
        if q.get("concept"):
            parts.append("概念：" + str(q["concept"]))
        if q.get("source_ref"):
            parts.append("出处：" + str(q["source_ref"]))
        db.replace_knowledge_chunks("question", str(q["id"]), [{
            "project_id": project_id,
            "title": "挂起疑问",
            "heading": "",
            "content": "\n".join(parts),
        }])


def delete_source(source_type: str, source_id: str) -> None:
    db.delete_knowledge_source(source_type, source_id)


def ensure_memory_indexed() -> None:
    if db.count_knowledge_chunks("memory") == 0:
        for item in db.list_memory(kind="fact"):
            reindex_memory(str(item["id"]))


def ensure_project_indexed(project_id: str) -> None:
    if not _project_has_notes(project_id):
        reindex_project(project_id)


def _project_has_notes(project_id: str) -> bool:
    rows = db.list_knowledge_chunks("note")
    return any(r.get("project_id") == project_id for r in rows)


def reindex_all() -> dict:
    """一次性回填：所有学习项目 + 所有事实记忆。"""
    n_projects = 0
    for p in db.list_learning_projects():
        reindex_project(str(p["id"]))
        n_projects += 1
    n_memory = 0
    for item in db.list_memory(kind="fact"):
        reindex_memory(str(item["id"]))
        n_memory += 1
    return {"projects": n_projects, "memory": n_memory}


# ---------- 检索 ----------

def _fts_query(terms: list[str]) -> str | None:
    phrases = [f'"{t.replace(chr(34), chr(34) * 2)}"' for t in terms if len(t) >= 3]
    return " OR ".join(phrases[:8]) or None


def _bm25_search(
    query: str, terms: list[str], project_id: str | None, memory_scope: list[str] | None, limit: int
) -> list[dict]:
    qs = _fts_query(terms)
    if not qs:
        return []
    return db.knowledge_fts_search(
        qs, project_id=project_id, memory_scope=memory_scope, limit=limit
    )


def _like_search(
    terms: list[str], project_id: str | None, memory_scope: list[str] | None, limit: int
) -> list[dict]:
    short = [t for t in terms if len(t) == 2]
    if not short and db.knowledge_fts_available():
        return []
    use = terms if not db.knowledge_fts_available() else short
    return db.knowledge_like_search(
        use, project_id=project_id, memory_scope=memory_scope, limit=limit
    )


def _merge_candidates(fts_rows: list[dict], like_rows: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    for r in fts_rows:
        r = dict(r)
        r["bm25"] = r.pop("score", None)
        merged[str(r["id"])] = r
    for r in like_rows:
        r = dict(r)
        if str(r["id"]) in merged:
            merged[str(r["id"])]["like"] = True
        else:
            r["bm25"] = None
            r["like"] = True
            merged[str(r["id"])] = r
    items = list(merged.values())
    have = [i["bm25"] for i in items if i.get("bm25") is not None]
    mn, mx = (min(have), max(have)) if have else (0.0, 1.0)
    rng = (mx - mn) or 1.0
    for i in items:
        if i.get("bm25") is not None:
            i["score"] = 1.0 - (float(i["bm25"]) - mn) / rng
        else:
            i["score"] = 0.15
        if i.get("like"):
            i["score"] += 0.15
    items.sort(key=lambda x: float(x.get("score", 0)), reverse=True)
    return items


async def _rerank(provider, query: str, candidates: list[dict]) -> list[dict]:
    if getattr(provider, "name", "") != "deepseek" or len(candidates) <= 1:
        return candidates
    numbered = []
    for i, c in enumerate(candidates, 1):
        head = c.get("title", "")
        if c.get("heading"):
            head += " · " + str(c["heading"])
        numbered.append(f"[{i}] {head}\n{str(c.get('content', ''))[:220]}")
    prompt = (
        f"你是检索重排器。\n问题：{query}\n\n候选片段：\n" + "\n\n".join(numbered) +
        "\n\n按与问题的相关性给每个候选打 0-100 分。只输出 JSON：{\"scores\":[...]}，数组长度必须等于候选数。"
    )
    try:
        resp = await provider.complete(
            [{"role": "system", "content": "只输出合法 JSON，不要解释。"},
             {"role": "user", "content": prompt}],
            ChatParams(thinking_enabled=False, temperature=0, max_tokens=500),
        )
        m = re.search(r"\{.*\}", resp.get("content") or "", re.DOTALL)
        if not m:
            return candidates
        scores = (json.loads(m.group(0)).get("scores") or [])
        if len(scores) != len(candidates):
            return candidates
        for i, c in enumerate(candidates):
            c["score"] = float(scores[i])
        candidates.sort(key=lambda x: float(x.get("score", 0)), reverse=True)
    except Exception:
        pass
    return candidates


async def search(
    query: str,
    provider,
    *,
    project_id: str | None = None,
    conversation_id: str | None = None,
    enable_rerank: bool = False,
    top_k: int = TOP_K,
    budget_tokens: int = RETRIEVE_BUDGET,
) -> list[dict]:
    q = (query or "").strip()
    if not q:
        return []
    ensure_memory_indexed()
    if project_id:
        ensure_project_indexed(project_id)

    terms = _terms(q)
    scope = db.get_memory_scope(conversation_id) if conversation_id else None
    merged = _merge_candidates(
        _bm25_search(q, terms, project_id, scope, top_k * 4),
        _like_search(terms, project_id, scope, top_k * 4),
    )
    candidates = merged[:top_k]
    if enable_rerank:
        candidates = await _rerank(provider, q, candidates)

    out: list[dict] = []
    total = 0
    for c in candidates:
        cost = max(int(c.get("tokens") or 0), 40)
        if out and total + cost > budget_tokens:
            break
        out.append(c)
        total += cost
    return out


# ---------- 提示词与引用元数据 ----------

def context_block(sources: list[dict]) -> str:
    if not sources:
        return ""
    lines = [
        "【知识来源】回答时可引用以下资料。引用处写 [编号]，编号必须与来源编号一致；"
        "没有用到的资料不要引用；禁止编造编号。"
    ]
    for i, s in enumerate(sources, 1):
        head = str(s.get("title", ""))
        if s.get("heading"):
            head += " > " + str(s["heading"])
        lines.append(f"[{i}] 来源：{head}")
        lines.append("    " + str(s.get("content", ""))[:500].replace("\n", " "))
    return "\n".join(lines)


def source_meta(sources: list[dict]) -> list[dict]:
    return [
        {
            "ref": i,
            "id": s.get("id"),
            "source_type": s.get("source_type"),
            "source_id": s.get("source_id"),
            "project_id": s.get("project_id"),
            "title": s.get("title", ""),
            "heading": s.get("heading", ""),
            "excerpt": str(s.get("content", ""))[:180],
            "score": round(float(s.get("score", 0)), 2),
        }
        for i, s in enumerate(sources, 1)
    ]


if __name__ == "__main__":
    if "--reindex-all" in sys.argv:
        result = reindex_all()
        print(f"reindexed projects={result['projects']} memory={result['memory']}")
    else:
        print("usage: python -m app.knowledge --reindex-all")
