"""Run the golden set through one or more modes and save every output.

    python -m eval.run_eval --modes rules_only                     # free, offline
    python -m eval.run_eval --modes baseline,llm_only,full --sleep 4
    python -m eval.run_eval --modes full --provider anthropic --tag haiku

Resumable: a crash whose output file already exists is skipped, so a run
interrupted by free-tier rate limits picks up where it stopped.
Then grade:  python -m eval.grade eval/out/<tag>
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from triager import prompts
from triager.llm_client import LLMClient, LLMConfig, LLMError
from triager.pipeline import MODES, TriageOptions, run, triage_to_dict

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "golden"
APP_PACKAGE = "com.example.triagedemo"


def load_labels() -> list[dict]:
    return [json.loads(l) for l in (GOLDEN / "labels.jsonl").read_text().splitlines() if l.strip()]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--modes", default="rules_only", help=f"comma list of {','.join(MODES)}")
    ap.add_argument("--provider")
    ap.add_argument("--model")
    ap.add_argument("--tag", help="output folder name; default derived from provider/model")
    ap.add_argument("--limit", type=int, help="only the first N crashes (smoke test)")
    ap.add_argument("--only", help="comma list of crash ids")
    ap.add_argument("--sleep", type=float, default=0.0, help="seconds between calls (free-tier rate limits)")
    args = ap.parse_args(argv)

    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    bad = [m for m in modes if m not in MODES]
    if bad:
        print(f"unknown mode(s): {bad}", file=sys.stderr)
        return 2

    client, provider, model = None, "none", "none"
    if any(m != "rules_only" for m in modes):
        try:
            cfg = LLMConfig.from_env(args.provider, args.model)
        except LLMError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        client, provider, model = LLMClient(cfg), cfg.provider, cfg.model

    tag = args.tag or (f"{provider}_{model}".replace("/", "-") if client else "offline")
    out_root = ROOT / "eval" / "out" / tag
    labels = load_labels()
    if args.only:
        wanted = set(args.only.split(","))
        labels = [l for l in labels if l["id"] in wanted]
    if args.limit:
        labels = labels[: args.limit]

    for mode in modes:
        out_dir = out_root / mode
        out_dir.mkdir(parents=True, exist_ok=True)
        done = skipped = failed = 0
        for lab in labels:
            out_file = out_dir / f"{lab['id']}.json"
            if out_file.exists():
                skipped += 1
                continue
            text = (GOLDEN / "crashes" / f"{lab['id']}.txt").read_text()
            mapping = GOLDEN / "mappings" / f"{lab['id']}.txt"
            opts = TriageOptions(
                mode=mode, app_packages=[APP_PACKAGE],
                mapping_text=mapping.read_text() if mapping.exists() else None,
                impact=lab.get("impact"), log_path=out_root / "runs.jsonl",
                crash_id=lab["id"], run_id=tag,
            )
            try:
                tr = run(text, opts, client)
            except LLMError as e:
                # leave no file: the next run retries this crash
                print(f"  [{mode}] {lab['id']}: {e}", file=sys.stderr)
                failed += 1
                if "HTTP 4" in str(e) and "429" not in str(e):
                    return 1  # auth/config problem: stop instead of burning through the set
                continue
            record = {
                "id": lab["id"], "mode": mode, "provider": provider if mode != "rules_only" else "none",
                "model": model if mode != "rules_only" else "none", "prompt_version": prompts.PROMPT_VERSION,
                "triage": triage_to_dict(tr.triage) if tr.triage else None, "error": tr.error,
                "valid_first_try": tr.valid_first_try, "repaired": tr.repaired,
                "input_tokens": tr.input_tokens, "output_tokens": tr.output_tokens,
                "latency_s": round(tr.latency_s, 3), "cost_usd": tr.cost_usd,
                "grounding_flags": tr.grounding_flags, "rule_hits": [h.id for h in tr.hits],
                "notes": sorted({n for c in tr.calls for n in c.notes}),
            }
            out_file.write_text(json.dumps(record, indent=2))
            done += 1
            print(f"  [{mode}] {lab['id']}: "
                  f"{record['triage']['root_cause_category'] if record['triage'] else 'ERROR'}"
                  f"{'' if mode == 'rules_only' else f'  {tr.latency_s:.1f}s'}")
            if args.sleep and mode != "rules_only":
                time.sleep(args.sleep)
        print(f"{mode}: {done} new, {skipped} already done, {failed} failed -> {out_dir}")
    print(f"\nGrade with:  python -m eval.grade {out_root.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
