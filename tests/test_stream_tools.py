# -*- coding: utf-8 -*-
"""流式工具探测：tool_calls 增量拼接 + 事件顺序 + usage 捕获（v0.25 防回归）。"""
import asyncio

from app.llm import DeepSeekProvider, MockProvider

ARG1 = '{"query":'
ARG2 = '"今天新闻"}'


class FakeFn:
    def __init__(self, name=None, arguments=None):
        self.name = name
        self.arguments = arguments


class FakeTC:
    def __init__(self, index, id=None, name=None, arguments=None):
        self.index = index
        self.id = id
        self.function = FakeFn(name, arguments)


class FakeDelta:
    def __init__(self, reasoning_content=None, content=None, tool_calls=None):
        self.reasoning_content = reasoning_content
        self.content = content
        self.tool_calls = tool_calls


class FakeChoice:
    def __init__(self, delta=None, finish_reason=None):
        self.delta = delta
        self.finish_reason = finish_reason


class FakeUsage:
    prompt_tokens = 100
    prompt_cache_hit_tokens = 20
    prompt_cache_miss_tokens = 80
    completion_tokens = 50
    completion_tokens_details = None


class FakeChunk:
    def __init__(self, usage=None, choices=None):
        self.usage = usage
        self.choices = choices


class FakeStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


class FakeClient:
    def __init__(self, stream):
        self._stream = stream

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    async def create(self, **kwargs):
        return self._stream


def _provider(chunks):
    p = DeepSeekProvider.__new__(DeepSeekProvider)
    p.client = FakeClient(FakeStream(chunks))
    return p


async def _collect(provider):
    out = []
    async for e in provider.stream_chat_with_tools(
        [{"role": "user", "content": "x"}], None,
        [{"type": "function", "function": {"name": "web_search"}}],
    ):
        out.append(e)
    return out


def test_tool_call_accumulation():
    chunks = [
        FakeChunk(choices=[FakeChoice(delta=FakeDelta(reasoning_content="我想想"))]),
        FakeChunk(choices=[FakeChoice(delta=FakeDelta(tool_calls=[FakeTC(0, id="call_1", name="web_search")]))]),
        FakeChunk(choices=[FakeChoice(delta=FakeDelta(tool_calls=[FakeTC(0, arguments=ARG1)]))]),
        FakeChunk(choices=[FakeChoice(delta=FakeDelta(tool_calls=[FakeTC(0, arguments=ARG2)]))]),
        FakeChunk(choices=[FakeChoice(delta=FakeDelta(content="我来查一下"))]),
        FakeChunk(usage=FakeUsage(), choices=[FakeChoice(delta=FakeDelta(), finish_reason="stop")]),
    ]
    events = asyncio.run(_collect(_provider(chunks)))
    kinds = [e["kind"] for e in events]
    assert "reasoning" in kinds and "content" in kinds
    last = events[-1]
    assert last["kind"] == "tool_calls"
    assert last["calls"] == [{"id": "call_1", "name": "web_search", "arguments": ARG1 + ARG2}]


def test_no_tool_calls():
    chunks = [
        FakeChunk(choices=[FakeChoice(delta=FakeDelta(content="直接回答"))]),
        FakeChunk(usage=FakeUsage(), choices=[FakeChoice(delta=FakeDelta(), finish_reason="stop")]),
    ]
    events = asyncio.run(_collect(_provider(chunks)))
    assert events[-1]["kind"] == "tool_calls" and events[-1]["calls"] == []


def test_mock_provider():
    async def run():
        p = MockProvider()
        out = []
        async for e in p.stream_chat_with_tools([{"role": "user", "content": "hi"}], None, []):
            out.append(e)
        return out

    events = asyncio.run(run())
    assert events[-1]["kind"] == "tool_calls" and events[-1]["calls"] == []
