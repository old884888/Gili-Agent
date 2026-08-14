"""联网搜索后端：DuckDuckGo（免费免 key，默认）/ Tavily（需 key，设置可配）。

结果格式统一为 [{title, url, snippet}]，失败返回 None（调用方给友好提示）。
工具执行结果由 main.py 注入【波动区】（缓存关键：结果永远在提示词尾部）。
"""
from __future__ import annotations

import json
import urllib.request
from typing import Optional


def search_web(
    query: str, provider: str = "duckduckgo", tavily_key: str = ""
) -> Optional[list[dict]]:
    try:
        if provider == "tavily" and tavily_key:
            return _tavily(query, tavily_key)
        return _duckduckgo(query)
    except Exception as e:  # noqa: BLE001
        print(f"[search] {provider}: {type(e).__name__}: {e}")
        return None


def _duckduckgo(query: str, max_results: int = 5) -> list[dict]:
    from ddgs import DDGS  # pip install ddgs

    with DDGS() as d:
        rows = list(d.text(query, max_results=max_results))
    return [
        {"title": r.get("title", ""), "url": r.get("href", ""), "snippet": r.get("body", "")}
        for r in rows
    ]


def _tavily(query: str, key: str, max_results: int = 5) -> list[dict]:
    req = urllib.request.Request(
        "https://api.tavily.com/search",
        data=json.dumps(
            {"api_key": key, "query": query, "max_results": max_results, "search_depth": "basic"}
        ).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read())
    return [
        {"title": x.get("title", ""), "url": x.get("url", ""), "snippet": x.get("content", "")}
        for x in data.get("results", [])
    ]


def format_results(results: list[dict], query: str, max_chars: int = 2000) -> str:
    """工具结果文本：截断保护（进波动区时控制体积）。"""
    lines = [f"【联网搜索结果】查询：{query}"]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']}\n   {r['url']}\n   {r['snippet'][:300]}")
    return "\n".join(lines)[:max_chars]
