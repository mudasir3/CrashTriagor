"""Grade eval outputs against golden/labels.jsonl and write results.md.

    python -m eval.grade eval/out/offline
    python -m eval.grade eval/out/offline eval/out/gemini_gemini-3.8-flash   # merge folders
    python -m eval.grade eval/out/offline --split test                        # held-out crashes only

Splits: "dev" crashes were written alongside rules.yaml and the prompt, so scores on
them are optimistic (the rules were tuned to them). Report "test" numbers: crashes
added after rules and prompt were frozen.

Automatic: category, culprit frame, severity, schema validity, grounding, latency, tokens, cost.
Manual:    fix usefulness. Fill the `grade` column (0/1/2) in fix_grades.csv, re-run.
"""
from __future__ import annotations

import csv
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODE_ORDER = ["rules_only", "baseline", "llm_only", "full"]
SEV = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


def load_labels() -> dict[str, dict]:
    labels = {}
    for line in (ROOT / "golden" / "labels.jsonl").read_text().splitlines():
        if line.strip():
            l = json.loads(line)
            labels[l["id"]] = l
    return labels


def frame_matches(output_frame: dict | None, accepted: list[str]) -> bool:
    """Accept 'Class#method'. Kotlin lambdas compile to Outer$method$1.invoke, so the
    method name embedded in the class counts too."""
    if not output_frame:
        return False
    cls = output_frame.get("class_name") or ""
    base, *inner = cls.split("$")
    methods = {output_frame.get("method") or ""} | {p for p in inner if p and not p.isdigit()}
    for a in accepted:
        a_cls, _, a_m = a.partition("#")
        if base == a_cls and (a_m == "*" or a_m in methods):
            return True
    return False


def pct(n: int, d: int) -> str:
    return f"{100 * n / d:.0f}% ({n}/{d})" if d else "n/a"


def percentile(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    k = (len(xs) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def load_outputs(folders: list[Path]) -> dict[str, dict[str, dict]]:
    """mode -> crash id -> record. A later folder overrides an earlier one per mode."""
    out: dict[str, dict[str, dict]] = defaultdict(dict)
    for folder in folders:
        for mode_dir in sorted(p for p in folder.iterdir() if p.is_dir()):
            for f in sorted(mode_dir.glob("*.json")):
                rec = json.loads(f.read_text())
                out[mode_dir.name][rec["id"]] = rec
    return out


def load_fix_grades(path: Path) -> dict[tuple[str, str], str]:
    if not path.exists():
        return {}
    with path.open() as f:
        return {(r["crash_id"], r["mode"]): r["grade"].strip() for r in csv.DictReader(f)}


def grade(folders: list[Path], split: str | None = None) -> dict:
    labels = load_labels()
    if split:
        labels = {k: v for k, v in labels.items() if v.get("split", "dev") == split}
    outputs = load_outputs(folders)
    target = folders[-1]
    fix_path = target / "fix_grades.csv"
    fix_grades = load_fix_grades(fix_path)
    summary: dict = {}
    per_category: dict[str, dict[str, list[bool]]] = defaultdict(lambda: defaultdict(list))
    failures: dict[str, list[dict]] = defaultdict(list)
    fix_rows = []

    for mode in [m for m in MODE_ORDER if m in outputs] + [m for m in outputs if m not in MODE_ORDER]:
        recs = {k: v for k, v in outputs[mode].items() if k in labels}
        n = len(recs)
        cat_ok = sev_exact = sev_one = frame_ok = frame_n = null_ok = null_n = 0
        valid_first = invalid_final = grounding = 0
        lat, tin, tout, costs = [], [], [], []
        fix_scores = []
        for cid, rec in recs.items():
            lab = labels.get(cid)
            if lab is None:
                continue  # not in this split
            t = rec.get("triage")
            valid_first += bool(rec.get("valid_first_try"))
            if t is None:
                invalid_final += 1
            grounding += bool(rec.get("grounding_flags"))
            if mode != "rules_only":
                lat.append(rec["latency_s"])
                tin.append(rec["input_tokens"])
                tout.append(rec["output_tokens"])
                costs.append(rec.get("cost_usd"))
            ok = bool(t) and t["root_cause_category"] == lab["category"]
            cat_ok += ok
            per_category[lab["category"]][mode].append(ok)
            if t and t["severity"] in SEV:
                d = abs(SEV[t["severity"]] - SEV[lab["severity"]])
                sev_exact += d == 0
                sev_one += d <= 1
            if lab["culprit_frames"]:
                frame_n += 1
                frame_ok += bool(t) and frame_matches(t.get("culprit_frame"), lab["culprit_frames"])
            else:
                null_n += 1
                null_ok += bool(t) and t.get("culprit_frame") is None
            if not ok:
                failures[mode].append({
                    "id": cid, "expected": lab["category"],
                    "got": t["root_cause_category"] if t else f"ERROR: {rec.get('error')}",
                })
            fix_rows.append({
                "crash_id": cid, "mode": mode,
                "fix_summary": (t or {}).get("suggested_fix", {}).get("summary", "") if t else "",
                "expected_fix": lab["expected_fix"],
                "grade": fix_grades.get((cid, mode), ""),
            })
            g = fix_grades.get((cid, mode), "")
            if g in ("0", "1", "2"):
                fix_scores.append(int(g))
        known_costs = [c for c in costs if c is not None]
        summary[mode] = {
            "n": n,
            "category_accuracy": pct(cat_ok, n),
            "frame_accuracy": pct(frame_ok, frame_n),
            "null_culprit_when_expected": pct(null_ok, null_n),
            "severity_exact": pct(sev_exact, n),
            "severity_within_one": pct(sev_one, n),
            "schema_valid_first_try": pct(valid_first, n) if mode != "rules_only" else "n/a (no model)",
            "invalid_after_repair": invalid_final,
            "grounding_flags": grounding,
            "latency_p50_s": f"{statistics.median(lat):.2f}" if lat else "n/a",
            "latency_p95_s": f"{percentile(lat, 0.95):.2f}" if lat else "n/a",
            "avg_tokens_in_out": f"{statistics.mean(tin):.0f} / {statistics.mean(tout):.0f}" if tin else "n/a",
            "cost_per_crash_usd": (f"{statistics.mean(known_costs):.5f}" if known_costs and len(known_costs) == len(costs)
                                   else ("n/a (set prices in triager/pricing.yaml)" if costs else "0 (no model)")),
            "fix_usefulness_0_2": (f"{statistics.mean(fix_scores):.2f} ({len(fix_scores)} graded)" if fix_scores
                                   else "not graded yet"),
        }

    # fix-grade sheet: keep existing grades, add new rows
    with fix_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["crash_id", "mode", "grade", "fix_summary", "expected_fix"])
        w.writeheader()
        for r in sorted(fix_rows, key=lambda r: (r["mode"], r["crash_id"])):
            w.writerow(r)

    result = {"split": split or "all", "summary": summary, "per_category": {c: {m: f"{sum(v)}/{len(v)}" for m, v in ms.items()}
                                                   for c, ms in sorted(per_category.items())},
              "failures": failures}
    (target / "results.json").write_text(json.dumps(result, indent=2))
    (target / "results.md").write_text(to_markdown(result, folders))
    return result


ROWS = [
    ("Root-cause category accuracy", "category_accuracy"),
    ("Culprit frame accuracy", "frame_accuracy"),
    ("Null culprit when no app frame applies", "null_culprit_when_expected"),
    ("Severity exact", "severity_exact"),
    ("Severity within one level", "severity_within_one"),
    ("Fix usefulness (0-2, hand graded)", "fix_usefulness_0_2"),
    ("Schema valid on first try", "schema_valid_first_try"),
    ("Invalid after repair retry", "invalid_after_repair"),
    ("Grounding flags (culprit not in input)", "grounding_flags"),
    ("Latency p50 (s)", "latency_p50_s"),
    ("Latency p95 (s)", "latency_p95_s"),
    ("Avg tokens in / out", "avg_tokens_in_out"),
    ("Cost per crash (USD)", "cost_per_crash_usd"),
]


def to_markdown(result: dict, folders: list[Path]) -> str:
    s = result["summary"]
    modes = list(s)
    lines = [
        "# Eval results",
        "",
        f"Split: **{result['split']}**. Crashes graded: {next(iter(s.values()))['n'] if s else 0}. Sources: "
        + ", ".join(str(f.relative_to(ROOT)) if f.is_relative_to(ROOT) else str(f) for f in folders),
        "",
        "| Metric | " + " | ".join(modes) + " |",
        "| --- | " + " | ".join("---" for _ in modes) + " |",
    ]
    for label, key in ROWS:
        lines.append(f"| {label} | " + " | ".join(str(s[m][key]) for m in modes) + " |")
    lines += ["", "## Per category (correct / total)", "",
              "| Category | " + " | ".join(modes) + " |", "| --- | " + " | ".join("---" for _ in modes) + " |"]
    for cat, ms in result["per_category"].items():
        lines.append(f"| {cat} | " + " | ".join(ms.get(m, "-") for m in modes) + " |")
    lines += ["", "## Category misses", ""]
    for m in modes:
        f = result["failures"].get(m, [])
        lines.append(f"**{m}** ({len(f)}): " + (", ".join(f"{x['id']} (expected {x['expected']}, got {x['got']})" for x in f) or "none"))
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    split = None
    if "--split" in argv:
        i = argv.index("--split")
        split = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    if not argv:
        print(__doc__)
        return 2
    folders = [Path(a).resolve() for a in argv]
    result = grade(folders, split)
    if split == "dev" or (split is None and all(l.get("split", "dev") == "dev" for l in load_labels().values())):
        md = folders[-1] / "results.md"
        md.write_text(md.read_text().replace("# Eval results\n", "# Eval results\n\n> Dev split only: rules.yaml and the prompt were written alongside these crashes, so these numbers are optimistic. Add held-out crashes with \"split\": \"test\" for numbers you can claim.\n", 1))
    print((folders[-1] / "results.md").read_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
