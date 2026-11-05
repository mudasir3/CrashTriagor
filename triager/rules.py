"""Signature rules: cheap, deterministic pattern matching that produces hints."""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from .parser import ParsedCrash

RULES_PATH = Path(__file__).with_name("rules.yaml")


@dataclass
class RuleHit:
    id: str
    category: str
    hint: str


@lru_cache(maxsize=4)
def load_rules(path: str = str(RULES_PATH)) -> list[dict]:
    rules = yaml.safe_load(Path(path).read_text())
    for r in rules:
        r["_compiled"] = {k: re.compile(v) for k, v in r["when"].items() if k != "kind"}
    return rules


def _matches(rule: dict, crash: ParsedCrash) -> bool:
    when = rule["when"]
    if "kind" in when and crash.kind != when["kind"]:
        return False
    c = rule["_compiled"]
    if "exception" in c and not any(c["exception"].search(l.type) for l in crash.chain):
        return False
    if "message" in c and not any(c["message"].search(l.message) for l in crash.chain):
        return False
    if "frames" in c:
        names = [f"{f.class_name}.{f.method}" for f in crash.all_frames()]
        if not any(c["frames"].search(n) for n in names):
            return False
    if "text" in c and not c["text"].search(crash.raw_block):
        return False
    return True


def apply_rules(crash: ParsedCrash, path: str = str(RULES_PATH)) -> list[RuleHit]:
    return [RuleHit(r["id"], r["category"], r["hint"]) for r in load_rules(path) if _matches(r, crash)]
