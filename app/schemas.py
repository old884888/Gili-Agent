"""Pydantic 模型 —— 前后端数据契约。

原型阶段已定稿：后续所有逻辑按此形状实现，不改接口。
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


# ---------- 聊天 ----------

class ChatParams(BaseModel):
    """对话参数 —— 对应右侧参数面板。

    思考模式/强度是 DeepSeek V4 的独立旋钮（reasoning_effort），
    思考开启时 temperature/top_p/penalty 不生效（API 不报错但忽略）。
    """

    model: str = "deepseek-v4-flash"
    thinking_enabled: bool = True
    reasoning_effort: Literal["low", "high", "max"] = "high"
    temperature: float = Field(1.0, ge=0.0, le=2.0)
    top_p: float = Field(1.0, ge=0.0, le=1.0)
    max_tokens: int = Field(4096, ge=1, le=131072)  # 思考模式消耗输出 token，默认给足
    presence_penalty: float = Field(0.0, ge=-2.0, le=2.0)
    frequency_penalty: float = Field(0.0, ge=-2.0, le=2.0)
    stream: bool = True


class MemoryParams(BaseModel):
    """记忆参数 —— 决定命中率的核心旋钮。"""

    history_token_budget: int = Field(12000, ge=500, le=131072)
    summary_rewrite_threshold: int = Field(16000, ge=1000, le=131072)
    enable_memory_recall: bool = True
    enable_kb_recall: bool = True       # 学习文档 / 记忆知识检索
    enable_rerank: bool = False          # LLM 重排（更准更慢，默认关）


class ChatRequest(BaseModel):
    """POST /api/chat 请求体。

    regenerate_message_id 提供时进入"重新生成"模式：
    上下文截断到该 AI 消息之前，message 字段被忽略，产出作为新版本。
    """

    conversation_id: Optional[str] = None
    message: str = Field("", min_length=0)
    regenerate_message_id: Optional[str] = None
    attachments: list[str] = Field(default_factory=list, description="上传的文件 id（内容注入波动区）")
    params: ChatParams = ChatParams()
    memory: MemoryParams = MemoryParams()


# ---------- 记忆 ----------

class MemoryItem(BaseModel):
    """单条记忆。id 为空时由后端生成。"""

    id: str = ""
    layer: Literal["core", "working", "longterm"] = "longterm"
    content: str
    tokens: int = 0
    importance: int = Field(2, ge=1, le=3)  # 1=细节 2=一般 3=重要
    created_at: str = ""
