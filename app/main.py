"""Gili Agent —— FastAPI 入口。

v0.5（核心阶段）：
- SQLite 落库（会话/消息/记忆/请求日志，见 app/db.py）
- PromptBuilder 三区完整实现：稳定区+低频区+波动区，前缀指纹守卫
- 记忆摘要低频重写：未摘录消息超阈值 → 后台 LLM 压缩 → 低频区更新
- 指标真实化：真实 usage 落库统计，无真实数据时演示数据打标

缓存失效记账：前缀指纹变化（技能切换/摘要重写/记忆编辑）→ 请求日志打标。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, knowledge, learning_skill, llm, log as logmod, memory, metrics, prompt_builder, search, skills, skills_market
from .schemas import ChatParams, ChatRequest, MemoryItem

# 联网搜索工具定义（进稳定区：开启技能时随请求发送，顺序固定）
WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "搜索互联网获取实时信息。用户需要最新资讯、新闻、实时数据或事实查证时使用。",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "搜索关键词"}},
            "required": ["query"],
        },
    },
}

# 网页抓取工具（借鉴 DSH web_fetch：把指定页面的正文喂给模型）
WEB_FETCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_fetch",
        "description": "抓取指定网页 URL 的正文内容。用户给出链接想了解详情、或搜索结果需要展开阅读时使用。",
        "parameters": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "完整网页地址（http/https）"}},
            "required": ["url"],
        },
    },
}

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
UPLOADS_DIR = BASE_DIR / "data" / "uploads"
log = logging.getLogger(__name__)
_UPLOAD_NAME_RE = re.compile(r"^f[0-9a-f]{8}\.(txt|json)$")


def _cleanup_uploads(max_age_days: int = 7) -> None:
    """启动时清理：过期附件 + 历史版本遗留的孤儿原文件（现仅保留 .txt/.json）。"""
    try:
        if not UPLOADS_DIR.exists():
            return
        cutoff = time.time() - max_age_days * 86400
        removed = 0
        for p in UPLOADS_DIR.iterdir():
            if not p.is_file():
                continue
            if p.stat().st_mtime < cutoff or not _UPLOAD_NAME_RE.fullmatch(p.name):
                try:
                    p.unlink()
                    removed += 1
                except OSError:
                    pass
        if removed:
            log.info("[uploads] 启动清理 %d 个过期/孤儿附件", removed)
    except OSError:
        pass


app = FastAPI(title="Gili Agent", version="0.15.2")
logmod.setup_logging()
db.init()
_cleanup_uploads()
_s = config.load()
log.info("Gili Agent 启动 v0.15.2 mode=%s", "real" if _s.get("api_key") else "prototype")

# 会话级前缀指纹：指纹变化 = 缓存失效（该请求的日志打标）
_prefix_fps: dict[str, str] = {}

# 最近一次路由消息快照（cid → (messages, model)）：供后台摘要请求复用 warm 前缀
_last_routed: dict[str, tuple[list[dict], str]] = {}


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


# ---------- 基础 ----------

@app.get("/api/health")
async def health():
    settings = config.load()
    mode = "real" if settings.get("api_key") else "prototype"
    return {"status": "ok", "mode": mode, "version": "0.15.2"}


@app.get("/api/config")
async def get_config():
    """可用模型与参数范围（UI 面板据此渲染）+ 技能定义。"""
    return {
        "models": [
            {"id": "deepseek-v4-pro", "name": "DeepSeek V4 Pro（旗舰 · 1M 上下文）"},
            {"id": "deepseek-v4-flash", "name": "DeepSeek V4 Flash（轻快 · 1M 上下文）"},
        ],
        "params": {
            "temperature": {"min": 0, "max": 2, "step": 0.1},
            "top_p": {"min": 0, "max": 1, "step": 0.05},
            "max_tokens": {"min": 1, "max": 131072, "step": 128},
            "presence_penalty": {"min": -2, "max": 2, "step": 0.1},
            "frequency_penalty": {"min": -2, "max": 2, "step": 0.1},
        },
        "memory": {
            "history_token_budget": {"min": 500, "max": 131072, "step": 500},
            "summary_rewrite_threshold": {"min": 1000, "max": 131072, "step": 1000},
        },
        "skills": skills.to_public(),
    }


# ---------- 设置 ----------

@app.get("/api/settings")
async def get_settings():
    s = config.load()
    return {
        **s,
        "api_key": config.mask_key(s.get("api_key", "")),
        "tavily_api_key": config.mask_key(s.get("tavily_api_key", "")),
    }


@app.put("/api/settings")
async def put_settings(body: dict):
    config.save(body)
    return {"status": "ok"}


@app.post("/api/settings/reveal-key")
async def reveal_key(body: dict):
    """点击「应用」时解密已保存密钥，回填输入框（不回显在列表接口）。"""
    field = str((body or {}).get("field") or "")
    if field not in ("api_key", "tavily_api_key"):
        raise HTTPException(status_code=400, detail="字段不支持")
    return {"value": config.load().get(field, "")}


@app.get("/api/skills/market")
async def get_skills_market():
    """更多技能：内置 + GitHub 镜像下载目录。"""
    installed = set(skills._user_skills().keys())
    return {"items": skills_market.public_catalog(installed)}


@app.post("/api/skills/install")
async def install_skill(body: dict):
    """下载并安装技能（GitHub 条目按镜像顺序尝试）。"""
    entry_id = str((body or {}).get("id") or "")
    entry = skills_market.get(entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="技能不存在")
    text, fallback = await asyncio.to_thread(skills_market.download_skill, entry)
    meta = skills_market.parse_meta(entry, text)
    pdir = skills_market.SKILLS_DIR / entry_id
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "SKILL.md").write_text(text, encoding="utf-8")
    skills.save_user_skill({
        "id": entry_id,
        "name": meta["name"],
        "desc": meta["desc"],
        "prompt_fragment": text,
        "source": entry.get("source", ""),
        "repo": entry.get("repo", ""),
    })
    settings = config.load()
    settings.setdefault("skills", {})[entry_id] = True
    config.save(settings)
    return {"status": "ok", "name": meta["name"], "id": entry_id, "fallback": fallback}


@app.post("/api/skills/uninstall")
async def uninstall_skill(body: dict):
    """卸载已安装的用户技能：移除开关、注册表和本地文件。"""
    entry_id = str((body or {}).get("id") or "")
    if entry_id not in skills._user_skills():
        raise HTTPException(status_code=404, detail="技能未安装")
    skills.remove_user_skill(entry_id)
    settings = config.load()
    settings.get("skills", {}).pop(entry_id, None)
    config.save(settings)
    import shutil as _shutil
    _shutil.rmtree(skills_market.SKILLS_DIR / entry_id, ignore_errors=True)
    return {"status": "ok"}


@app.post("/api/settings/test")
async def test_settings(body: dict):
    """测试 DeepSeek 连接：调 /models 接口（0 token 消耗）。"""
    key = (body.get("api_key") or "").strip()
    base_url = (body.get("base_url") or "").strip()
    # 防打码覆盖：前端传了打码后的展示值 → 用已保存的 key
    saved = config.load()
    if config._MASK_MARK in key:
        key = saved.get("api_key", "")
    if not key.strip():
        return {"ok": False, "error": "请先填写 DeepSeek API Key"}
    if not base_url:
        base_url = (saved.get("base_url") or "").strip() or "https://api.deepseek.com"
    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=key, base_url=base_url)
        models = await client.models.list()
        return {"ok": True, "models": [m.id for m in models.data][:6]}
    except Exception as e:  # noqa: BLE001 —— 把原始错误回给用户便于排查
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ---------- 会话 ----------

@app.get("/api/conversations")
async def list_conversations():
    return {"items": db.list_conversations()}


@app.post("/api/conversations")
async def create_conversation():
    return db.create_conversation()


@app.get("/api/conversations/{cid}/messages")
async def get_messages(cid: str):
    if not db.get_conversation(cid):
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"items": db.get_ui_messages(cid)}


@app.put("/api/messages/{mid}")
async def edit_message(mid: str, body: dict):
    """编辑消息内容（改写后历史前缀变化 → 下轮请求自动打 ⚠）。"""
    m = db.get_message(mid)
    if not m:
        raise HTTPException(status_code=404, detail="消息不存在")
    content = (body.get("content") or "").strip()
    if not content:
        return {"status": "error", "detail": "内容不能为空"}
    db.update_message_content(mid, content)
    return {"status": "ok"}


@app.delete("/api/messages/{mid}")
async def delete_message(mid: str):
    """删除气泡：整组版本一起删（不级联删除后续消息）。"""
    m = db.get_message(mid)
    if not m:
        raise HTTPException(status_code=404, detail="消息不存在")
    db.delete_version_group(m["version_group"] or m["id"])
    return {"status": "ok"}


@app.get("/api/messages/{mid}/versions")
async def message_versions(mid: str):
    """版本组全部版本（含思考链），供前端切换查看。"""
    m = db.get_message(mid)
    if not m:
        raise HTTPException(status_code=404, detail="消息不存在")
    group = m["version_group"] or m["id"]
    return {"items": db.list_versions(group), "active_version": m["version_no"]}


@app.put("/api/conversations/{cid}")
async def rename_conversation(cid: str, body: dict):
    if not db.get_conversation(cid):
        raise HTTPException(status_code=404, detail="会话不存在")
    title = (body.get("title") or "").strip()
    if not title:
        return {"status": "error", "detail": "标题不能为空"}
    db.rename_conversation(cid, title[:40])
    return {"status": "ok", "title": title[:40]}


@app.post("/api/conversations/link-memory")
async def link_memory(body: dict | None = None):
    """把 2+ 个对话连入同一记忆组（多选卡片后点「连接记忆」）。"""
    ids = [str(x) for x in ((body or {}).get("ids") or [])]
    ids = [i for i in dict.fromkeys(ids) if db.get_conversation(i)]
    if len(ids) < 2:
        raise HTTPException(status_code=400, detail="至少选择两个对话")
    gid = db.link_memory_group(ids)
    return {"status": "ok", "group_id": gid, "count": len(ids)}


@app.post("/api/conversations/{cid}/unlink-memory")
async def unlink_memory(cid: str):
    """取消单个对话的记忆共享（组内只剩一个成员时自动解散）。"""
    if not db.get_conversation(cid):
        raise HTTPException(status_code=404, detail="会话不存在")
    db.unlink_memory_group(cid)
    return {"status": "ok"}


@app.post("/api/conversations/{cid}/auto-title")
async def auto_title(cid: str):
    """AI 起标题：LLM 概括对话 → 存为标题。"""
    if not db.get_conversation(cid):
        raise HTTPException(status_code=404, detail="会话不存在")
    settings = config.load()
    provider = llm.make_provider(settings)
    if provider.name != "deepseek":
        return {"ok": False, "detail": "原型模式：AI 起标题需要先配置 API Key"}
    title = await _gen_title(provider, cid)
    if not title:
        return {"ok": False, "detail": "起标题失败，请重试"}
    db.rename_conversation(cid, title)
    return {"ok": True, "title": title}


@app.delete("/api/conversations/{cid}")
async def delete_conversation(cid: str):
    db.delete_conversation(cid)
    _prefix_fps.pop(cid, None)  # 清除前缀指纹，避免孤儿状态
    _last_routed.pop(cid, None)  # 清除路由快照
    learning_skill.forget(cid)  # 清除学习引导等会话级内存态
    return {"status": "ok"}


@app.post("/api/conversations/{cid}/favorite")
async def set_favorite(cid: str, body: dict):
    if not db.get_conversation(cid):
        raise HTTPException(status_code=404, detail="会话不存在")
    fav = bool(body.get("favorite", True))
    db.set_favorite(cid, fav)
    return {"status": "ok", "favorite": fav}


@app.post("/api/conversations/{cid}/export")
async def export_conversation(cid: str):
    """导出会话为 markdown 文件（exports/ 目录，可被 Obsidian 打开）。"""
    import re

    conv = db.get_conversation(cid)
    if not conv:
        raise HTTPException(status_code=404, detail="会话不存在")
    msgs = db.get_messages(cid)
    if not msgs:
        return {"ok": False, "detail": "没有可导出的消息"}
    lines = [
        f"# {conv['title']}",
        "",
        f"> 导出自 Gili Agent · {conv['created_at']}",
        "",
    ]
    for m in msgs:
        who = "你" if m["role"] == "user" else "Gili"
        lines += [f"**{who}**：{m['content']}", ""]
    exports = BASE_DIR / "exports"
    exports.mkdir(exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|]', "_", conv["title"] or "对话")[:40]
    fname = f"{safe}-{time.strftime('%Y%m%d-%H%M')}.md"
    path = exports / fname
    path.write_text("\n".join(lines), encoding="utf-8")
    return {"ok": True, "name": fname, "path": str(path)}


# ---------- 文件上传 ----------

_TEXT_SUFFIXES = {".txt", ".md", ".csv", ".json", ".jsonl", ".log", ".py", ".js", ".ts", ".jsx", ".tsx", ".html", ".css"}


def _extract_text(suffix: str, data: bytes) -> str:
    try:
        if suffix == ".pdf":
            import io
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            return "\n".join(p.extract_text() or "" for p in reader.pages)
        if suffix in _TEXT_SUFFIXES:
            return data.decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        log.warning("[upload] %s 提取失败: %s: %s", suffix, type(e).__name__, e)
    return ""


def _load_attachment(fid: str) -> tuple[str, str] | None:
    """读取上传附件 → (文件名, 提取文本)。"""
    meta_p = UPLOADS_DIR / f"{fid}.json"
    txt_p = UPLOADS_DIR / f"{fid}.txt"
    if not meta_p.exists() or not txt_p.exists():
        return None
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    return meta["name"], txt_p.read_text(encoding="utf-8")


@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    """上传文件并提取文本（供聊天附件使用，内容注入波动区）。"""
    name = (file.filename or "未命名").strip()
    suffix = Path(name).suffix.lower()
    data = await file.read()
    if len(data) > 5 * 1024 * 1024:
        return {"ok": False, "detail": "文件超过 5MB"}
    text = _extract_text(suffix, data)
    if not text.strip():
        return {"ok": False, "detail": "无法提取文本（支持 txt/md/csv/json/代码/PDF）"}
    fid = "f" + uuid.uuid4().hex[:8]
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    # 只落提取文本与元数据；原文件不再落盘（历史上写后从未读取，纯磁盘泄漏）
    (UPLOADS_DIR / f"{fid}.txt").write_text(text[:20000], encoding="utf-8")
    (UPLOADS_DIR / f"{fid}.json").write_text(
        json.dumps({"name": name, "suffix": suffix}), encoding="utf-8"
    )
    return {
        "ok": True,
        "file_id": fid,
        "name": name,
        "preview": text[:150],
        "tokens": prompt_builder.estimate_tokens(text[:20000]),
    }


# ---------- 记忆容量 ----------

@app.get("/api/memory/usage")
async def memory_usage(conversation_id: str | None = None, budget: int = 12000):
    """记忆容量统计：稳定/低频/波动三区 tokens + 存储概况（进度条与详情弹窗数据源）。"""
    settings = config.load()
    cid = conversation_id
    if not cid or not db.get_conversation(cid):
        convs = db.list_conversations()
        cid = convs[0]["id"] if convs else None

    est = prompt_builder.estimate_tokens
    soul = settings.get("soul", "")
    skill_fragments, _ = prompt_builder.build_skill_context(settings)
    stable = est(soul) + sum(est(f) for f in skill_fragments)
    summary, fixed_facts, retrieved_facts = memory.get_context(cid) if cid else ("", [], [])
    lowfreq = est(summary) + sum(m["tokens"] for m in fixed_facts)
    history = db.get_messages(cid, summarized=0) if cid else []
    volatile = sum(m["tokens"] for m in history) + sum(m["tokens"] for m in retrieved_facts)
    used = stable + lowfreq + volatile

    return {
        "conversation_id": cid,
        "budget": budget,
        "stable": stable,
        "lowfreq": lowfreq,
        "volatile": volatile,
        "used": used,
        "percent": min(100, round(used / budget * 100)) if budget else 0,
        "detail": {
            "soul_tokens": est(soul),
            "skill_fragments": len(skill_fragments),
            "summary_tokens": est(summary),
            "fixed_facts": len(fixed_facts),
            "retrieved_facts": len(retrieved_facts),
            "history_msgs": len(history),
            "history_tokens": volatile,
        },
        "store": {
            "messages": db._query("SELECT COUNT(*) AS n FROM messages")[0]["n"],
            "memory_items": db._query("SELECT COUNT(*) AS n FROM memory_items")[0]["n"],
            "conversations": db._query("SELECT COUNT(*) AS n FROM conversations")[0]["n"],
            "db_kb": round((BASE_DIR / "data" / "gili.db").stat().st_size / 1024, 1)
            if (BASE_DIR / "data" / "gili.db").exists() else 0,
        },
    }


async def _gen_title(provider, cid: str) -> str | None:
    """用 LLM 生成 ≤12 字标题（思考关闭 + 低温，快且省）。"""
    msgs = db.get_messages(cid)[-6:]
    if not msgs:
        return None
    transcript = "\n".join(
        f"{'用户' if m['role'] == 'user' else 'Gili'}：{m['content'][:100]}" for m in msgs
    )
    prompt_msgs = [
        {
            "role": "system",
            "content": "用不超过 12 个字概括这段对话的主题。直接输出标题本身，不要引号、不要句号、不要任何解释。",
        },
        {"role": "user", "content": transcript[:1500]},
    ]
    params = ChatParams(thinking_enabled=False, temperature=0.3, max_tokens=32)
    parts = []
    async for evt in provider.stream_chat(prompt_msgs, params):
        if evt["kind"] == "content":
            parts.append(evt["text"])
    title = "".join(parts).strip().strip('"\'“”').strip()
    return title[:30] or None


# ---------- 聊天 ----------

@app.post("/api/chat")
async def chat(req: ChatRequest):
    settings = config.load()
    provider = llm.make_provider(settings)

    # 重新生成模式：目标 AI 消息及其后的上下文全部截断，原位产出新版本
    regen = None
    if req.regenerate_message_id:
        regen = db.get_message(req.regenerate_message_id)
        if not regen or regen["role"] != "assistant":
            raise HTTPException(status_code=404, detail="消息不存在或不可重新生成")
        cid = regen["conversation_id"]
    else:
        cid = req.conversation_id
        if not cid or not db.get_conversation(cid):
            cid = db.create_conversation()["id"]

    # 学习模式拦截：本会话引导进行中 → 消息走状态机（不调 LLM），确认后实时生成文档
    if not regen and learning_skill.has(cid):
        return await _learning_intercept(cid, req.message)
    # 学习档案更新建议：有未确认的建议时，用户回答优先走档案更新
    if not regen and learning_skill.has_pending_update(cid):
        return await _update_intercept(cid, req.message)
    # 学习对话显式命令：开始/完成章节、记笔记，直接写档案（不调 LLM）
    if not regen:
        command = learning_skill.handle_command(cid, req.message)
        if command:
            db.add_message(cid, "user", req.message)
            db.add_message(cid, "assistant", command["content"])
            db.touch(cid)
            if command.get("command") == "organize":
                project = db.get_learning_project_by_conv(cid)
                if project:

                    async def _safe_organize():
                        try:
                            result = await learning_skill.organize_project_notes(project["id"], provider)
                            db.add_message(cid, "assistant", result["content"])
                        except Exception as e:  # noqa: BLE001
                            log.exception("[organize] %s: %s", type(e).__name__, e)
                            db.add_message(cid, "assistant", f"⚠️ 整理失败：{e}（笔记原文未受影响，可直接重试）")
                        db.touch(cid)

                    asyncio.create_task(_safe_organize())
            return JSONResponse({
                "mode": "learning_command",
                "status": command.get("status", "ok"),
                "command": command.get("command", ""),
            })
    if not regen:
        learning_skill._user_turns[cid] = learning_skill._user_turns.get(cid, 0) + 1

    skill_fragments, skill_volatile = prompt_builder.build_skill_context(settings)

    # 学习项目深度绑定：总览进低频区（可缓存），笔记摘要留在波动区
    learning_overview = ""
    learning_notes = ""
    lp_ctx = learning_skill.project_context(cid)
    if lp_ctx:
        learning_overview = learning_skill.overview_text(lp_ctx)
        learning_notes = learning_skill.notes_excerpts(lp_ctx)

    if regen:
        # 上下文 = 目标 AI 消息之前的未摘录消息；当前消息 = 之前的最后一条用户消息
        history, history_trimmed = db.get_context_messages(
            cid, req.memory.history_token_budget, before_rowid=regen["rowid"]
        )
        users = [m for m in history if m["role"] == "user"]
        if not users:
            raise HTTPException(status_code=400, detail="找不到可重新生成的上下文")
        current = {"role": "user", "content": users[-1]["content"]}
        version_group = regen["version_group"] or regen["id"]
        version_no = db.next_version_no(version_group)
    else:
        history, history_trimmed = db.get_context_messages(cid, req.memory.history_token_budget)
        current = {"role": "user", "content": req.message}
        if not current["content"].strip():
            raise HTTPException(status_code=400, detail="消息内容不能为空")
        version_group = version_no = None

    # 检索用词 = 当前消息（重新生成时用被重生成的那条用户消息）
    summary, fixed_facts, retrieved_facts = memory.get_context(
        cid, current["content"], recall=req.memory.enable_memory_recall
    )

    # 知识检索：学习文档 / 记忆命中 → 波动区尾部，回答带 [n] 引用
    kb_sources: list[dict] = []
    if req.memory.enable_kb_recall:
        lp_project = db.get_learning_project_by_conv(cid)
        kb_sources = await knowledge.search(
            current["content"], provider,
            project_id=lp_project["id"] if lp_project else None,
            conversation_id=cid,
            enable_rerank=req.memory.enable_rerank,
        )

    # 附件内容进【波动区】尾部（缓存只保它前面的前缀；每文件截断 4000 字）
    attach_text = ""
    for fid in req.attachments[:3]:
        att = _load_attachment(fid)
        if att:
            attach_text += f"\n\n【文件附件：{att[0]}】\n{att[1][:4000]}"
    volatile_text = learning_notes
    if skill_volatile:
        volatile_text = (volatile_text + "\n" if volatile_text else "") + skill_volatile
    if attach_text:
        volatile_text = (volatile_text + "\n" if volatile_text else "") + attach_text

    messages, fp = prompt_builder.build_prompt(
        soul=settings.get("soul", ""),
        skill_fragments=skill_fragments,
        summary=summary,
        memories=fixed_facts,  # 常驻 ⭐3 事实 → 低频区
        retrieved=retrieved_facts,  # 检索事实 → 波动区尾部
        history=history,
        current=current,
        volatile_text=volatile_text,
        lowfreq_text=learning_overview,
    )
    if kb_sources:
        # 知识引用挂绝对尾部（当前消息之后）：插中间会破坏下一轮的前缀命中
        messages.append({"role": "system", "content": knowledge.context_block(kb_sources)})

    # 前缀守卫（DSH 信封哈希）：稳定+低频 + model + tools 任一变化 = 缓存时代切换。
    # model 纳入是因为缓存域是 provider×model 级（v4-flash ↔ v4-pro 不共享缓存）。
    fp = hashlib.sha256(
        (fp + "|" + req.params.model + "|" + json.dumps(tools, sort_keys=True, ensure_ascii=False)).encode("utf-8")
    ).hexdigest()
    prev_fp = _prefix_fps.get(cid)
    cache_invalidated = (prev_fp is not None and prev_fp != fp) or history_trimmed
    _prefix_fps[cid] = fp

    # 联网技能开启 → 附带工具定义（稳定信封，随请求发送）；按名字稳定排序（DSH 纪律）
    tools: list[dict] = []
    if any(s["id"] == "search" for s in skills.enabled_skills(settings)):
        tools = sorted([WEB_SEARCH_TOOL, WEB_FETCH_TOOL], key=lambda t: t["function"]["name"])

    log.info("[chat] 开始 cid=%s model=%s regen=%s search=%s", cid, req.params.model, bool(regen), bool(tools))

    def _chunks(s: str, n: int = 6):
        for i in range(0, len(s), n):
            yield s[i : i + n]

    async def event_stream():
        start = time.time()
        yield _sse({"event": "start", "mode": provider.name, "conversation_id": cid})
        yield _sse({"event": "params", "params": req.params.model_dump()})
        raw = ""
        reasoning_text = ""
        msgs_for_llm = list(messages)
        needs_stream = True
        error_detail = None

        # 用户消息先落库：即使流式失败/断连，消息与 token 成本都不丢
        if not regen:
            db.add_message(cid, "user", req.message)

        try:
            # 联网技能开启：先非流式探测一轮 —— 需要工具则执行后进入流式回答；
            # 不需要工具则探测轮内容就是最终答案（避免二次调用导致内容重复）
            if tools:
                r1 = await provider.complete(msgs_for_llm, req.params, tools=tools)
                if r1.get("usage"):
                    metrics.record(
                        r1["usage"], req.params.model,
                        int((time.time() - start) * 1000), cache_invalidated,
                        conversation_id=cid,
                    )
                if r1["tool_calls"]:
                    # 工具结果进【波动区】尾部（缓存只保住它之前的前缀）
                    tool_msgs = [{
                        "role": "assistant",
                        "content": r1["content"] or "",  # DSH：空内容回传 ''（非 null），字节稳定
                        **({"reasoning_content": r1["reasoning_content"]}
                           if r1.get("reasoning_content") else {}),
                        "tool_calls": [
                            {"id": tc["id"], "type": "function",
                             "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                            for tc in r1["tool_calls"]
                        ],
                    }]
                    for tc in r1["tool_calls"]:
                        if tc["name"] == "web_fetch":
                            try:
                                url = (json.loads(tc["arguments"]) or {}).get("url", "")
                            except json.JSONDecodeError:
                                url = ""
                            content = search.fetch_url(url)
                            tool_msgs.append({
                                "role": "tool", "tool_call_id": tc["id"], "content": content,
                            })
                            yield _sse({"event": "tool", "query": url, "results": 0})
                            continue
                        try:
                            query = (json.loads(tc["arguments"]) or {}).get("query", "")
                        except json.JSONDecodeError:
                            query = ""
                        result = search.search_web(
                            query,
                            settings.get("search_provider", "deepseek"),
                            settings.get("tavily_api_key", ""),
                            settings.get("api_key", ""),
                        )
                        content = search.format_results(result, query) or (
                            f"搜索失败：无法获取「{query}」的结果，请告知用户稍后重试或换关键词。"
                        )
                        tool_msgs.append({
                            "role": "tool", "tool_call_id": tc["id"], "content": content,
                        })
                        yield _sse({
                            "event": "tool", "query": query,
                            "results": len(result.get("sources") or []),
                        })
                    msgs_for_llm = [*msgs_for_llm, *tool_msgs]
                else:
                    # 没触发工具：探测轮内容就是答案（分块补流式观感），不再二次调用
                    raw = r1["content"]
                    needs_stream = False
                    for ch in _chunks(raw):
                        yield _sse({"event": "delta", "kind": "content", "text": ch})

            # 保存本次实际路由的消息快照：后台摘要任务复用它做 prefix-extension（DSH 实践）
            _last_routed[cid] = (list(msgs_for_llm), req.params.model)

            if needs_stream:
                async for evt in provider.stream_chat(msgs_for_llm, req.params):
                    yield _sse({"event": "delta", "kind": evt["kind"], "text": evt["text"]})
                    if evt["kind"] == "content":
                        raw += evt["text"]
                    elif evt["kind"] == "reasoning":
                        reasoning_text += evt["text"]
                    await asyncio.sleep(0)
        except Exception as e:  # noqa: BLE001 —— 网络/API 失败：保留部分回答并通知前端
            log.exception("[chat-error] %s: %s", type(e).__name__, e)
            error_detail = f"请求失败：{e}"

        usage = provider.last_usage or {}
        latency = int((time.time() - start) * 1000)
        # 流式轮真实跑过才记这一轮 usage（探测轮已单独记账；两轮都真实花钱都入账）
        if usage and needs_stream:
            metrics.record(usage, req.params.model, latency, cache_invalidated, conversation_id=cid)

        # 落库（提示词已构建完，此时追加不影响本次请求）
        if regen:
            db.deactivate_group(version_group)
        new_msg = None
        if raw.strip() or not error_detail:
            new_msg = db.add_message(
                cid, "assistant", raw, reasoning=reasoning_text,
                version_group=version_group, version_no=version_no,
                sources=json.dumps(knowledge.source_meta(kb_sources), ensure_ascii=False)
                if kb_sources else None,
            )
        db.touch(cid)

        if error_detail:
            yield _sse({
                "event": "error", "detail": error_detail,
                "message_id": new_msg["id"] if new_msg else None,
            })
            return

        log.info(
            "[chat] 完成 cid=%s 用时%dms hit=%s miss=%s out=%s",
            cid, latency,
            usage.get("prompt_cache_hit_tokens", 0),
            usage.get("prompt_cache_miss_tokens", 0),
            usage.get("completion_tokens", 0),
        )
        yield _sse({
            "event": "done",
            "usage": usage,
            "cache_invalidated": cache_invalidated,
            "message_id": new_msg["id"],
            "version_group": version_group or new_msg["id"],
            "version_no": version_no or 1,
            "versions_count": db.count_versions(version_group or new_msg["id"]),
            "sources": knowledge.source_meta(kb_sources),
        })
        # 学习档案联动：判断是否学完某章，生成待确认建议气泡
        if not regen and provider.name == "deepseek" and db.get_learning_project_by_conv(cid):
            try:
                proposal = await learning_skill.propose_update(provider, cid)
                if proposal:
                    db.add_message(
                        cid, "assistant", proposal["content"],
                        quick_options=_opts_json(
                            proposal["quick_options"],
                            proposal.get("input", False),
                            proposal.get("placeholder", ""),
                        ),
                    )
                    yield _sse({"event": "proposal", "status": "ready"})
            except Exception as e:  # noqa: BLE001
                log.exception("[learning-propose] %s: %s", type(e).__name__, e)
        # AI 起标题：仅新对话首次（标题仍为默认值时）
        if provider.name == "deepseek" and db.get_conversation(cid)["title"] == "新对话":
            async def _safe_title():
                try:
                    if db.get_conversation(cid)["title"] != "新对话":
                        return  # 用户已手动改名则跳过
                    title = await _gen_title(provider, cid)
                    if title:
                        db.rename_conversation(cid, title)
                except Exception as e:  # noqa: BLE001
                    log.exception("[auto-title] %s: %s", type(e).__name__, e)

            asyncio.create_task(_safe_title())
        # 海马体：每 EXTRACT_INTERVAL 轮批量抽取事实（批量写 = 低频区少变）
        if provider.name == "deepseek" and db.count_unextracted_user_messages(cid) >= memory.EXTRACT_INTERVAL:
            async def _safe_extract():
                try:
                    r = await memory.extract_facts(cid, provider)
                    if any(r.values()):
                        log.info("[extract-facts] %s +%s ~%s -%s", cid, r["added"], r["updated"], r["deleted"])
                except Exception as e:  # noqa: BLE001
                    log.exception("[extract-facts] %s: %s", type(e).__name__, e)

            asyncio.create_task(_safe_extract())
        # 记忆摘要低频重写：超阈值 → 后台压缩（下次请求低频区即更新）。
        # 保留窗口随预算缩放（预算 × 0.35），预算越大压缩后保留的原文越多。
        keep_window = memory.keep_window_for_budget(req.memory.history_token_budget)
        if provider.name == "deepseek" and memory.should_summarize(
            cid, req.memory.summary_rewrite_threshold
        ):
            async def _safe_summarize():
                try:
                    await memory.summarize_conversation(cid, provider, keep_window, prefix=_last_routed.get(cid))
                except Exception as e:  # noqa: BLE001 —— 后台任务异常不打断主流程
                    log.exception("[auto-summarize] %s: %s", type(e).__name__, e)

            asyncio.create_task(_safe_summarize())

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ---------- 指标 ----------

@app.get("/api/metrics/summary")
async def metrics_summary(conversation_id: str | None = None):
    return metrics.get_summary(conversation_id)


@app.get("/api/metrics/requests")
async def metrics_requests(conversation_id: str | None = None):
    return metrics.get_requests(conversation_id)


# ---------- 记忆 ----------

@app.get("/api/memory")
async def get_memory():
    return {"items": db.list_memory()}


@app.get("/api/memory/search")
async def search_memory(q: str = ""):
    """按关键词检索事实条目（记忆面板搜索框）。"""
    q = q.strip()
    if not q:
        return {"items": []}
    facts = db.list_memory(kind="fact")
    found = memory.retrieve_facts(q, facts, limit_tokens=10**9)
    return {"items": found[:20]}


@app.post("/api/memory")
async def add_memory(item: MemoryItem):
    row = db.add_memory_item(item.layer, item.content, importance=item.importance)
    knowledge.reindex_memory(row["id"])
    return {"status": "ok", "item": row}


@app.put("/api/memory/{item_id}")
async def edit_memory(item_id: str, body: dict):
    """编辑记忆内容/重要度（低频区变化 → 前缀指纹变化 → 自动记账缓存失效）。"""
    item = db.get_memory_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="记忆不存在")
    content = body.get("content")
    importance = body.get("importance")
    if importance is not None:
        try:
            importance = int(importance)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="importance 必须是整数")
    if content is None and importance is None:
        raise HTTPException(status_code=400, detail="没有要修改的内容")
    db.update_memory_item(item_id, content=content, importance=importance)
    knowledge.reindex_memory(item_id)
    return {"status": "ok", "item": db.get_memory_item(item_id)}


@app.delete("/api/memory/{item_id}")
async def delete_memory(item_id: str):
    db.delete_memory_item(item_id)
    knowledge.delete_source("memory", item_id)
    return {"status": "ok"}


@app.get("/api/knowledge/search")
async def knowledge_search(q: str = "", project_id: str | None = None, rerank: bool = False):
    """检索自检：学习文档 / 记忆知识块，供数据面板或调试使用。"""
    q = q.strip()
    if not q:
        return {"items": []}
    settings = config.load()
    provider = llm.make_provider(settings)
    items = await knowledge.search(
        q, provider, project_id=project_id,
        enable_rerank=rerank, top_k=10, budget_tokens=4000,
    )
    return {"items": knowledge.source_meta(items)}


@app.post("/api/memory/summarize")
async def summarize_memory(body: dict | None = None):
    """手动触发低频压缩：对指定会话（默认最近活跃）执行摘要重写。"""
    settings = config.load()
    provider = llm.make_provider(settings)
    if provider.name != "deepseek":
        return {
            "status": "ok",
            "cache_invalidated": False,
            "detail": "原型模式：真实压缩需要先配置 DeepSeek API Key",
        }
    cid = (body or {}).get("conversation_id")
    if not cid:
        convs = db.list_conversations()
        cid = convs[0]["id"] if convs else None
    if not cid:
        return {"status": "ok", "cache_invalidated": False, "detail": "还没有对话可压缩"}
    # 手动按钮 = 强制压缩：只保留最近 2 条，其余全压（复用路由快照的 warm 前缀）
    text = await memory.summarize_conversation(cid, provider, force=True, prefix=_last_routed.get(cid))
    if not text:
        return {
            "status": "ok",
            "cache_invalidated": False,
            "detail": "对话内容太少（不足 2 轮），多聊几句再来压缩",
        }
    return {
        "status": "ok",
        "cache_invalidated": True,
        "detail": f"已压缩并重写摘要（{len(text)} 字，缓存已失效）",
    }


def _update_summary_message(cid: str, st: dict) -> None:
    """点改计划字段后：原位更新确认摘要消息的内容与可编辑卡片。"""
    mid = st.get("summary_msg_id")
    if not mid:
        return
    msg = learning_skill.question_message(st)
    if msg.get("kind") != "plan_edit":
        return
    db.update_message_content(mid, msg["content"])
    db.update_message_quick_options(
        mid,
        json.dumps({
            "kind": "plan_edit",
            "plan": msg.get("plan") or {},
            "assumed": (msg.get("plan") or {}).get("assumed") or [],
            "opts": msg["quick_options"],
            "input": False,
            "placeholder": "",
        }, ensure_ascii=False),
    )


# ---------- 学习模式（AI 决策式引导，引导消息直接落库到对话） ----------

def _opts_json(opts: list, input_mode: bool = False, placeholder: str = "") -> str | None:
    if not opts and not input_mode:
        return None
    return json.dumps(
        {"opts": opts or [], "input": input_mode, "placeholder": placeholder or ""},
        ensure_ascii=False,
    )


def _emit_guide(cid: str, st: dict, fallback: bool = False) -> None:
    """生成并落库一条 AI 引导消息（含选项卡/自定义输入框）。"""
    msg = learning_skill.question_message(st, fallback)
    if msg.get("kind") == "plan_edit":
        qo = json.dumps({
            "kind": "plan_edit",
            "plan": msg.get("plan") or {},
            "assumed": (msg.get("plan") or {}).get("assumed") or [],
            "opts": msg["quick_options"],
            "input": False,
            "placeholder": "",
        }, ensure_ascii=False)
    else:
        qo = _opts_json(msg["quick_options"], msg.get("input", False), msg.get("placeholder", ""))
    row = db.add_message(cid, "assistant", msg["content"], quick_options=qo)
    if msg.get("kind") == "plan_edit":
        learning_skill.note_summary_msg(cid, row["id"])
    db.touch(cid)


async def _create_project_flow(cid: str) -> dict:
    """确认创建：落库进度消息 → LLM 实时生成 4 份文档 → 落库完成消息。"""
    settings = config.load()
    provider = llm.make_provider(settings)
    if provider.name != "deepseek":
        return {"status": "error", "detail": "创建学习项目需要先配置 DeepSeek API Key"}
    db.add_message(
        cid, "assistant",
        "⏳ 收到！正在**实时分析**你的学习目标并生成文档…（大纲 / 进度表 / 笔记 / 疑问本，约 10-30 秒）",
    )
    try:
        project = await learning_skill.create_project(provider, cid)
    except ValueError as e:
        db.add_message(cid, "assistant", f"⚠️ 生成失败：{e}\n\n再说一次「确认创建」即可重试。")
        return {"status": "error", "detail": str(e)}
    n_chapters = len(db.list_learning_chapters(project["id"]))
    db.add_message(
        cid, "assistant",
        f"🎉 **学习项目「{project['subject']}」创建成功！**\n\n"
        f"- 📖 共 {n_chapters} 章：{project['level_from'] or '零基础'} → {project['level_to'] or '入门'}\n"
        f"- 📄 大纲 / 进度表 / 笔记 / 疑问本已生成\n\n"
        "点顶栏 **📖 学习文档** 随时查看。这个对话就是你的学习专用对话，继续聊就行！",
    )
    db.touch(cid)
    return {"status": "created", "project": project}


async def _learning_intercept(cid: str, user_text: str) -> JSONResponse:
    """学习引导中的聊天统一入口：回答 → 推进状态机 → 返回 AI 引导消息。
    打字与点选项卡都走这里（confirm 阶段任意消息 = 确认创建）。"""
    db.add_message(cid, "user", user_text)
    st = learning_skill.get(cid)
    if st is None:
        return JSONResponse({"mode": "learning", "status": "ended"})
    if st["done"]:
        return JSONResponse(await _create_project_flow(cid))
    settings = config.load()
    provider = llm.make_provider(settings)
    st = await learning_skill.answer(user_text, cid, provider)
    if learning_skill.snapshot(st)["done"]:
        return JSONResponse(await _create_project_flow(cid))
    _emit_guide(cid, st, fallback=provider.name != "deepseek")
    snap = learning_skill.snapshot(st, fallback=provider.name != "deepseek")
    return JSONResponse({
        "mode": "learning", "status": "ask",
        "step": snap.get("step", len(st["history"]) + 1), "done": snap["done"],
    })


async def _update_intercept(cid: str, user_text: str) -> JSONResponse:
    """学习档案建议的回答：确认打勾 / 暂不 / 自定义笔记。"""
    db.add_message(cid, "user", user_text)
    result = learning_skill.handle_update_answer(cid, user_text)
    db.add_message(cid, "assistant", result["content"])
    db.touch(cid)
    return JSONResponse({
        "mode": "learning_update",
        "status": result.get("status", "ok"),
        "chapter_title": result.get("chapter_title", ""),
    })


@app.post("/api/learning/mode")
async def learning_mode(body: dict | None = None):
    """进入/推进学习模式（按会话）。无 answer = 进入引导；有 answer = 推进状态机。"""
    b = body or {}
    cid = (b.get("conversation_id") or "").strip()
    if not cid or not db.get_conversation(cid):
        raise HTTPException(status_code=404, detail="会话不存在，请先新建对话")
    existing = db.get_learning_project_by_conv(cid)
    if existing:
        return {"status": "has_project", "project": existing}

    if b.get("action") == "edit-plan":
        # 确认摘要卡片点改字段 → 原位更新摘要消息
        try:
            st = learning_skill.edit_plan(cid, str(b.get("field") or ""), str(b.get("value") or ""))
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
        _update_summary_message(cid, st)
        snap = learning_skill.snapshot(st)
        return {"status": "ask", "step": len(st["history"]) + 1, "done": snap["done"]}

    answer_val = b.get("answer")
    if answer_val is not None:
        st = learning_skill.get(cid)
        if st is None:
            raise HTTPException(status_code=409, detail="学习引导已结束，点「🎓 学习模式」重开新项目")
        db.add_message(cid, "user", str(answer_val))
        if st["done"]:
            return await _create_project_flow(cid)  # 点「✅ 确认创建」选项卡
        settings = config.load()
        provider = llm.make_provider(settings)
        st = await learning_skill.answer(str(answer_val), cid, provider)
        _emit_guide(cid, st, fallback=provider.name != "deepseek")  # 落库下一条引导消息
    else:
        created = not learning_skill.has(cid)
        st = None
        if created:
            settings = config.load()
            provider = llm.make_provider(settings)
            history = db.get_ui_messages(cid)
            if provider.name == "deepseek" and any(m["role"] == "user" for m in history):
                transcript = "\n".join(
                    f"{m['role']}: {str(m['content'])[:500]}" for m in history[-12:]
                )
                st = await learning_skill.recommend(cid, provider, transcript)
        if st is None:
            st = learning_skill.start(cid)
        if learning_skill.snapshot(st)["done"]:  # 异常残留的已完成引导 → 重开
            learning_skill.clear(cid)
            st = learning_skill.start(cid)
            created = True
        if created:
            _emit_guide(cid, st, fallback=provider.name != "deepseek")  # 落库第 1 问
    snap = learning_skill.snapshot(st)
    return {"status": "ask", "step": snap.get("step", len(st["history"]) + 1), "done": snap["done"]}


@app.post("/api/learning/projects")
async def create_learning_project(body: dict | None = None):
    """备用入口：确认创建（点选项卡/打字确认走 /api/learning/mode 或 /api/chat 拦截）。"""
    cid = ((body or {}).get("conversation_id") or "").strip()
    if not cid or not db.get_conversation(cid):
        raise HTTPException(status_code=404, detail="会话不存在")
    return await _create_project_flow(cid)


@app.get("/api/learning/projects")
async def list_learning_projects():
    return {"items": db.list_learning_projects()}


@app.get("/api/learning/projects/{pid}")
async def project_detail(pid: str):
    project = db.get_learning_project(pid)
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    chapters = db.list_learning_chapters(pid)
    questions = db.list_learning_questions(pid)
    notes = []
    for meta in db.list_note_meta(pid):
        p = learning_skill.NOTES_DIR / pid / meta["filename"]
        notes.append({**meta, "content": p.read_text(encoding="utf-8") if p.exists() else ""})
    done = sum(1 for c in chapters if c["status"] == "done")
    return {
        "project": project, "chapters": chapters, "questions": questions,
        "notes": notes, "progress": {"done": done, "total": len(chapters)},
    }


@app.delete("/api/learning/projects/{pid}")
async def delete_learning_project(pid: str):
    """删除学习记录（文档/章节/疑问/知识块），保留原对话。"""
    if not db.get_learning_project(pid):
        raise HTTPException(status_code=404, detail="项目不存在")
    shutil.rmtree(learning_skill.NOTES_DIR / pid, ignore_errors=True)
    db.delete_learning_project(pid)
    return {"status": "ok"}


@app.put("/api/learning/chapters/{chapter_id}")
async def update_chapter(chapter_id: str, body: dict):
    """章节编辑：勾选/取消完成、标题/目标/备注（结构变化自动同步文档与索引）。"""
    chapter = db.get_learning_chapter(chapter_id)
    if not chapter:
        raise HTTPException(status_code=404, detail="章节不存在")
    pid = chapter["project_id"]
    status = str(body.get("status") or "")
    if status in ("done", "pending"):
        db.set_chapter_status(chapter_id, status)
    if body.get("note") is not None:
        db.update_learning_chapter_note(chapter_id, str(body["note"]))
    if body.get("title") is not None or body.get("objective") is not None:
        db.update_learning_chapter(
            chapter_id,
            title=body.get("title") if body.get("title") is not None else None,
            objective=body.get("objective") if body.get("objective") is not None else None,
        )
    done, total = learning_skill.refresh_project_progress(pid)
    learning_skill.regenerate_project_docs(pid)  # 大纲/进度表与档案同步
    knowledge.reindex_project(pid)
    return {"status": "ok", "done": done, "total": total}


@app.post("/api/learning/projects/{pid}/chapters")
async def add_chapter(pid: str, body: dict):
    """插入新章节（order_no 缺省追加到末尾）。"""
    project = db.get_learning_project(pid)
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    title = str(body.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="章节标题不能为空")
    chapters = db.list_learning_chapters(pid)
    order_no = int(body.get("order_no") or 0)
    if not (1 <= order_no <= len(chapters) + 1):
        order_no = len(chapters) + 1
    db.insert_learning_chapter(pid, title[:60], str(body.get("objective") or "")[:120], order_no)
    learning_skill.refresh_project_progress(pid)
    learning_skill.regenerate_project_docs(pid)
    knowledge.reindex_project(pid)
    return {"status": "ok", "chapters": db.list_learning_chapters(pid)}


@app.delete("/api/learning/chapters/{chapter_id}")
async def delete_chapter(chapter_id: str):
    """删除章节（后续章节序号自动前移；被删的是当前章节则清空）。"""
    chapter = db.get_learning_chapter(chapter_id)
    if not chapter:
        raise HTTPException(status_code=404, detail="章节不存在")
    pid = chapter["project_id"]
    db.delete_learning_chapter(chapter_id)
    learning_skill.refresh_project_progress(pid)
    learning_skill.regenerate_project_docs(pid)
    knowledge.reindex_project(pid)
    return {"status": "ok", "chapters": db.list_learning_chapters(pid)}


@app.post("/api/learning/chapters/{chapter_id}/move")
async def move_chapter(chapter_id: str, body: dict):
    """章节上移/下移。"""
    chapter = db.get_learning_chapter(chapter_id)
    if not chapter:
        raise HTTPException(status_code=404, detail="章节不存在")
    direction = str(body.get("direction") or "")
    if direction not in ("up", "down"):
        raise HTTPException(status_code=400, detail="direction 必须是 up 或 down")
    db.move_learning_chapter(chapter_id, direction)
    learning_skill.regenerate_project_docs(chapter["project_id"])
    knowledge.reindex_project(chapter["project_id"])
    return {"status": "ok", "chapters": db.list_learning_chapters(chapter["project_id"])}


@app.post("/api/learning/chapters/{chapter_id}/start")
async def start_chapter(chapter_id: str):
    """工作室/侧栏点「开始」：设置当前章节，并写一条消息进学习对话。"""
    chapter = db.get_learning_chapter(chapter_id)
    if not chapter:
        raise HTTPException(status_code=404, detail="章节不存在")
    project = db.get_learning_project(chapter["project_id"])
    db.set_current_chapter(chapter["project_id"], chapter_id)
    chapters = db.list_learning_chapters(chapter["project_id"])
    idx = next((i for i, c in enumerate(chapters, 1) if c["id"] == chapter_id), 0)
    if project and project.get("conversation_id") and db.get_conversation(project["conversation_id"]):
        objective = f"\n\n本章目标：{chapter['objective']}" if chapter.get("objective") else ""
        db.add_message(
            project["conversation_id"], "assistant",
            f"📖 开始学习 **第 {idx} 章 {chapter['title']}**。{objective}",
        )
        db.touch(project["conversation_id"])
    return {"status": "ok", "chapter_index": idx, "title": chapter["title"]}


@app.post("/api/learning/projects/{pid}/organize")
async def organize_notes(pid: str, body: dict | None = None):
    """工作室「整理」Tab：同步触发 AI 整理（阻塞约 10-30 秒）。"""
    project = db.get_learning_project(pid)
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    settings = config.load()
    provider = llm.make_provider(settings)
    try:
        result = await learning_skill.organize_project_notes(pid, provider)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return result


@app.post("/api/learning/projects/{pid}/organize/rollback")
async def rollback_organized_notes(pid: str):
    """回退到最近一份历史整理稿。"""
    project = db.get_learning_project(pid)
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    try:
        return learning_skill.rollback_organized(pid)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/learning/projects/{pid}/notes")
async def add_project_note(pid: str, body: dict):
    """学习对话/工作台追加笔记或疑问。"""
    project = db.get_learning_project(pid)
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    content = str(body.get("content") or "").strip()
    if not content:
        return {"status": "error", "detail": "内容不能为空"}
    kind = str(body.get("kind") or "note")
    result = learning_skill._add_note_command(project, content, kind)
    return {"status": result.get("status", "ok"), "content": result.get("content", "")}


# ---------- 静态前端 ----------

app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
