"""对话人格画像：大五 + 行为维八维（docs/13 设计）。

- 默认关闭、会话独立；开启后每 20 轮后台分析一次
- 关闭即删除该会话全部快照（"关闭就没"）
- 画像不进对话提示词（v1 纯展示）；界面明示"非临床测评"
- 独立模型：设置 persona_api_key/base_url/model（默认复用主 key + v4-flash）
"""
from __future__ import annotations

import json
import logging
import re

from . import db, llm
from .schemas import ChatParams

log = logging.getLogger(__name__)

INTERVAL = 20  # 每 N 轮分析一次

# (key, 中文名, 高分端, 低分端)
DIMS: list[tuple[str, str, str, str]] = [
    ("openness", "开放性", "好奇探索、爱新想法、抽象思考", "务实、偏好熟悉、具体落地"),
    ("conscientiousness", "尽责性", "条理自律、目标明确、计划性强", "随性灵活、顺其自然"),
    ("extraversion", "外向性", "热情健谈、乐于互动、从交流中获得能量", "内敛安静、偏好独处、表达克制"),
    ("agreeableness", "宜人性", "合作体贴、易共情、优先考虑他人", "直率有主见、敢于反对、竞争意识"),
    ("stability", "情绪稳定性", "情绪平稳、抗压、不内耗", "敏感、易焦虑、情绪起伏明显"),
    ("verbosity", "表达详尽度", "长篇展开、喜欢解释原理", "简洁直接、点到为止"),
    ("questioning", "主动质疑", "频繁追问、挑战结论", "接受为主、跟随引导"),
    ("humor", "幽默戏谑", "爱开玩笑、反讽、玩梗", "严肃正式、就事论事"),
]

DIM_KEYS = [d[0] for d in DIMS]


def state_view(cid: str) -> dict:
    """前端视图：开关状态 + 轮数 + 快照列表。"""
    st = db.get_persona_state(cid)
    enabled = bool(st and st.get("enabled"))
    turns = int(st["user_turns"]) if st else 0
    snaps = db.list_persona_snapshots(cid)
    items = []
    for s in snaps:
        try:
            dims = json.loads(s["dims"])
            evidence = json.loads(s.get("evidence") or "[]")
        except (json.JSONDecodeError, TypeError):
            continue
        items.append({
            "id": s["id"],
            "turn_no": s["turn_no"],
            "dims": dims,
            "evidence": evidence,
            "summary": s.get("summary") or "",
            "model": s.get("model") or "",
            "created_at": s.get("created_at") or "",
        })
    return {
        "enabled": enabled,
        "turns": turns,
        "interval": INTERVAL,
        "next_in": (INTERVAL - turns % INTERVAL) if enabled else INTERVAL,
        "snapshots": items,
    }


def toggle(cid: str, enabled: bool) -> dict:
    """开关：开启从零计轮；关闭删除全部快照（"关闭就没"）。"""
    if enabled:
        db.set_persona_enabled(cid, True)
    else:
        db.delete_persona(cid)
    return state_view(cid)


def count_turn(cid: str) -> int:
    """每轮用户消息后调用（仅开启时计数）；返回当前累计轮数。"""
    return db.incr_persona_turns(cid)


def should_analyze(cid: str) -> bool:
    st = db.get_persona_state(cid)
    if not st or not st.get("enabled"):
        return False
    return int(st["user_turns"]) > 0 and int(st["user_turns"]) % INTERVAL == 0


def _make_providers(settings: dict) -> list[tuple[object, str]]:
    """画像专用 provider 链：独立 key 优先，失败回退主 key。"""
    out: list[tuple[object, str]] = []
    model = (settings.get("persona_model") or "deepseek-v4-flash").strip()
    if settings.get("persona_api_key"):
        base = (settings.get("persona_base_url") or "https://api.deepseek.com").strip()
        out.append((llm.DeepSeekProvider(settings["persona_api_key"], base), model))
    main = llm.make_provider(settings)
    if main.name == "deepseek":
        out.append((main, model))
    return out


async def analyze(cid: str, settings: dict, _providers=None) -> dict | None:
    """分析本会话 → 落库快照。失败（含坏 JSON）返回 None 不落库。

    _providers：测试注入用（[(provider, model), ...]），生产路径为 None。
    """
    st = db.get_persona_state(cid)
    if not st or not st.get("enabled"):
        return None
    # 素材：全部用户消息（截断）+ 上一快照先验
    msgs = [m for m in db.get_messages(cid, asc=True) if m["role"] == "user"]
    transcript = "\n".join(
        f"用户：{str(m['content'])[:300]}" for m in msgs[-80:]
    )
    if not transcript.strip():
        return None
    snaps = db.list_persona_snapshots(cid)
    prior = ""
    if snaps:
        prev = snaps[-1]
        prior = (
            f"上一份画像（第{prev['turn_no']}轮，作为先验平滑演变）：\n"
            f"维度：{prev['dims']}\n概述：{prev.get('summary') or ''}"
        )

    dim_desc = "\n".join(
        f"{k}（{name}）：高分={hi}；低分={lo}" for k, name, hi, lo in DIMS
    )
    system = (
        "你是对话风格画像分析师。根据用户在本对话中的发言，估计其大五人格倾向（0-100 整数，50=中性）。\n"
        "只基于可观察文本，不猜测无法从对话推断的维度；每个维度给 1 条最典型的原文证据（quote 为原文摘录）。\n"
        f"维度定义：\n{dim_desc}\n"
        "只输出 JSON：{\"dims\":{\"openness\":62,...8个维度},\"evidence\":[{\"dim\":\"openness\",\"quote\":\"…\"}],\"summary\":\"一句话概述\"}"
    )
    user = f"对话记录：\n{transcript[:6000]}\n\n{prior}"
    params = ChatParams(thinking_enabled=False, temperature=0.2, max_tokens=1200)

    last_err: Exception | None = None
    providers = _providers or _make_providers(settings)
    for provider, model in providers:
        try:
            resp = await provider.complete(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                params,
            )
            data = _parse_json(resp.get("content") or "")
            if not isinstance(data, dict) or not isinstance(data.get("dims"), dict):
                last_err = ValueError("画像输出无 dims")
                continue
            dims = _normalize_dims(data["dims"])
            evidence = _normalize_evidence(data.get("evidence"))
            summary = str(data.get("summary") or "")[:200]
            snap = db.add_persona_snapshot(
                cid, int(st["user_turns"]),
                json.dumps(dims, ensure_ascii=False),
                json.dumps(evidence, ensure_ascii=False),
                summary, model,
            )
            log.info("[persona] %s 第%s轮快照完成 model=%s", cid, st["user_turns"], model)
            return {**snap, "dims": dims, "evidence": evidence, "summary": summary}
        except Exception as e:  # noqa: BLE001 —— 独立 key 失败回退主 provider
            log.warning("[persona] provider 失败，尝试下一个: %s: %s", type(e).__name__, e)
            last_err = e
    if last_err:
        log.warning("[persona] 分析失败: %s: %s", type(last_err).__name__, last_err)
    return None


def _normalize_dims(raw: dict) -> dict[str, int]:
    out: dict[str, int] = {}
    for k in DIM_KEYS:
        try:
            v = int(raw.get(k, 50))
        except (TypeError, ValueError):
            v = 50
        out[k] = max(0, min(100, v))
    return out


def _normalize_evidence(raw) -> list[dict]:
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    for e in raw[:8]:
        if not isinstance(e, dict):
            continue
        dim = str(e.get("dim") or "")
        if dim not in DIM_KEYS:
            continue
        quote = str(e.get("quote") or "").strip()[:200]
        if quote:
            out.append({"dim": dim, "quote": quote})
    return out


def _parse_json(raw: str) -> dict | None:
    text = (raw or "").strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None
