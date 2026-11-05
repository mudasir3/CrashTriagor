"""Append-only JSONL log of every model call: tokens, latency, cost, prompt version."""
from __future__ import annotations

import json
import time
from pathlib import Path

import yaml

PRICING_PATH = Path(__file__).with_name("pricing.yaml")
DEFAULT_LOG = Path("runs.jsonl")


def load_pricing(path: Path = PRICING_PATH) -> dict:
    return yaml.safe_load(path.read_text()) or {}


def cost_usd(provider: str, model: str, input_tokens: int, output_tokens: int, pricing: dict | None = None) -> float | None:
    """None when no price is configured, so 'unknown' never masquerades as $0."""
    pricing = load_pricing() if pricing is None else pricing
    p = pricing.get(f"{provider}/{model}")
    if not p:
        return None
    return input_tokens / 1e6 * p["input_per_mtok"] + output_tokens / 1e6 * p["output_per_mtok"]


def log_call(record: dict, path: Path = DEFAULT_LOG) -> None:
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **record}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(record) + "\n")
