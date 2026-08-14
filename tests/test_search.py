# -*- coding: utf-8 -*-
"""v0.20 搜索格式化与页面抓取。"""
from app import search


def test_format_keeps_sources_complete():
    result = {
        "answer": "A" * 5000,  # 超长答案
        "sources": [
            {"title": "来源1", "url": "https://a.com", "snippet": "片段1"},
            {"title": "来源2", "url": "https://b.com", "snippet": ""},
        ],
    }
    text = search.format_results(result, "q")
    assert len(text) <= 3000
    assert "来源（引用处用 [编号] 标注）" in text
    assert "[1] 来源1" in text and "[2] 来源2" in text
    assert "https://a.com" in text and "https://b.com" in text


def test_format_empty():
    assert search.format_results({"sources": [], "answer": ""}, "q") == ""


def test_fetch_invalid_url():
    assert "无效" in search.fetch_url("not-a-url")
    assert "无效" in search.fetch_url("ftp://x")


def test_html_to_text():
    html = "<html><head><script>var a=1;</script><style>.x{}</style></head>" \
           "<body><p>第一段</p><p>第二段<br>换行</p></body></html>"
    text = search._html_to_text(html)
    assert "第一段" in text and "第二段" in text and "换行" in text
    assert "var a" not in text and ".x{" not in text
