"""Command line entry point.

    triage crash.txt
    adb logcat -d | triage - --package com.example.app
    triage crash.txt --json
    triage crash.txt --stream
    triage crash.txt --mapping app/build/outputs/mapping/release/mapping.txt
    triage crash.txt --dry-run          # show exactly what would be sent, call nothing
    triage crashes/ --mode rules_only   # batch a directory
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .llm_client import LLMClient, LLMConfig, LLMError, PRESETS
from .pipeline import MODES, TriageOptions, TriageRun, run, triage_to_dict

SEV_COLOR = {"P0": "\033[1;31m", "P1": "\033[31m", "P2": "\033[33m", "P3": "\033[36m"}
DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"


def _c(code: str) -> str:
    return code if sys.stdout.isatty() and not os.getenv("NO_COLOR") else ""


def render(tr: TriageRun) -> str:
    if tr.error:
        return f"Triage failed: {tr.error}"
    t = tr.triage
    sev = t.severity.value
    lines = [
        f"{_c(SEV_COLOR[sev])}{sev}{_c(RESET)}  {_c(BOLD)}{t.root_cause_category.value}{_c(RESET)}"
        f"  ({t.confidence.value} confidence)  {t.exception}",
        "",
        f"{_c(BOLD)}Root cause{_c(RESET)}  {t.root_cause}",
    ]
    if t.culprit_frame:
        cf = t.culprit_frame
        loc = f"{cf.file}:{cf.line}" if cf.file and cf.line else (cf.file or "")
        lines.append(f"{_c(BOLD)}Culprit{_c(RESET)}     {cf.class_name}.{cf.method}  {_c(DIM)}{loc}{_c(RESET)}")
    lines.append(f"{_c(BOLD)}Severity{_c(RESET)}    {t.severity_reason}")
    lines += ["", f"{_c(BOLD)}Fix{_c(RESET)}  {t.suggested_fix.summary}"]
    steps = [s for s in t.suggested_fix.steps if s.strip() != t.suggested_fix.summary.strip()]
    lines += [f"  {i}. {s}" for i, s in enumerate(steps, 1)]
    if t.suggested_fix.code_snippet:
        lines += ["", *("    " + l for l in t.suggested_fix.code_snippet.splitlines())]
    if t.needs_more_info:
        lines += ["", f"{_c(BOLD)}Needs{_c(RESET)}  " + "; ".join(t.needs_more_info)]
    meta = [f"mode={tr.mode}"]
    if tr.hits:
        meta.append("rules=" + ",".join(h.id for h in tr.hits))
    if tr.calls:
        c = tr.calls[-1]
        meta.append(f"{c.provider}/{c.model}")
        meta.append(f"{tr.input_tokens}+{tr.output_tokens} tok")
        meta.append(f"{tr.latency_s:.2f}s")
        meta.append("cost=" + (f"${tr.cost_usd:.5f}" if tr.cost_usd is not None else "n/a (no price set)"))
        if tr.repaired:
            meta.append("repaired")
    if tr.redactions:
        meta.append("redacted=" + ",".join(f"{k}:{v}" for k, v in tr.redactions.items()))
    if tr.grounding_flags:
        meta.append("grounding=" + ",".join(tr.grounding_flags))
    lines += ["", _c(DIM) + "  ".join(meta) + _c(RESET)]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="triage", description="Triage an Android crash log.")
    p.add_argument("input", help="crash file, directory of .txt crashes, or - for stdin")
    p.add_argument("--mode", choices=MODES, default="full")
    p.add_argument("--provider", choices=list(PRESETS), default=None, help="default: $TRIAGER_PROVIDER or gemini")
    p.add_argument("--model", default=None, help="default: $TRIAGER_MODEL or the provider preset")
    p.add_argument("--package", action="append", default=[], help="app package prefix; repeatable")
    p.add_argument("--mapping", type=Path, help="R8 mapping.txt for retrace")
    p.add_argument("--crash-count", type=int)
    p.add_argument("--affected-users", type=int)
    p.add_argument("--on-startup", action="store_true")
    p.add_argument("--json", action="store_true", help="print the triage JSON only")
    p.add_argument("--stream", action="store_true", help="stream model output as it arrives")
    p.add_argument("--dry-run", action="store_true", help="print the request instead of sending it")
    p.add_argument("--log", type=Path, default=Path("runs.jsonl"))
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    impact = {k: v for k, v in {
        "crash_count": args.crash_count, "affected_users": args.affected_users,
        "on_startup": True if args.on_startup else None,
    }.items() if v is not None} or None

    if args.input == "-":
        inputs = [("stdin", sys.stdin.read())]
    else:
        path = Path(args.input)
        files = sorted(path.glob("*.txt")) if path.is_dir() else [path]
        inputs = [(f.stem, f.read_text(errors="replace")) for f in files]
    mapping_text = args.mapping.read_text() if args.mapping else None

    client = None
    if args.mode != "rules_only" and not args.dry_run:
        try:
            client = LLMClient(LLMConfig.from_env(args.provider, args.model))
        except LLMError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2

    def on_delta(piece: str) -> None:
        sys.stderr.write(_c(DIM) + piece + _c(RESET))
        sys.stderr.flush()

    exit_code = 0
    for name, text in inputs:
        opts = TriageOptions(
            mode=args.mode, app_packages=args.package, mapping_text=mapping_text, impact=impact,
            stream=args.stream, on_delta=on_delta if args.stream else None, dry_run=args.dry_run,
            log_path=args.log, crash_id=name,
        )
        try:
            tr = run(text, opts, client)
        except LLMError as e:
            print(f"error ({name}): {e}", file=sys.stderr)
            exit_code = 1
            continue
        if args.stream:
            sys.stderr.write("\n\n")
        if len(inputs) > 1 and not args.json:
            print(f"\n=== {name} ===")
        if args.dry_run:
            print(f"--- system ---\n{tr.request_preview['system']}\n\n--- user ---\n{tr.request_preview['user']}")
            print(f"\n--- redactions --- {tr.redactions or 'none'}")
            continue
        if args.json:
            print(json.dumps(triage_to_dict(tr.triage) if tr.triage else {"error": tr.error}, indent=2))
        else:
            print(render(tr))
        if tr.error:
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
