"""联网搜索后端（v0.20 重构，借鉴 DSH web 体系：多源降级 + 页面抓取）。

- deepseek（默认）：DeepSeek 官方原生搜索——Anthropic 兼容端点 + web_search_20250305
  服务端工具，复用现有 API Key（零新增密钥）；服务端执行搜索，返回结构化来源
  （title/url）+ 模型综合答案文本。国内可用，不受 DuckDuckGo 访问限制影响。
- tavily：需要 key（设置可配）。
- duckduckgo：免费兜底（ddgs 爬虫，境内不稳定，仅作最后降级）。

统一返回 {"sources": [{title, url, snippet}], "answer": str}；
fetch_url 抓取页面正文（借鉴 DSH web_fetch：HTML→文本、非 2xx 报状态、超时保护）。
"""
from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from typing import Optional

log = logging.getLogger(__name__)

_TIMEOUT = 20
_DS_BASE = "https://api.deepseek.com/anthropic/v1"
_DS_MODEL = "deepseek-v4-flash"


def search_web(
    query: str,
    provider: str = "deepseek",
    tavily_key: str = "",
    api_key: str = "",
) -> dict:
    """多源降级链：deepseek（有 key）→ tavily（有 key）→ duckduckgo。"""
    if provider == "deepseek" and api_key:
        try:
            return _deepseek(query, api_key)
        except Exception as e:  # noqa: BLE001 —— 降级不中断
            log.warning("[search] deepseek 失败，降级: %s: %s", type(e).__name__, e)
    if provider == "tavily" and tavily_key:
        try:
            return {"sources": _tavily(query, tavily_key), "answer": ""}
        except Exception as e:  # noqa: BLE001
            log.warning("[search] tavily 失败，降级: %s: %s", type(e).__name__, e)
    try:
        return {"sources": _duckduckgo(query), "answer": ""}
    except Exception as e:  # noqa: BLE001
        log.warning("[search] %s: %s: %s", provider, type(e).__name__, e)
        return {"sources": [], "answer": ""}


def _deepseek(query: str, key: str) -> dict:
    """DeepSeek 原生搜索：Messages 端点 + web_search_20250305 工具。

    响应结构（实测 2026-08）：
    - web_search_tool_result 块：content[] 含 {type:web_search_result, title, url, encrypted_content}
    - text 块：模型基于搜索结果的综合答案（正文在服务端解密，无需本地处理）
    """
    body = {
        "model": _DS_MODEL,
        "max_tokens": 2000,
        "messages": [{"role": "user", "content": [{"type": "text", "text": query}]}],
        "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
    }
    req = urllib.request.Request(
        _DS_BASE + "/messages",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "x-api-key": key,
            "authorization": "Bearer " + key,
            "anthropic-version": "2023-06-01",
        },
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    blocks = data.get("content") or []
    sources: list[dict] = []
    seen: set[str] = set()
    for b in blocks:
        if b.get("type") == "web_search_tool_result":
            for item in b.get("content") or []:
                if item.get("type") != "web_search_result":
                    continue
                url = str(item.get("url") or "")
                if not url or url in seen:
                    continue
                seen.add(url)
                sources.append({
                    "title": str(item.get("title") or ""),
                    "url": url,
                    "snippet": "",
                })
    answer_parts = [
        b.get("text") for b in blocks if b.get("type") == "text" and b.get("text")
    ]
    return {"sources": sources[:8], "answer": "".join(answer_parts).strip()[:4000]}


def _duckduckgo(query: str, max_results: int = 5) -> list[dict]:
    from ddgs import DDGS  # pip install ddgs

    with DDGS() as d:
        rows = list(d.text(query, max_results=max_results))
    return [
        {"title": r.get("title") or "", "url": r.get("href") or "", "snippet": r.get("body") or ""}
        for r in rows
    ]


def _tavily(query: str, key: str, max_results: int = 5) -> list[dict]:
    body = {"api_key": key, "query": query, "max_results": max_results}
    req = urllib.request.Request(
        "https://api.tavily.com/search",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return [
        {"title": r.get("title") or "", "url": r.get("url") or "", "snippet": r.get("content") or ""}
        for r in data.get("results", [])
    ]


def fetch_url(url: str, max_chars: int = 3000) -> str:
    """抓取网页正文（借鉴 DSH web_fetch：HTML→文本、非 2xx 报状态、超时保护）。"""
    if not re.match(r"^https?://", (url or "").strip()):
        return "无效 URL（需要 http/https 开头）"
    req = urllib.request.Request(url.strip(), headers={"User-Agent": "Mozilla/5.0 (GiliAgent/0.20)"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            if resp.status >= 400:
                return f"抓取失败：HTTP {resp.status}"
            raw = resp.read(409600)
            charset = resp.headers.get_content_charset() or "utf-8"
            html = raw.decode(charset, errors="replace")
    except Exception as e:  # noqa: BLE001
        return f"抓取失败：{type(e).__name__}: {e}"
    return _html_to_text(html)[:max_chars]


def _html_to_text(html: str) -> str:
    """HTML → 可读纯文本（去脚本/样式/标签/实体，保留段落结构）。"""
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?is)<br\s*/?>|</p>|</div>|</h[1-6]>|</li>|</tr>", "\n", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = re.sub(r"&#?\w+;", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def format_results(result: dict, query: str, max_chars: int = 3000) -> str:
    """搜索结果 → 注入波动区的文本块（综合答案 + 完整来源列表）。

    来源列表永远完整保留（引用编号是模型的义务），综合答案按剩余预算截断。
    """
    sources = result.get("sources") or []
    answer = (result.get("answer") or "").strip()
    if not answer and not sources:
        return ""
    src_lines: list[str] = []
    if sources:
        src_lines.append("来源（引用处用 [编号] 标注）：")
        for i, s in enumerate(sources[:8], 1):
            label = s.get("title") or s.get("url") or ""
            src_lines.append(f"[{i}] {label}")
            if s.get("url"):
                src_lines.append(f"    {s['url']}")
            if s.get("snippet"):
                src_lines.append(f"    {s['snippet'][:200]}")
    src_text = "\n".join(src_lines)
    budget = max_chars - len(src_text) - 30
    if answer and budget >= 200:
        answer = answer[:budget]
        return ("【搜索结果综合】" + answer + "\n\n" + src_text)[:max_chars]
    return src_text[:max_chars]
