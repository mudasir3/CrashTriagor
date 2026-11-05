"""The triage pipeline: redact -> parse -> (retrace) -> rules -> LLM -> validate -> ground.

Four modes, so the eval can measure what each layer contributes:
  rules_only  parser + rules, no model at all (free, deterministic floor)
  baseline    raw log + a one-line prompt, schema enforced
  llm_only    full system prompt + schema, raw log, no parser or rules
  full        parser + rules + full prompt + schema + grounding check
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from pydantic import ValidationError

from . import prompts
from .cost_log import cost_usd, log_call
from .llm_client import LLMClient, LLMResult
from .parser import ParsedCrash, parse
from .redact import redact
from .retrace import retrace
from .rules import RuleHit, apply_rules
from .schema import Confidence, CulpritFrame, SuggestedFix, Triage, triage_json_schema

MODES = ("full", "llm_only", "baseline", "rules_only")
MAX_RAW_CHARS = 12_000


@dataclass
class TriageOptions:
    mode: str = "full"
    app_packages: list[str] = field(default_factory=list)
    mapping_text: str | None = None
    impact: dict | None = None
    stream: bool = False
    on_delta: Callable[[str], None] | None = None
    dry_run: bool = False
    log_path: Path | None = Path("runs.jsonl")
    crash_id: str | None = None
    run_id: str | None = None


@dataclass
class TriageRun:
    mode: str
    parsed: ParsedCrash
    hits: list[RuleHit]
    redactions: dict[str, int]
    triage: Triage | None = None
    calls: list[LLMResult] = field(default_factory=list)
    valid_first_try: bool | None = None
    repaired: bool = False
    grounding_flags: list[str] = field(default_factory=list)
    error: str | None = None
    request_preview: dict | None = None

    @property
    def input_tokens(self) -> int:
        return sum(c.input_tokens for c in self.calls)

    @property
    def output_tokens(self) -> int:
        return sum(c.output_tokens for c in self.calls)

    @property
    def latency_s(self) -> float:
        return sum(c.latency_s for c in self.calls)

    @property
    def cost_usd(self) -> float | None:
        costs = [cost_usd(c.provider, c.model, c.input_tokens, c.output_tokens) for c in self.calls]
        return None if not costs or any(c is None for c in costs) else sum(costs)


# ------------------------------------------------------------------ severity heuristic (rules_only)

P0_HINTS = re.compile(r"(?i)\b(login|signin|auth|payment|checkout|billing|purchase)\w*")


def heuristic_severity(parsed: ParsedCrash, category: str, impact: dict | None) -> tuple[str, str]:
    if impact:
        if impact.get("on_startup"):
            return "P0", "Impact data: crashes on startup."
        users = impact.get("affected_users") or 0
        count = impact.get("crash_count") or 0
        if users >= 1000 or count >= 5000:
            return "P1", f"Impact data: {users} users / {count} crashes."
        if users >= 100 or count >= 500:
            return "P2", f"Impact data: {users} users / {count} crashes."
        return "P3", f"Impact data: {users} users / {count} crashes."
    if "StrictMode policy violation" in parsed.raw_block:
        return "P3", "Inferred: StrictMode violation, not a crash."
    names = " ".join(f"{f.class_name}.{f.method}" for f in parsed.all_frames() if f.origin == "app")
    if re.search(r"(App|Application)\.onCreate", names) or category == "database":
        return "P0", "Inferred from the trace alone: startup or data path."
    if P0_HINTS.search(names):
        return "P0", "Inferred from the trace alone: login/payment path."
    if parsed.kind == "anr":
        return "P1", "Inferred from the trace alone: ANR."
    return "P2", "Inferred from the trace alone; no impact data."


EXC_CATEGORY = [
    (r"OutOfMemoryError", "memory"),
    (r"NullPointerException|UninitializedPropertyAccess", "null_safety"),
    (r"SecurityException", "permission_security"),
    (r"SQLite|Room", "database"),
    (r"UnknownHost|SocketTimeout|ConnectException|SSL", "network"),
    (r"WindowManager", "window_ui"),
]


def rules_triage(parsed: ParsedCrash, hits: list[RuleHit], impact: dict | None) -> Triage:
    primary = parsed.primary
    exc = primary.type if primary else ("ANR" if parsed.kind == "anr" else parsed.native_signal or "unknown")
    if hits:
        category = hits[0].category
    elif parsed.kind == "native":
        category = "native"
    else:
        category = next((c for pat, c in EXC_CATEGORY if re.search(pat, exc)), "unknown")
    f = parsed.first_app_frame() if parsed.kind != "native" else None
    culprit = CulpritFrame(class_name=f.class_name, method=f.method, file=f.file, line=f.line, is_app_code=True) if f else None
    sev, reason = heuristic_severity(parsed, category, impact)
    needs: list[str] = []
    if parsed.obfuscated and not parsed.retraced:
        needs.append("mapping.txt for this build")
    if parsed.kind == "native":
        needs.append("symbolicated backtrace (unstripped .so + ndk-stack)")
    if category == "memory":
        needs.append("heap dump (LeakCanary or Android Studio profiler)")
    hint = hits[0].hint if hits else None
    msg = f": {primary.message}" if primary and primary.message else ""
    return Triage(
        crash_kind=parsed.kind if parsed.kind in ("jvm_exception", "anr", "native") else "unknown",
        exception=exc,
        root_cause_category=category,
        root_cause=hint or f"{exc}{msg}",
        culprit_frame=culprit,
        severity=sev,
        severity_reason=reason,
        confidence="high" if hits and not needs else ("medium" if hits else "low"),
        suggested_fix=SuggestedFix(summary=hint or "No known signature; inspect the first app frame.",
                                   steps=[hint] if hint else ["Inspect the first app frame and the deepest cause."]),
        needs_more_info=needs,
        rule_hits=[h.id for h in hits],
    )


# ------------------------------------------------------------------ grounding

def grounding_check(t: Triage, parsed: ParsedCrash, input_text: str) -> list[str]:
    """Rule 7 of the prompt, enforced in code: the culprit must exist in the input."""
    flags: list[str] = []
    cf = t.culprit_frame
    if cf:
        haystack = input_text + "\n" + "\n".join(f.short for f in parsed.all_frames())
        base = cf.class_name.split("$")[0]
        simple = base.split(".")[-1]
        if base not in haystack and simple not in haystack:
            flags.append("culprit_class_not_in_input")
        elif cf.method not in haystack:
            flags.append("culprit_method_not_in_input")
    return flags


# ------------------------------------------------------------------ main entry

def _build_request(mode: str, parsed: ParsedCrash, hits: list[RuleHit], redacted: str, impact: dict | None):
    if mode == "full":
        raw = parsed.raw_block[:MAX_RAW_CHARS]
        user = prompts.build_user_message(parsed.to_prompt_dict(), [h.__dict__ for h in hits], impact, raw)
        return prompts.SYSTEM_PROMPT, user
    if mode == "llm_only":
        return prompts.SYSTEM_PROMPT, prompts.build_llm_only_message(redacted[:MAX_RAW_CHARS], impact)
    if mode == "baseline":
        return "You are a helpful assistant.", f"{prompts.BASELINE_PROMPT}\n\n{redacted[:MAX_RAW_CHARS]}"
    raise ValueError(mode)


def run(text: str, opts: TriageOptions, client: LLMClient | None = None) -> TriageRun:
    if opts.mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    redacted, counts = redact(text)
    parsed = parse(redacted, opts.app_packages)
    if opts.mapping_text and opts.mode in ("full", "rules_only"):
        parsed = retrace(parsed, opts.mapping_text, opts.app_packages)
    hits = apply_rules(parsed)
    tr = TriageRun(mode=opts.mode, parsed=parsed, hits=hits, redactions=counts)

    if opts.mode == "rules_only":
        tr.triage = rules_triage(parsed, hits, opts.impact)
        tr.valid_first_try = True
        tr.grounding_flags = grounding_check(tr.triage, parsed, redacted)
        return tr

    system, user = _build_request(opts.mode, parsed, hits, redacted, opts.impact)
    schema = triage_json_schema()
    if opts.dry_run:
        tr.request_preview = {"system": system, "user": user, "schema": schema}
        return tr
    if client is None:
        raise ValueError("an LLMClient is required for LLM modes")

    run_id = opts.run_id or uuid.uuid4().hex[:8]
    res = client.complete(system, [{"role": "user", "content": user}], schema, opts.stream, opts.on_delta)
    tr.calls.append(res)
    triage, err = _validate(res.data, res.text)
    tr.valid_first_try = triage is not None
    if triage is None:
        repair_user = f"{user}\n\n<previous_output>\n{res.text[:4000]}\n</previous_output>\n\n{prompts.build_repair_message(err)}"
        res2 = client.complete(system, [{"role": "user", "content": repair_user}], schema, False, None)
        tr.calls.append(res2)
        triage, err = _validate(res2.data, res2.text)
        tr.repaired = triage is not None
    if triage is None:
        tr.error = f"invalid output after repair: {err}"
    else:
        flags = grounding_check(triage, parsed, redacted)
        if flags:
            triage.confidence = Confidence.low
            triage.needs_more_info.append("culprit frame not found in the input (possible hallucination)")
        tr.grounding_flags = flags
        tr.triage = triage

    if opts.log_path:
        for i, c in enumerate(tr.calls):
            log_call({
                "run_id": run_id, "crash_id": opts.crash_id, "mode": opts.mode, "call": i,
                "provider": c.provider, "model": c.model, "prompt_version": prompts.PROMPT_VERSION,
                "input_tokens": c.input_tokens, "output_tokens": c.output_tokens,
                "tokens_estimated": c.tokens_estimated, "latency_s": round(c.latency_s, 3),
                "attempts": c.attempts, "cost_usd": cost_usd(c.provider, c.model, c.input_tokens, c.output_tokens),
                "valid": (i == 0 and tr.valid_first_try) or (i == 1 and tr.repaired), "notes": c.notes,
            }, opts.log_path)
    return tr


def _validate(data: dict | None, text: str) -> tuple[Triage | None, str]:
    if data is None:
        return None, f"output was not parseable JSON: {text[:200]!r}"
    try:
        return Triage.model_validate(data), ""
    except ValidationError as e:
        return None, "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()[:10])


def triage_to_dict(t: Triage) -> dict:
    return json.loads(t.model_dump_json())
