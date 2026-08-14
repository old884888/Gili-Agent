"""LLM Provider 抽象层。

- MockProvider：原型回退 —— 未配置 API Key 时自动使用，明确标注是假数据
- DeepSeekProvider：真实调用（OpenAI 兼容）—— 流式 + usage 解析

流式事件契约：stream_chat 逐个产出 dict：
    {"kind": "reasoning", "text": "..."}  思考过程（reasoning_content）
    {"kind": "content",   "text": "..."}  正式回复

关键点：
- 流式响应必须传 stream_options={"include_usage": True}，
  流结束后 provider.last_usage 里才有 prompt_cache_hit_tokens / miss_tokens
- 思考模式（DeepSeek V4）：thinking.type 放 extra_body，reasoning_effort 顶层传参；
  关闭思考时不要带 reasoning_effort
- 思考模式下 temperature/top_p/penalty 不生效（API 不报错但忽略）
"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import AsyncIterator, Optional

from .schemas import ChatParams


class LLMProvider(ABC):
    """所有模型接入的统一接口。现在只有 DeepSeek，未来可扩展。"""

    name: str = "base"
    last_usage: Optional[dict] = None

    @abstractmethod
    async def stream_chat(
        self, messages: list[dict], params: Optional[ChatParams] = None
    ) -> AsyncIterator[dict]:
        """流式产出 {"kind": ..., "text": ...} 事件。"""
        raise NotImplementedError

    @abstractmethod
    async def complete(
        self,
        messages: list[dict],
        params: Optional[ChatParams] = None,
        tools: Optional[list[dict]] = None,
    ) -> dict:
        """非流式（工具探测轮用）：返回 {"content", "tool_calls", "usage"}。"""
        raise NotImplementedError


class MockProvider(LLMProvider):
    name = "mock"

    async def stream_chat(self, messages: list[dict], params: Optional[ChatParams] = None):
        last = messages[-1]["content"] if messages else ""
        reply = (
            "（原型模式 · 模拟回复）我收到了你的消息：「"
            f"{last[:40]}」。"
            "你还没在 ⚙ 设置里填 DeepSeek API Key —— 填上并测试连接后，这里就是真实的流式回复。"
        )
        for ch in reply:
            yield {"kind": "content", "text": ch}
            await asyncio.sleep(0.02)

    async def complete(self, messages, params=None, tools=None):
        last = messages[-1]["content"] if messages else ""
        return {
            "content": f"（原型模式 · 模拟回复）{last[:40]}",
            "tool_calls": [],
            "usage": {},
        }


class DeepSeekProvider(LLMProvider):
    name = "deepseek"

    def __init__(self, api_key: str, base_url: str = "https://api.deepseek.com"):
        from openai import AsyncOpenAI  # 延迟导入：没 key 时不强制装包

        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    async def stream_chat(self, messages: list[dict], params: Optional[ChatParams] = None):
        params = params or ChatParams()
        kwargs: dict = dict(
            model=params.model,
            messages=messages,
            temperature=params.temperature,
            top_p=params.top_p,
            max_tokens=params.max_tokens,
            presence_penalty=params.presence_penalty,
            frequency_penalty=params.frequency_penalty,
            stream=True,
            stream_options={"include_usage": True},
        )
        if params.thinking_enabled:
            kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
            kwargs["reasoning_effort"] = params.reasoning_effort
        else:
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}

        stream = await self.client.chat.completions.create(**kwargs)
        async for chunk in stream:
            # 实测（2026-08）：DeepSeek 的流末块 choices 与 usage 同时存在，
            # 所以 usage 判断必须独立，不能用 elif —— 否则永远捕获不到
            if chunk.usage:
                u = chunk.usage
                details = getattr(u, "completion_tokens_details", None)
                self.last_usage = {
                    "prompt_tokens": u.prompt_tokens,
                    "prompt_cache_hit_tokens": getattr(u, "prompt_cache_hit_tokens", 0),
                    "prompt_cache_miss_tokens": getattr(u, "prompt_cache_miss_tokens", 0),
                    "completion_tokens": getattr(u, "completion_tokens", 0),
                    "reasoning_tokens": getattr(details, "reasoning_tokens", 0),
                }
            if chunk.choices:
                delta = chunk.choices[0].delta
                # 截断原因（stop=正常结束, length=撞 max_tokens 上限）
                if chunk.choices[0].finish_reason:
                    self.last_usage = {
                        **(self.last_usage or {}),
                        "finish_reason": chunk.choices[0].finish_reason,
                    }
                reasoning = getattr(delta, "reasoning_content", None)
                if reasoning:
                    yield {"kind": "reasoning", "text": reasoning}
                if delta.content:
                    yield {"kind": "content", "text": delta.content}


    async def complete(self, messages, params=None, tools=None):
        """非流式：工具探测轮用（一次几百 token，比流式探测便宜可控）。"""
        params = params or ChatParams()
        kwargs: dict = dict(
            model=params.model,
            messages=messages,
            temperature=params.temperature,
            top_p=params.top_p,
            max_tokens=params.max_tokens,  # 不压上限：无工具时它就是最终答案
            presence_penalty=params.presence_penalty,
            frequency_penalty=params.frequency_penalty,
            stream=False,
        )
        if tools:
            kwargs["tools"] = tools
        if params.thinking_enabled:
            kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
            kwargs["reasoning_effort"] = params.reasoning_effort
        else:
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}

        resp = await self.client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message
        details = getattr(resp.usage, "completion_tokens_details", None)
        self.last_usage = {
            "prompt_tokens": getattr(resp.usage, "prompt_tokens", 0),
            "prompt_cache_hit_tokens": getattr(resp.usage, "prompt_cache_hit_tokens", 0),
            "prompt_cache_miss_tokens": getattr(resp.usage, "prompt_cache_miss_tokens", 0),
            "completion_tokens": getattr(resp.usage, "completion_tokens", 0),
            "reasoning_tokens": getattr(details, "reasoning_tokens", 0),
            "finish_reason": resp.choices[0].finish_reason,
        }
        return {
            "content": msg.content or "",
            "tool_calls": [
                {"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments}
                for tc in (msg.tool_calls or [])
            ],
            "usage": self.last_usage,
        }


def make_provider(settings: dict) -> LLMProvider:
    """按设置选 Provider：有 key 走真实，否则回退 mock。"""
    key = (settings.get("api_key") or "").strip()
    if key:
        base = (settings.get("base_url") or "").strip() or "https://api.deepseek.com"
        return DeepSeekProvider(key, base)
    return MockProvider()
