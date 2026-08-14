"""SQLite 持久层 —— data/gili.db（WAL 模式）。

表：
- conversations  会话
- messages       消息（summarized=1 表示已被摘录、不再进入提示词窗口）
- memory_items   三层记忆（layer: core/working/longterm；kind: fact/summary）
- request_logs   每次请求的 usage（命中率/成本/延迟/缓存失效标记）

连接显式设 check_same_thread=False（异步单线程事件循环内使用，后台任务同线程），
连接按需开合，个人单用户场景够用。
"""
from __future__ import annotations

import sqlite3
import time
import uuid
from pathlib import Path

from .prompt_builder import estimate_tokens

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA_DIR / "gili.db"

_SCHEMA: list[str] = [
    """CREATE TABLE IF NOT EXISTS conversations (
        id TEXT PRIMARY KEY,
        title TEXT DEFAULT '新对话',
        created_at TEXT,
        updated_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS messages (
        id TEXT PRIMARY KEY,
        conversation_id TEXT,
        role TEXT,
        content TEXT,
        tokens INTEGER DEFAULT 0,
        summarized INTEGER DEFAULT 0,
        created_at TEXT,
        reasoning TEXT DEFAULT '',
        edited_at TEXT,
        version_group TEXT,
        version_no INTEGER DEFAULT 1,
        is_active INTEGER DEFAULT 1
    )""",
    """CREATE TABLE IF NOT EXISTS memory_items (
        id TEXT PRIMARY KEY,
        layer TEXT,
        kind TEXT DEFAULT 'fact',
        conversation_id TEXT,
        content TEXT,
        tokens INTEGER DEFAULT 0,
        created_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS request_logs (
        id TEXT PRIMARY KEY,
        conversation_id TEXT,
        ts TEXT,
        model TEXT,
        prompt_tokens INTEGER DEFAULT 0,
        hit_tokens INTEGER DEFAULT 0,
        miss_tokens INTEGER DEFAULT 0,
        reasoning_tokens INTEGER DEFAULT 0,
        latency_ms INTEGER DEFAULT 0,
        cost_cny REAL DEFAULT 0,
        cache_invalidated INTEGER DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id)",
    "CREATE INDEX IF NOT EXISTS idx_request_logs_ts ON request_logs(ts)",
    "CREATE INDEX IF NOT EXISTS idx_memory_created ON memory_items(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_messages_vg ON messages(version_group)",
    "CREATE INDEX IF NOT EXISTS idx_memory_kind ON memory_items(kind)",
    "CREATE INDEX IF NOT EXISTS idx_memory_conv ON memory_items(conversation_id)",
    """CREATE TABLE IF NOT EXISTS learning_projects (
        id TEXT PRIMARY KEY,
        subject TEXT,
        level_from TEXT,
        level_to TEXT,
        style TEXT,
        status TEXT DEFAULT 'active',
        progress_pct INTEGER DEFAULT 0,
        conversation_id TEXT,
        created_at TEXT,
        updated_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS learning_chapters (
        id TEXT PRIMARY KEY,
        project_id TEXT,
        title TEXT,
        objective TEXT,
        order_no INTEGER,
        status TEXT DEFAULT 'pending',
        note TEXT,
        updated_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS learning_questions (
        id TEXT PRIMARY KEY,
        project_id TEXT,
        question TEXT,
        concept TEXT,
        source_ref TEXT,
        status TEXT DEFAULT 'pending',
        created_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS learning_notes_meta (
        id TEXT PRIMARY KEY,
        project_id TEXT,
        filename TEXT,
        title TEXT,
        version_no INTEGER DEFAULT 1,
        updated_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS knowledge_chunks (
        id TEXT PRIMARY KEY,
        source_type TEXT NOT NULL,
        source_id TEXT NOT NULL,
        project_id TEXT,
        conversation_id TEXT,
        title TEXT DEFAULT '',
        heading TEXT DEFAULT '',
        content TEXT NOT NULL,
        tokens INTEGER DEFAULT 0,
        ordinal INTEGER DEFAULT 0,
        updated_at TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_kb_source ON knowledge_chunks(source_type, source_id)",
    "CREATE INDEX IF NOT EXISTS idx_kb_project ON knowledge_chunks(project_id)",
]


_initialized = False
_FTS_AVAILABLE = False


def init() -> None:
    """建表 + 迁移（幂等）。首次调用时执行，也可显式调用。"""
    global _initialized
    if _initialized:
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = _conn(skip_init=True)
    try:
        for s in _SCHEMA:
            conn.execute(s)
        _migrate(conn)
        _init_knowledge_fts(conn)
        conn.commit()
    finally:
        conn.close()
    _initialized = True


def _init_knowledge_fts(conn: sqlite3.Connection) -> None:
    """FTS5（trigram）+ 外部内容同步触发器；不可用则静默回退 LIKE。"""
    global _FTS_AVAILABLE
    try:
        conn.execute(
            """CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
                content, title, heading,
                content='knowledge_chunks',
                content_rowid='rowid',
                tokenize='trigram'
            )"""
        )
        cols = {r[1] for r in conn.execute("PRAGMA table_info(knowledge_fts)")}
        if "heading" not in cols:
            # FTS5 虚拟表不支持 ALTER，直接重建（内容表不受影响，随后 rebuild 回填）
            conn.execute("DROP TABLE knowledge_fts")
            conn.execute(
                """CREATE VIRTUAL TABLE knowledge_fts USING fts5(
                    content, title, heading,
                    content='knowledge_chunks',
                    content_rowid='rowid',
                    tokenize='trigram'
                )"""
            )
        conn.execute(
            """CREATE TRIGGER IF NOT EXISTS kb_chunks_ai AFTER INSERT ON knowledge_chunks BEGIN
                INSERT INTO knowledge_fts(rowid, content, title, heading)
                VALUES (new.rowid, new.content, new.title, new.heading);
            END"""
        )
        conn.execute(
            """CREATE TRIGGER IF NOT EXISTS kb_chunks_ad AFTER DELETE ON knowledge_chunks BEGIN
                INSERT INTO knowledge_fts(knowledge_fts, rowid, content, title, heading)
                VALUES ('delete', old.rowid, old.content, old.title, old.heading);
            END"""
        )
        conn.execute(
            """CREATE TRIGGER IF NOT EXISTS kb_chunks_au AFTER UPDATE ON knowledge_chunks BEGIN
                INSERT INTO knowledge_fts(knowledge_fts, rowid, content, title, heading)
                VALUES ('delete', old.rowid, old.content, old.title, old.heading);
                INSERT INTO knowledge_fts(rowid, content, title, heading)
                VALUES (new.rowid, new.content, new.title, new.heading);
            END"""
        )
        conn.execute(
            "INSERT INTO knowledge_fts(knowledge_fts) VALUES('rebuild')"
        )
        _FTS_AVAILABLE = True
    except Exception:
        _FTS_AVAILABLE = False


def knowledge_fts_available() -> bool:
    return _FTS_AVAILABLE


def _migrate(conn: sqlite3.Connection) -> None:
    """增量加列（v0.7）：消息抽取标记 / 记忆重要度 / 会话收藏。"""
    for table, col, ddl in [
        ("messages", "extracted", "ALTER TABLE messages ADD COLUMN extracted INTEGER DEFAULT 0"),
        ("messages", "reasoning", "ALTER TABLE messages ADD COLUMN reasoning TEXT DEFAULT ''"),
        ("messages", "edited_at", "ALTER TABLE messages ADD COLUMN edited_at TEXT"),
        ("messages", "version_group", "ALTER TABLE messages ADD COLUMN version_group TEXT"),
        ("messages", "version_no", "ALTER TABLE messages ADD COLUMN version_no INTEGER DEFAULT 1"),
        ("messages", "is_active", "ALTER TABLE messages ADD COLUMN is_active INTEGER DEFAULT 1"),
        ("memory_items", "importance", "ALTER TABLE memory_items ADD COLUMN importance INTEGER DEFAULT 2"),
        ("conversations", "favorite", "ALTER TABLE conversations ADD COLUMN favorite INTEGER DEFAULT 0"),
        ("request_logs", "conversation_id", "ALTER TABLE request_logs ADD COLUMN conversation_id TEXT"),
        ("learning_projects", "conversation_id", "ALTER TABLE learning_projects ADD COLUMN conversation_id TEXT"),
        ("messages", "quick_options", "ALTER TABLE messages ADD COLUMN quick_options TEXT"),
        ("messages", "sources", "ALTER TABLE messages ADD COLUMN sources TEXT"),
        ("conversations", "memory_group", "ALTER TABLE conversations ADD COLUMN memory_group TEXT"),
        ("learning_projects", "current_chapter", "ALTER TABLE learning_projects ADD COLUMN current_chapter TEXT"),
    ]:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            conn.execute(ddl)
    # 老数据回填：无版本组的历史消息以自身 id 为组（重新生成才能正确停用旧版）
    conn.execute("UPDATE messages SET version_group = id WHERE version_group IS NULL")


def _conn(skip_init: bool = False) -> sqlite3.Connection:
    if not skip_init:
        init()
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _query(sql: str, params: tuple = ()) -> list[dict]:
    conn = _conn()
    try:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _exec(sql: str, params: tuple = ()) -> None:
    conn = _conn()
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ---------- 会话 ----------

def create_conversation(title: str = "新对话") -> dict:
    cid = "c" + uuid.uuid4().hex[:8]
    t = _now()
    _exec(
        "INSERT INTO conversations (id, title, created_at, updated_at) VALUES (?,?,?,?)",
        (cid, title, t, t),
    )
    return get_conversation(cid)


def get_conversation(cid: str) -> dict | None:
    rows = _query("SELECT * FROM conversations WHERE id=?", (cid,))
    return rows[0] if rows else None


def list_conversations() -> list[dict]:
    return _query(
        """SELECT cv.id, cv.title, cv.updated_at, cv.favorite,
                  (SELECT COUNT(*) FROM messages m WHERE m.conversation_id=cv.id) AS msg_count,
                  lp.id AS project_id, lp.subject AS project_subject,
                  cv.memory_group,
                  (SELECT COUNT(*) FROM conversations g
                   WHERE g.memory_group = cv.memory_group AND cv.memory_group IS NOT NULL) AS memory_group_size
           FROM conversations cv
           LEFT JOIN learning_projects lp ON lp.conversation_id = cv.id
           ORDER BY cv.updated_at DESC"""
    )


def set_favorite(cid: str, favorite: bool) -> None:
    _exec("UPDATE conversations SET favorite=? WHERE id=?", (1 if favorite else 0, cid))


def touch(cid: str) -> None:
    """更新会话时间（标题由 AI 起标题或手动重命名负责）。"""
    _exec("UPDATE conversations SET updated_at=? WHERE id=?", (_now(), cid))


def rename_conversation(cid: str, title: str) -> None:
    _exec("UPDATE conversations SET title=?, updated_at=? WHERE id=?", (title, _now(), cid))


def link_memory_group(cids: list[str]) -> str:
    """把 2+ 个会话连入同一记忆组（现有组会合并，覆盖为同一组）。"""
    gid = "g" + uuid.uuid4().hex[:8]
    for cid in cids:
        _exec("UPDATE conversations SET memory_group=? WHERE id=?", (gid, cid))
    return gid


def unlink_memory_group(cid: str) -> None:
    """取消单个会话的共享；组内只剩一个成员时自动解散。"""
    rows = _query("SELECT memory_group FROM conversations WHERE id=?", (cid,))
    gid = rows[0]["memory_group"] if rows else None
    _exec("UPDATE conversations SET memory_group=NULL WHERE id=?", (cid,))
    if gid:
        remaining = _query("SELECT COUNT(*) AS n FROM conversations WHERE memory_group=?", (gid,))[0]["n"]
        if remaining < 2:
            _exec("UPDATE conversations SET memory_group=NULL WHERE memory_group=?", (gid,))


def get_memory_scope(cid: str) -> list[str]:
    """该会话可见的会话 id 集合（未共享 = 仅自己；共享 = 全组成员）。"""
    row = get_conversation(cid)
    if not row:
        return []
    if not row.get("memory_group"):
        return [cid]
    rows = _query(
        "SELECT id FROM conversations WHERE memory_group=?",
        (row["memory_group"],),
    )
    return [str(r["id"]) for r in rows]


def delete_conversation(cid: str) -> None:
    """级联删除：消息 + 该会话记忆/知识块 + 会话本身，并维护共享组。"""
    row = get_conversation(cid)
    gid = row.get("memory_group") if row else None
    _exec("DELETE FROM messages WHERE conversation_id=?", (cid,))
    _exec("DELETE FROM memory_items WHERE conversation_id=?", (cid,))
    _exec("DELETE FROM knowledge_chunks WHERE conversation_id=?", (cid,))
    _exec("DELETE FROM request_logs WHERE conversation_id=?", (cid,))
    _exec("DELETE FROM conversations WHERE id=?", (cid,))
    if gid:
        remaining = _query("SELECT COUNT(*) AS n FROM conversations WHERE memory_group=?", (gid,))[0]["n"]
        if remaining < 2:
            _exec("UPDATE conversations SET memory_group=NULL WHERE memory_group=?", (gid,))


# ---------- 消息 ----------

def add_message(
    cid: str,
    role: str,
    content: str,
    reasoning: str = "",
    version_group: str | None = None,
    version_no: int = 1,
    is_active: int = 1,
    quick_options: str | None = None,
    sources: str | None = None,
) -> dict:
    """落库消息。quick_options / sources：JSON 字符串，供前端渲染选项卡与引用。"""
    mid = "m" + uuid.uuid4().hex[:8]
    if version_group is None:
        version_group = mid  # 每条消息默认自成一组，重新生成时同组产生新版本
    row = {
        "id": mid,
        "conversation_id": cid,
        "role": role,
        "content": content,
        "tokens": estimate_tokens(content),
        "summarized": 0,
        "created_at": _now(),
        "reasoning": reasoning,
        "edited_at": None,
        "version_group": version_group,
        "version_no": version_no,
        "is_active": is_active,
        "quick_options": quick_options,
        "sources": sources,
    }
    _exec(
        """INSERT INTO messages (id, conversation_id, role, content, tokens, summarized, created_at,
                                 reasoning, edited_at, version_group, version_no, is_active, quick_options, sources)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        tuple(row.values()),
    )
    return row


def get_message(mid: str) -> dict | None:
    rows = _query("SELECT rowid, * FROM messages WHERE id=?", (mid,))
    return rows[0] if rows else None


def get_messages(cid: str, summarized: int | None = None, asc: bool = True) -> list[dict]:
    sql = "SELECT rowid, * FROM messages WHERE conversation_id=?"
    params: list = [cid]
    if summarized is not None:
        sql += " AND summarized=?"
        params.append(summarized)
    # rowid 兜底：同一秒多条消息时保证顺序稳定（created_at 秒级精度不够）
    sql += " ORDER BY created_at, rowid" + (" ASC" if asc else " DESC")
    return _query(sql, tuple(params))


def get_ui_messages(cid: str) -> list[dict]:
    """会话视图消息：仅当前生效版本，附带版本总数（供前端版本切换条）。"""
    return _query(
        """SELECT m.*,
                  (SELECT COUNT(*) FROM messages v
                   WHERE v.version_group = m.version_group AND m.version_group IS NOT NULL) AS versions_count
           FROM messages m
           WHERE m.conversation_id=? AND m.is_active=1
           ORDER BY m.created_at, m.rowid""",
        (cid,),
    )


def update_message_content(mid: str, content: str) -> None:
    _exec(
        "UPDATE messages SET content=?, tokens=?, edited_at=? WHERE id=?",
        (content, estimate_tokens(content), _now(), mid),
    )


def deactivate_group(group: str) -> None:
    _exec("UPDATE messages SET is_active=0 WHERE version_group=?", (group,))


def delete_version_group(group: str) -> None:
    """删除某条消息的全部版本（气泡删除）。"""
    _exec("DELETE FROM messages WHERE version_group=?", (group,))


def next_version_no(group: str) -> int:
    rows = _query(
        "SELECT COALESCE(MAX(version_no), 0) + 1 AS n FROM messages WHERE version_group=?",
        (group,),
    )
    return rows[0]["n"] if rows else 1


def list_versions(group: str) -> list[dict]:
    """版本组全部版本（含思考链），按版本号升序。"""
    return _query(
        "SELECT id, version_no, content, reasoning, created_at, is_active FROM messages WHERE version_group=? ORDER BY version_no",
        (group,),
    )


def count_versions(group: str) -> int:
    rows = _query(
        "SELECT COUNT(*) AS n FROM messages WHERE version_group=?", (group,)
    )
    return rows[0]["n"] if rows else 1


def get_context_messages(
    cid: str, token_budget: int, before_rowid: int | None = None
) -> tuple[list[dict], bool]:
    """提示词窗口内的工作记忆：未摘录消息，按预算从开头整条裁剪。

    before_rowid：只取该消息之前的部分（重新生成时截断上下文用）。
    返回 (消息, 是否发生了裁剪)。裁剪是确定性的（同样的预算 → 同样的前缀），
    两次裁剪之间缓存稳定；裁剪发生的那一次会缓存失效（该花的钱，指标里能看到）。
    """
    msgs = get_messages(cid, summarized=0)
    if before_rowid is not None:
        msgs = [m for m in msgs if m["rowid"] < before_rowid]
    before = len(msgs)
    total = sum(m["tokens"] for m in msgs)
    while msgs and total > token_budget:
        total -= msgs[0]["tokens"]
        msgs.pop(0)
    return msgs, len(msgs) < before


def mark_summarized(msg_ids: list[str]) -> None:
    if not msg_ids:
        return
    placeholders = ",".join("?" * len(msg_ids))
    _exec(f"UPDATE messages SET summarized=1 WHERE id IN ({placeholders})", tuple(msg_ids))


def mark_extracted(msg_ids: list[str]) -> None:
    if not msg_ids:
        return
    placeholders = ",".join("?" * len(msg_ids))
    _exec(f"UPDATE messages SET extracted=1 WHERE id IN ({placeholders})", tuple(msg_ids))


def count_unextracted_user_messages(cid: str) -> int:
    rows = _query(
        "SELECT COUNT(*) AS n FROM messages WHERE conversation_id=? AND role='user' AND extracted=0",
        (cid,),
    )
    return rows[0]["n"] if rows else 0


# ---------- 记忆 ----------

def list_memory(layer: str | None = None, kind: str | None = None) -> list[dict]:
    sql = "SELECT * FROM memory_items WHERE 1=1"
    params: list = []
    if layer:
        sql += " AND layer=?"
        params.append(layer)
    if kind:
        sql += " AND kind=?"
        params.append(kind)
    sql += " ORDER BY created_at"
    return _query(sql, tuple(params))


def add_memory_item(
    layer: str,
    content: str,
    kind: str = "fact",
    conversation_id: str | None = None,
    importance: int = 2,
) -> dict:
    mid = "m" + uuid.uuid4().hex[:8]
    row = {
        "id": mid,
        "layer": layer,
        "kind": kind,
        "conversation_id": conversation_id,
        "content": content,
        "tokens": estimate_tokens(content),
        "importance": max(1, min(3, importance)),
        "created_at": _now(),
    }
    _exec(
        """INSERT INTO memory_items (id, layer, kind, conversation_id, content, tokens, importance, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        tuple(row.values()),
    )
    return row


def update_memory_item(item_id: str, content: str | None = None, importance: int | None = None) -> None:
    sets, params = [], []
    if content is not None:
        sets.append("content=?")
        params.append(content)
        sets.append("tokens=?")
        params.append(estimate_tokens(content))
    if importance is not None:
        sets.append("importance=?")
        params.append(max(1, min(3, importance)))
    if not sets:
        return
    params.append(item_id)
    _exec(f"UPDATE memory_items SET {', '.join(sets)} WHERE id=?", tuple(params))


def delete_memory_item(item_id: str) -> None:
    _exec("DELETE FROM memory_items WHERE id=?", (item_id,))


def get_summary_item(cid: str) -> dict | None:
    rows = _query(
        "SELECT * FROM memory_items WHERE kind='summary' AND conversation_id=? LIMIT 1",
        (cid,),
    )
    return rows[0] if rows else None


def upsert_summary(cid: str, content: str) -> None:
    existing = get_summary_item(cid)
    if existing:
        _exec(
            "UPDATE memory_items SET content=?, tokens=?, created_at=? WHERE id=?",
            (content, estimate_tokens(content), _now(), existing["id"]),
        )
    else:
        add_memory_item("longterm", content, kind="summary", conversation_id=cid)


# ---------- 学习模式 ----------

def create_learning_project(
    subject: str, level_from: str, level_to: str, style: str, conversation_id: str
) -> dict:
    pid = "p" + uuid.uuid4().hex[:8]
    t = _now()
    _exec(
        """INSERT INTO learning_projects (id, subject, level_from, level_to, style, status, progress_pct, conversation_id, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (pid, subject, level_from, level_to, style, "active", 0, conversation_id, t, t),
    )
    return get_learning_project(pid)


def get_learning_project(pid: str) -> dict | None:
    rows = _query("SELECT * FROM learning_projects WHERE id=?", (pid,))
    return rows[0] if rows else None


def get_learning_project_by_conv(cid: str) -> dict | None:
    rows = _query("SELECT * FROM learning_projects WHERE conversation_id=? LIMIT 1", (cid,))
    return rows[0] if rows else None


def delete_learning_project(pid: str) -> None:
    """删除学习记录（章节/疑问/笔记元数据/知识块/项目），保留对话本体。"""
    _exec("DELETE FROM knowledge_chunks WHERE project_id=?", (pid,))
    _exec("DELETE FROM learning_chapters WHERE project_id=?", (pid,))
    _exec("DELETE FROM learning_questions WHERE project_id=?", (pid,))
    _exec("DELETE FROM learning_notes_meta WHERE project_id=?", (pid,))
    _exec("DELETE FROM learning_projects WHERE id=?", (pid,))


def list_learning_projects() -> list[dict]:
    return _query(
        """SELECT p.*,
                  cv.title AS conversation_title,
                  CASE WHEN cv.id IS NULL THEN 0 ELSE 1 END AS has_conversation,
                  (SELECT COUNT(*) FROM learning_chapters c WHERE c.project_id=p.id) AS chapters_total,
                  (SELECT COUNT(*) FROM learning_chapters c WHERE c.project_id=p.id AND c.status='done') AS chapters_done
           FROM learning_projects p
           LEFT JOIN conversations cv ON cv.id = p.conversation_id
           ORDER BY p.created_at DESC"""
    )


def add_learning_chapter(pid: str, title: str, objective: str, order_no: int) -> None:
    _exec(
        """INSERT INTO learning_chapters (id, project_id, title, objective, order_no, status, note, updated_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        ("c" + uuid.uuid4().hex[:8], pid, title, objective, order_no, "pending", "", _now()),
    )


def list_learning_chapters(pid: str) -> list[dict]:
    return _query(
        "SELECT * FROM learning_chapters WHERE project_id=? ORDER BY order_no", (pid,)
    )


def set_chapter_status(chapter_id: str, status: str) -> None:
    _exec("UPDATE learning_chapters SET status=?, updated_at=? WHERE id=?", (status, _now(), chapter_id))


def get_learning_chapter(chapter_id: str) -> dict | None:
    rows = _query("SELECT * FROM learning_chapters WHERE id=?", (chapter_id,))
    return rows[0] if rows else None


def set_current_chapter(pid: str, chapter_id: str | None) -> None:
    _exec("UPDATE learning_projects SET current_chapter=?, updated_at=? WHERE id=?", (chapter_id, _now(), pid))


def get_current_chapter(pid: str) -> dict | None:
    project = get_learning_project(pid)
    if not project or not project.get("current_chapter"):
        return None
    return get_learning_chapter(project["current_chapter"])


def update_learning_chapter_note(chapter_id: str, note: str) -> None:
    _exec("UPDATE learning_chapters SET note=?, updated_at=? WHERE id=?", (note, _now(), chapter_id))


def update_learning_project_progress(pid: str, progress_pct: int) -> None:
    _exec(
        "UPDATE learning_projects SET progress_pct=?, updated_at=? WHERE id=?",
        (progress_pct, _now(), pid),
    )


def add_learning_question(pid: str, question: str, concept: str, source_ref: str = "") -> None:
    _exec(
        """INSERT INTO learning_questions (id, project_id, question, concept, source_ref, status, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        ("q" + uuid.uuid4().hex[:8], pid, question, concept, source_ref, "pending", _now()),
    )


def list_learning_questions(pid: str) -> list[dict]:
    return _query(
        "SELECT * FROM learning_questions WHERE project_id=? ORDER BY created_at", (pid,)
    )


def add_note_meta(pid: str, filename: str, title: str) -> None:
    _exec(
        """INSERT INTO learning_notes_meta (id, project_id, filename, title, version_no, updated_at)
           VALUES (?,?,?,?,?,?)""",
        ("n" + uuid.uuid4().hex[:8], pid, filename, title, 1, _now()),
    )


def list_note_meta(pid: str) -> list[dict]:
    return _query(
        "SELECT * FROM learning_notes_meta WHERE project_id=? ORDER BY rowid", (pid,)
    )


# ---------- 请求日志 ----------

def add_request_log(
    model: str,
    prompt_tokens: int,
    hit_tokens: int,
    miss_tokens: int,
    reasoning_tokens: int,
    latency_ms: int,
    cost_cny: float,
    cache_invalidated: bool = False,
    conversation_id: str | None = None,
) -> None:
    _exec(
        """INSERT INTO request_logs
           (id, conversation_id, ts, model, prompt_tokens, hit_tokens, miss_tokens,
            reasoning_tokens, latency_ms, cost_cny, cache_invalidated)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "r" + uuid.uuid4().hex[:8],
            conversation_id,
            _now(),
            model,
            prompt_tokens,
            hit_tokens,
            miss_tokens,
            reasoning_tokens,
            latency_ms,
            cost_cny,
            1 if cache_invalidated else 0,
        ),
    )


def _cid_filter(cid: str | None) -> tuple[str, tuple]:
    return (" AND conversation_id=?", (cid,)) if cid else ("", ())


def list_request_logs(limit: int = 30, conversation_id: str | None = None) -> list[dict]:
    extra, params = _cid_filter(conversation_id)
    return _query(
        f"SELECT * FROM request_logs WHERE 1=1{extra} ORDER BY ts DESC, rowid DESC LIMIT ?",
        (*params, limit),
    )


def get_request_stats(conversation_id: str | None = None) -> dict:
    extra, params = _cid_filter(conversation_id)
    rows = _query(
        f"""SELECT COUNT(*) AS count,
                  COALESCE(SUM(prompt_tokens),0) AS prompt,
                  COALESCE(SUM(hit_tokens),0) AS hit,
                  COALESCE(SUM(miss_tokens),0) AS miss,
                  COALESCE(SUM(reasoning_tokens),0) AS reasoning,
                  COALESCE(SUM(cost_cny),0) AS cost,
                  COALESCE(AVG(latency_ms),0) AS latency,
                  COALESCE(SUM(cache_invalidated),0) AS invalidated
           FROM request_logs WHERE 1=1{extra}""",
        params,
    )
    s = rows[0]
    prompt = s["prompt"]
    return {
        "count": s["count"],
        "total_prompt_tokens": prompt,
        "total_hit_tokens": s["hit"],
        "total_miss_tokens": s["miss"],
        "total_reasoning_tokens": s["reasoning"],
        "cost_cny": round(s["cost"], 6),
        "avg_latency_ms": int(s["latency"]),
        "cache_invalidated": s["invalidated"],
        "hit_rate": round(s["hit"] / prompt, 4) if prompt else 0.0,
    }


def get_series(limit: int = 40, conversation_id: str | None = None) -> list[dict]:
    """最近 N 条请求（时间正序返回，供曲线从左到右绘制）。"""
    extra, params = _cid_filter(conversation_id)
    rows = _query(
        f"""SELECT ts, hit_tokens, prompt_tokens FROM request_logs
           WHERE 1=1{extra} ORDER BY ts DESC, rowid DESC LIMIT ?""",
        (*params, limit),
    )
    out = []
    for r in reversed(rows):
        hit_rate = r["hit_tokens"] / r["prompt_tokens"] if r["prompt_tokens"] else 0.0
        out.append({"ts": r["ts"][-8:], "hit_rate": round(hit_rate, 4), "prompt_tokens": r["prompt_tokens"]})
    return out


# ---------- 知识块（学习文档 / 记忆检索） ----------

def get_memory_item(item_id: str) -> dict | None:
    rows = _query("SELECT * FROM memory_items WHERE id=?", (item_id,))
    return rows[0] if rows else None


def replace_knowledge_chunks(source_type: str, source_id: str, chunks: list[dict]) -> None:
    """同源重建：先删后插（触发器自动同步 FTS）；单连接批量写入。"""
    conn = _conn()
    try:
        conn.execute(
            "DELETE FROM knowledge_chunks WHERE source_type=? AND source_id=?",
            (source_type, source_id),
        )
        t = _now()
        rows = [
            (
                "k" + uuid.uuid4().hex[:8],
                source_type,
                source_id,
                c.get("project_id"),
                c.get("conversation_id"),
                c.get("title", ""),
                c.get("heading", ""),
                c.get("content", ""),
                estimate_tokens(c.get("content", "")),
                c.get("ordinal", i),
                t,
            )
            for i, c in enumerate(chunks)
        ]
        if rows:
            conn.executemany(
                """INSERT INTO knowledge_chunks
                   (id, source_type, source_id, project_id, conversation_id, title, heading,
                    content, tokens, ordinal, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
        conn.commit()
    finally:
        conn.close()


def delete_knowledge_source(source_type: str, source_id: str) -> None:
    _exec("DELETE FROM knowledge_chunks WHERE source_type=? AND source_id=?", (source_type, source_id))


def count_knowledge_chunks(source_type: str | None = None, source_id: str | None = None) -> int:
    sql = "SELECT COUNT(*) AS n FROM knowledge_chunks WHERE 1=1"
    params: list = []
    if source_type is not None:
        sql += " AND source_type=?"
        params.append(source_type)
    if source_id is not None:
        sql += " AND source_id=?"
        params.append(source_id)
    return _query(sql, tuple(params))[0]["n"]


def knowledge_fts_search(
    q: str, project_id: str | None = None, memory_scope: list[str] | None = None, limit: int = 60
) -> list[dict]:
    """FTS5 BM25 召回，返回 knowledge_chunks 行 + score（负数越小越相关）。"""
    if not knowledge_fts_available():
        return []
    sql = (
        "SELECT c.*, bm25(knowledge_fts) AS score "
        "FROM knowledge_fts f JOIN knowledge_chunks c ON c.rowid = f.rowid "
        "WHERE knowledge_fts MATCH ?"
    )
    params: list = [q]
    if project_id:
        sql += " AND (c.project_id=? OR c.source_type='memory')"
        params.append(project_id)
    else:
        # 普通对话没有绑定学习项目：只检索记忆，不把其他项目
        # 的大纲/笔记/章节/疑问漏进当前对话。
        sql += " AND c.source_type='memory'"
    if memory_scope:
        sql += (
            " AND (c.source_type != 'memory' OR c.conversation_id IS NULL OR "
            f"c.conversation_id IN ({','.join('?' * len(memory_scope))}))"
        )
        params.extend(memory_scope)
    sql += " ORDER BY score LIMIT ?"
    params.append(limit)
    try:
        return _query(sql, tuple(params))
    except Exception:
        return []


def knowledge_like_search(
    terms: list[str], project_id: str | None = None,
    memory_scope: list[str] | None = None, limit: int = 60
) -> list[dict]:
    """LIKE 兜底：2 字中文 / FTS5 不可用时使用。"""
    if not terms:
        return []
    clauses = []
    params: list = []
    for t in terms[:16]:
        clauses.append("(c.content LIKE ? OR c.title LIKE ?)")
        like = f"%{t}%"
        params.extend([like, like])
    sql = "SELECT c.* FROM knowledge_chunks c WHERE (" + " OR ".join(clauses) + ")"
    if project_id:
        sql += " AND (c.project_id=? OR c.source_type='memory')"
        params.append(project_id)
    else:
        sql += " AND c.source_type='memory'"
    if memory_scope:
        sql += (
            " AND (c.source_type != 'memory' OR c.conversation_id IS NULL OR "
            f"c.conversation_id IN ({','.join('?' * len(memory_scope))}))"
        )
        params.extend(memory_scope)
    sql += " LIMIT ?"
    params.append(limit)
    return _query(sql, tuple(params))


def list_knowledge_chunks(
    source_type: str | None = None, source_id: str | None = None
) -> list[dict]:
    sql = "SELECT * FROM knowledge_chunks WHERE 1=1"
    params: list = []
    if source_type is not None:
        sql += " AND source_type=?"
        params.append(source_type)
    if source_id is not None:
        sql += " AND source_id=?"
        params.append(source_id)
    sql += " ORDER BY ordinal"
    return _query(sql, tuple(params))
