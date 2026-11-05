"""Provider wrapper: one small interface over two wire formats.

* OpenAI-compatible chat completions: Gemini (free tier), Groq, OpenRouter,
  OpenAI, Ollama. Structured output via response_format json_schema.
* Anthropic Messages API: structured output via a forced tool call.

Stdlib only (urllib), so every byte on the wire is visible and explainable.
Retries use exponential backoff with full jitter and honour Retry-After.
"""
from __future__ import annotations

import json
import os
import random
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

PRESETS: dict[str, dict] = {
    # model defaults are only a starting point: check the provider's model list.
    "gemini": {"style": "openai", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
               "key_env": "GEMINI_API_KEY", "model": "gemini-3.8-flash", "stream_usage": False},
    "groq": {"style": "openai", "base_url": "https://api.groq.com/openai/v1",
             "key_env": "GROQ_API_KEY", "model": None, "stream_usage": True},
    "openrouter": {"style": "openai", "base_url": "https://openrouter.ai/api/v1",
                   "key_env": "OPENROUTER_API_KEY", "model": None, "stream_usage": True},
    "openai": {"style": "openai", "base_url": "https://api.openai.com/v1",
               "key_env": "OPENAI_API_KEY", "model": None, "stream_usage": True},
    "ollama": {"style": "openai", "base_url": "http://localhost:11434/v1",
               "key_env": None, "model": None, "stream_usage": False},
    "anthropic": {"style": "anthropic", "base_url": "https://api.anthropic.com",
                  "key_env": "ANTHROPIC_API_KEY", "model": "claude-haiku-5-5"},
}

RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}


class LLMError(RuntimeError):
    pass


@dataclass
class LLMResult:
    text: str
    data: dict | None
    input_tokens: int
    output_tokens: int
    latency_s: float
    provider: str
    model: str
    attempts: int = 1
    tokens_estimated: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass
class LLMConfig:
    provider: str = "gemini"
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    temperature: float = 0.0
    max_tokens: int = 2048
    timeout_s: float = 90.0
    max_retries: int = 5
    backoff_base_s: float = 1.0
    backoff_cap_s: float = 30.0

    @classmethod
    def from_env(cls, provider: str | None = None, model: str | None = None) -> "LLMConfig":
        provider = provider or os.getenv("TRIAGER_PROVIDER", "gemini")
        if provider not in PRESETS:
            raise LLMError(f"Unknown provider '{provider}'. Choose from: {', '.join(PRESETS)}")
        p = PRESETS[provider]
        cfg = cls(provider=provider)
        cfg.model = model or os.getenv("TRIAGER_MODEL") or p["model"]
        cfg.base_url = os.getenv("TRIAGER_BASE_URL") or p["base_url"]
        cfg.api_key = os.getenv(p["key_env"]) if p["key_env"] else None
        if not cfg.model:
            raise LLMError(f"No model set for '{provider}'. Pass --model or set TRIAGER_MODEL.")
        if p["key_env"] and not cfg.api_key:
            raise LLMError(f"Missing API key: set {p['key_env']}.")
        return cfg


# ------------------------------------------------------------------ HTTP

def _post(url: str, headers: dict, body: dict, cfg: LLMConfig, stream: bool):
    """POST with retries. Returns the open response (caller reads it) and attempt count."""
    data = json.dumps(body).encode()
    attempt = 0
    while True:
        attempt += 1
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **headers})
        try:
            return urllib.request.urlopen(req, timeout=cfg.timeout_s), attempt
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:800]
            if e.code not in RETRY_STATUS or attempt > cfg.max_retries:
                raise LLMError(f"HTTP {e.code} from {cfg.provider}: {detail}") from e
            delay = _backoff(attempt, cfg, e.headers.get("Retry-After"))
        except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
            if attempt > cfg.max_retries:
                raise LLMError(f"Network error talking to {cfg.provider}: {e}") from e
            delay = _backoff(attempt, cfg, None)
        time.sleep(delay)


def _backoff(attempt: int, cfg: LLMConfig, retry_after: str | None) -> float:
    if retry_after:
        try:
            return min(float(retry_after), cfg.backoff_cap_s * 2)
        except ValueError:
            pass
    # full jitter: uniform(0, base * 2^attempt), capped
    return random.uniform(0, min(cfg.backoff_cap_s, cfg.backoff_base_s * 2 ** attempt))


def _sse_events(resp):
    """Yield decoded JSON payloads from a server-sent-events response."""
    for raw in resp:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            yield json.loads(payload)
        except json.JSONDecodeError:
            continue


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _parse_json(text: str) -> dict | None:
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        t = t[t.find("{"):] if "{" in t else t
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(t[start:end + 1])
    except json.JSONDecodeError:
        return None


# ------------------------------------------------------------------ client

class LLMClient:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self.style = PRESETS[cfg.provider]["style"]
        self._json_mode = "schema"  # degrades to "object" if a provider rejects json_schema

    def complete(
        self,
        system: str,
        messages: list[dict],
        schema: dict | None = None,
        stream: bool = False,
        on_delta: Callable[[str], None] | None = None,
    ) -> LLMResult:
        t0 = time.monotonic()
        if self.style == "anthropic":
            res = self._anthropic(system, messages, schema, stream, on_delta)
        else:
            res = self._openai(system, messages, schema, stream, on_delta)
        res.latency_s = time.monotonic() - t0
        return res

    # -------------------------------------------------------------- OpenAI-style

    def _openai(self, system, messages, schema, stream, on_delta) -> LLMResult:
        cfg = self.cfg
        url = cfg.base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
        sys_text = system
        body: dict = {
            "model": cfg.model,
            "temperature": cfg.temperature,
            "max_tokens": cfg.max_tokens,
        }
        if schema is not None:
            if self._json_mode == "schema":
                body["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": "triage", "schema": schema, "strict": True},
                }
            else:
                body["response_format"] = {"type": "json_object"}
                sys_text += "\n\nReturn JSON matching this schema exactly:\n" + json.dumps(schema)
        body["messages"] = [{"role": "system", "content": sys_text}] + messages
        if stream:
            body["stream"] = True
            if PRESETS[cfg.provider].get("stream_usage"):
                body["stream_options"] = {"include_usage": True}
        try:
            resp, attempts = _post(url, headers, body, cfg, stream)
        except LLMError as e:
            msg = str(e)
            if schema is not None and self._json_mode == "schema" and "HTTP 400" in msg and (
                "response_format" in msg or "json_schema" in msg or "schema" in msg.lower()
            ):
                self._json_mode = "object"  # provider/model lacks native schema mode
                res = self._openai(system, messages, schema, stream, on_delta)
                res.notes.append("native json_schema rejected; fell back to json_object + schema in prompt")
                return res
            raise

        notes: list[str] = []
        if stream:
            parts: list[str] = []
            usage = None
            with resp:
                for ev in _sse_events(resp):
                    if ev.get("usage"):
                        usage = ev["usage"]
                    for ch in ev.get("choices") or []:
                        piece = (ch.get("delta") or {}).get("content") or ""
                        if piece:
                            parts.append(piece)
                            if on_delta:
                                on_delta(piece)
            text = "".join(parts)
        else:
            with resp:
                payload = json.loads(resp.read())
            text = payload["choices"][0]["message"].get("content") or ""
            usage = payload.get("usage")
        estimated = not usage
        in_tok = (usage or {}).get("prompt_tokens") or _estimate_tokens(sys_text + json.dumps(messages))
        out_tok = (usage or {}).get("completion_tokens") or _estimate_tokens(text)
        if self._json_mode == "object" and schema is not None:
            notes.append("json_object mode")
        return LLMResult(text, _parse_json(text) if schema is not None else None, in_tok, out_tok, 0.0,
                         cfg.provider, cfg.model, attempts, estimated, notes)

    # -------------------------------------------------------------- Anthropic

    def _anthropic(self, system, messages, schema, stream, on_delta) -> LLMResult:
        cfg = self.cfg
        url = cfg.base_url.rstrip("/") + "/v1/messages"
        headers = {"x-api-key": cfg.api_key or "", "anthropic-version": "2023-06-01"}
        body: dict = {
            "model": cfg.model,
            "max_tokens": cfg.max_tokens,
            "temperature": cfg.temperature,
            "system": system,
            "messages": messages,
        }
        if schema is not None:
            body["tools"] = [{
                "name": "report_triage",
                "description": "Report the structured crash triage.",
                "input_schema": schema,
            }]
            body["tool_choice"] = {"type": "tool", "name": "report_triage"}
        if stream:
            body["stream"] = True
        resp, attempts = _post(url, headers, body, cfg, stream)

        in_tok = out_tok = 0
        if stream:
            text_parts: list[str] = []
            json_parts: list[str] = []
            with resp:
                for ev in _sse_events(resp):
                    t = ev.get("type")
                    if t == "message_start":
                        in_tok = ev["message"]["usage"].get("input_tokens", 0)
                    elif t == "content_block_delta":
                        d = ev["delta"]
                        piece = d.get("partial_json") or d.get("text") or ""
                        (json_parts if d.get("type") == "input_json_delta" else text_parts).append(piece)
                        if piece and on_delta:
                            on_delta(piece)
                    elif t == "message_delta":
                        out_tok = ev.get("usage", {}).get("output_tokens", out_tok)
                    elif t == "error":
                        raise LLMError(f"Anthropic stream error: {ev.get('error')}")
            text = "".join(json_parts) or "".join(text_parts)
            data = _parse_json(text) if schema is not None else None
        else:
            with resp:
                payload = json.loads(resp.read())
            usage = payload.get("usage", {})
            in_tok, out_tok = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
            data, text = None, ""
            for block in payload.get("content", []):
                if block.get("type") == "tool_use":
                    data = block.get("input")
                    text = json.dumps(data)
                elif block.get("type") == "text":
                    text += block.get("text", "")
            if schema is not None and data is None:
                data = _parse_json(text)
        return LLMResult(text, data, in_tok, out_tok, 0.0, cfg.provider, cfg.model, attempts)
