"""Responses API: tool-protocol parity with /v1/chat/completions."""
import asyncio
import json
import sys
from pathlib import Path

import pytest

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))

import app


TOOL_JSON = '{"name": "get_weather", "arguments": {"city": "Paris"}}'


def _stream_of(*chunks):
    return iter(chunks)


@pytest.fixture
def no_engine(monkeypatch):
    # The endpoint only needs safe_chat_stream; never touch the browser.
    monkeypatch.setattr(app, "engine", type("E", (), {"current_acc_id": None})())


def test_tools_are_ignored_when_the_protocol_is_off(no_engine, monkeypatch):
    monkeypatch.setattr(app.CFG, "tool_protocol", False)
    monkeypatch.setattr(app, "safe_chat_stream",
                        lambda *a, **k: _stream_of("just an answer"))
    req = app.ResponsesRequest(
        input="weather?",
        tools=[{"type": "function", "name": "get_weather", "parameters": {}}])

    out = asyncio.run(app.responses_api(req, None))

    assert out["output"][0]["type"] == "message"
    assert out["output"][0]["content"][0]["text"] == "just an answer"


def test_tool_calls_are_parsed_when_the_protocol_is_on(no_engine, monkeypatch):
    monkeypatch.setattr(app.CFG, "tool_protocol", True)
    monkeypatch.setattr(app, "safe_chat_stream", lambda *a, **k: _stream_of(TOOL_JSON))
    req = app.ResponsesRequest(
        input="weather?",
        tools=[{"type": "function", "name": "get_weather", "parameters": {}}])

    out = asyncio.run(app.responses_api(req, None))

    item = out["output"][0]
    assert item["type"] == "function_call"
    assert item["name"] == "get_weather"
    assert json.loads(item["arguments"]) == {"city": "Paris"}
    assert item["status"] == "completed"


def test_usage_is_estimated_not_zero(no_engine, monkeypatch):
    monkeypatch.setattr(app.CFG, "tool_protocol", False)
    monkeypatch.setattr(app, "safe_chat_stream",
                        lambda *a, **k: _stream_of("x" * 400))
    req = app.ResponsesRequest(input="hello")

    out = asyncio.run(app.responses_api(req, None))

    assert out["usage"]["total_tokens"] > 0
    assert out["usage"]["output_tokens"] == 100


def _collect(response) -> list[str]:
    async def run():
        return [chunk async for chunk in response.body_iterator]
    return asyncio.run(run())


def test_streaming_text_events(no_engine, monkeypatch):
    monkeypatch.setattr(app.CFG, "tool_protocol", False)
    monkeypatch.setattr(app, "safe_chat_stream",
                        lambda *a, **k: _stream_of("hello ", "world"))
    req = app.ResponsesRequest(input="hi", stream=True)

    chunks = _collect(asyncio.run(app.responses_api(req, None)))
    events = [chunk.split("\n", 1)[0].removeprefix("event: ") for chunk in chunks]

    assert events[0] == "response.created"
    assert "response.output_text.delta" in events
    assert events[-1] == "response.completed"
    assert events.count("response.output_item.added") == 1


def test_streaming_tool_call_emits_function_call_events(no_engine, monkeypatch):
    monkeypatch.setattr(app.CFG, "tool_protocol", True)
    monkeypatch.setattr(app, "safe_chat_stream", lambda *a, **k: _stream_of(TOOL_JSON))
    req = app.ResponsesRequest(
        input="weather?", stream=True,
        tools=[{"type": "function", "name": "get_weather", "parameters": {}}])

    chunks = _collect(asyncio.run(app.responses_api(req, None)))
    events = [chunk.split("\n", 1)[0].removeprefix("event: ") for chunk in chunks]

    assert "response.function_call_arguments.done" in events
    assert "response.output_text.delta" not in events
    assert events[-1] == "response.completed"
    # A tool call must not also be announced as a message item.
    assert events.count("response.output_item.added") == 1
