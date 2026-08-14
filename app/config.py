"""设置存取 —— data/settings.json（已 gitignore；API Key 永不进代码与日志）。

要点：
- save() 与现有设置合并（局部更新不丢其他字段）
- 打码的 key（含 …）不会被存回去（防 UI 回显覆盖真实 key）
"""
from __future__ import annotations

import json
from pathlib import Path

from . import secrets

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SETTINGS_FILE = DATA_DIR / "settings.json"

DEFAULTS: dict = {
    "api_key": "",
    "base_url": "https://api.deepseek.com",
    "default_model": "deepseek-v4-flash",
    "soul": "你叫 Gili，是用户专属的 AI 伙伴。用中文回复，风格友好、直接、不啰嗦。",
    "skills": {},
    "search_provider": "deepseek",
    "tavily_api_key": "",
    "persona_model": "",
    "persona_api_key": "",
    "persona_base_url": "",
}

_MASK_MARK = "…"
_MASK_KEYS = ("api_key", "tavily_api_key", "persona_api_key")
_SECRET_KEYS = ("api_key", "tavily_api_key", "persona_api_key")

# 2026-07-24 起弃用的旧模型名（仍临时路由到 v4-flash 对应模式），自动迁移
_LEGACY_MODELS = {"deepseek-chat", "deepseek-reasoner"}


def load() -> dict:
    if not SETTINGS_FILE.exists():
        return dict(DEFAULTS)
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return dict(DEFAULTS)
        if data.get("default_model") in _LEGACY_MODELS:
            data["default_model"] = DEFAULTS["default_model"]
        merged = {**DEFAULTS, **data}
        # 空值回退默认（base_url 等等）
        if not merged.get("base_url"):
            merged["base_url"] = DEFAULTS["base_url"]
        for k in _SECRET_KEYS:
            if merged.get(k):
                merged[k] = secrets.decrypt(merged[k])
        return merged
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULTS)


def save(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    old = load()
    # 防打码覆盖：UI 回传的 key 若含省略号说明是打码后的展示值，保留旧的
    for k in _MASK_KEYS:
        if isinstance(data.get(k), str) and _MASK_MARK in data[k] and old.get(k):
            data = {**data, k: old[k]}
    merged = {**old, **data}
    # 空值 = 保留旧密钥；显式清除用 "__clear__"；新值落盘前加密
    for k in _SECRET_KEYS:
        if data.get(k) == "__clear__":
            merged[k] = ""
        elif not data.get(k) and old.get(k):
            merged[k] = old[k]
        elif merged.get(k):
            merged[k] = secrets.encrypt(merged[k])
    SETTINGS_FILE.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def mask_key(key: str) -> str:
    """回显打码：sk-abc…wxyz。"""
    if not key:
        return ""
    return key[:6] + "…" + key[-4:]
