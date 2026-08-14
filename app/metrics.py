"""指标记录 —— 命中率/成本/延迟的权威统计（纯真实数据，无假数据）。

指标准确性（重要）：
- hit/miss tokens 来自 DeepSeek 响应 usage 字段（服务端权威），真实准确
- 成本 = 分模型定价 × 汇率（估算，价格以官网为准，含峰值时段政策不确定性）
- 延迟含网络流式传输时间（近似）
- 推理 token 按输出价计费，此处成本估算未包含（输出价远高于输入，后续补）
"""
from __future__ import annotations

from . import db

# 2026-07 起官方价目（USD/百万 token），汇率 7.3 为估算占位
_PRICING = {
    "deepseek-v4-flash": {"hit": 0.0028, "miss": 0.14},
    "deepseek-v4-pro": {"hit": 0.003625, "miss": 0.435},
}
_FX = 7.3


def _est_cost(model: str, hit: int, miss: int) -> float:
    p = _PRICING.get(model, _PRICING["deepseek-v4-flash"])
    return round((hit * p["hit"] + miss * p["miss"]) * _FX / 1_000_000, 5)


def record(
    usage: dict,
    model: str,
    latency_ms: int,
    cache_invalidated: bool = False,
    conversation_id: str | None = None,
) -> None:
    """落一条真实请求日志（SQLite），归属到所属会话。"""
    miss = usage.get("prompt_cache_miss_tokens", 0)
    hit = usage.get("prompt_cache_hit_tokens", 0)
    prompt = usage.get("prompt_tokens") or (hit + miss)
    if not prompt:
        return
    db.add_request_log(
        model=model,
        prompt_tokens=prompt,
        hit_tokens=hit,
        miss_tokens=miss,
        reasoning_tokens=usage.get("reasoning_tokens", 0),
        latency_ms=latency_ms,
        cost_cny=_est_cost(model, hit, miss),
        cache_invalidated=cache_invalidated,
        conversation_id=conversation_id,
    )


def get_summary(conversation_id: str | None = None) -> dict:
    """真实请求统计（可过滤到单个会话；没数据时各指标为 0/空）。"""
    return {
        **db.get_request_stats(conversation_id),
        "series": db.get_series(conversation_id=conversation_id),
        "source": "live",
    }


def get_requests(conversation_id: str | None = None) -> dict:
    rows = db.list_request_logs(limit=30, conversation_id=conversation_id)
    items = []
    for r in rows:
        hit_rate = r["hit_tokens"] / r["prompt_tokens"] if r["prompt_tokens"] else 0.0
        items.append({**r, "hit_rate": round(hit_rate, 4), "ts": r["ts"][-8:]})
    return {"items": items}
