"""Wire-format tests with a fake urlopen: no network, no API key."""
import io
import json
import urllib.error

import pytest

from triager import llm_client
from triager.llm_client import LLMClient, LLMConfig, LLMError

SCHEMA = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"], "additionalProperties": False}


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def sse(*events) -> FakeResp:
    lines = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
    return FakeResp(lines.encode())


@pytest.fixture
def calls(monkeypatch):
    """Queue of responses (bytes, FakeResp, or Exception) for urlopen."""
    queue, sent = [], []

    def fake_urlopen(req, timeout=None):
        sent.append(json.loads(req.data))
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item if isinstance(item, FakeResp) else FakeResp(json.dumps(item).encode())

    monkeypatch.setattr(llm_client.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(llm_client.time, "sleep", lambda s: None)
    return queue, sent


def http_error(code, body="", headers=None):
    return urllib.error.HTTPError("u", code, "err", headers or {}, io.BytesIO(body.encode()))


def openai_cfg():
    return LLMConfig(provider="gemini", model="m", base_url="https://x", api_key="k")


def anthropic_cfg():
    return LLMConfig(provider="anthropic", model="m", base_url="https://x", api_key="k")


def test_openai_structured(calls):
    queue, sent = calls
    queue.append({"choices": [{"message": {"content": '{"a": "b"}'}}], "usage": {"prompt_tokens": 10, "completion_tokens": 3}})
    r = LLMClient(openai_cfg()).complete("sys", [{"role": "user", "content": "hi"}], SCHEMA)
    assert r.data == {"a": "b"} and (r.input_tokens, r.output_tokens) == (10, 3)
    assert sent[0]["response_format"]["type"] == "json_schema"
    assert sent[0]["temperature"] == 0.0


def test_openai_stream(calls):
    queue, _ = calls
    queue.append(sse({"choices": [{"delta": {"content": '{"a": '}}]}, {"choices": [{"delta": {"content": '"b"}'}}]}))
    pieces = []
    r = LLMClient(openai_cfg()).complete("s", [{"role": "user", "content": "x"}], SCHEMA, stream=True, on_delta=pieces.append)
    assert r.data == {"a": "b"} and "".join(pieces) == '{"a": "b"}'
    assert r.tokens_estimated  # Gemini preset sends no usage in the stream


def test_openai_falls_back_when_json_schema_rejected(calls):
    queue, sent = calls
    queue.append(http_error(400, '{"error": "response_format json_schema is not supported"}'))
    queue.append({"choices": [{"message": {"content": '{"a": "b"}'}}], "usage": {}})
    r = LLMClient(openai_cfg()).complete("s", [{"role": "user", "content": "x"}], SCHEMA)
    assert sent[1]["response_format"] == {"type": "json_object"}
    assert "Return JSON matching this schema" in sent[1]["messages"][0]["content"]
    assert r.data == {"a": "b"} and r.notes


def test_retries_429_then_succeeds(calls):
    queue, sent = calls
    queue.append(http_error(429, "rate limited", {"Retry-After": "1"}))
    queue.append(http_error(503, "busy"))
    queue.append({"choices": [{"message": {"content": '{"a": "b"}'}}]})
    r = LLMClient(openai_cfg()).complete("s", [{"role": "user", "content": "x"}], SCHEMA)
    assert r.attempts == 3 and len(sent) == 3


def test_does_not_retry_auth_errors(calls):
    queue, sent = calls
    queue.append(http_error(401, "bad key"))
    with pytest.raises(LLMError, match="HTTP 401"):
        LLMClient(openai_cfg()).complete("s", [{"role": "user", "content": "x"}], SCHEMA)
    assert len(sent) == 1


def test_anthropic_forced_tool(calls):
    queue, sent = calls
    queue.append({"content": [{"type": "tool_use", "name": "report_triage", "input": {"a": "b"}}],
                  "usage": {"input_tokens": 50, "output_tokens": 7}})
    r = LLMClient(anthropic_cfg()).complete("sys", [{"role": "user", "content": "x"}], SCHEMA)
    assert r.data == {"a": "b"} and r.input_tokens == 50
    assert sent[0]["tool_choice"] == {"type": "tool", "name": "report_triage"}
    assert sent[0]["system"] == "sys"


def test_anthropic_stream(calls):
    queue, _ = calls
    queue.append(sse(
        {"type": "message_start", "message": {"usage": {"input_tokens": 42}}},
        {"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": '{"a": '}},
        {"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": '"b"}'}},
        {"type": "message_delta", "usage": {"output_tokens": 9}},
    ))
    r = LLMClient(anthropic_cfg()).complete("s", [{"role": "user", "content": "x"}], SCHEMA, stream=True)
    assert r.data == {"a": "b"} and (r.input_tokens, r.output_tokens) == (42, 9)


def test_config_requires_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(LLMError, match="GEMINI_API_KEY"):
        LLMConfig.from_env("gemini")
