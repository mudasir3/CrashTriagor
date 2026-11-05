"""Minimal R8/ProGuard retrace from mapping.txt.

Covers the common case (class rename, method rename, line ranges). For anything
unusual (inlining chains, outlines) use the official `retrace` tool from the
Android SDK command-line tools and pass the retraced text in instead.

mapping.txt format:
    com.example.Foo -> a.b:
        12:15:void bar(java.lang.String):40:43 -> a
        int count -> c
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .parser import Frame, ParsedCrash, classify_origin

CLASS_LINE = re.compile(r"^(\S+) -> (\S+):$")
METHOD_LINE = re.compile(
    r"^\s+(?:(\d+):(\d+):)?[\w.$\[\]<>]+ ([\w$<>]+)\([^)]*\)(?::(\d+)(?::(\d+))?)? -> ([\w$<>]+)$"
)


@dataclass
class MethodMapping:
    original: str
    obf_start: int | None
    obf_end: int | None
    orig_start: int | None
    orig_end: int | None


@dataclass
class ClassMapping:
    original: str
    methods: dict[str, list[MethodMapping]] = field(default_factory=dict)


def load_mapping(text: str) -> dict[str, ClassMapping]:
    classes: dict[str, ClassMapping] = {}
    cur: ClassMapping | None = None
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        m = CLASS_LINE.match(line)
        if m:
            cur = ClassMapping(original=m.group(1))
            classes[m.group(2)] = cur
            continue
        m = METHOD_LINE.match(line)
        if m and cur:
            s, e, name, os_, oe, obf = m.groups()
            mm = MethodMapping(
                original=name,
                obf_start=int(s) if s else None,
                obf_end=int(e) if e else None,
                orig_start=int(os_) if os_ else None,
                orig_end=int(oe) if oe else (int(os_) if os_ else None),
            )
            cur.methods.setdefault(obf, []).append(mm)
    return classes


def _retrace_frame(f: Frame, mapping: dict[str, ClassMapping], app_packages: list[str]) -> Frame:
    outer, sep, inner = f.class_name.partition("$")
    cm = mapping.get(f.class_name) or mapping.get(outer)
    if not cm:
        return f
    cls = cm.original if f.class_name in mapping else cm.original + sep + inner
    method, line = f.method, f.line
    candidates = cm.methods.get(f.method, [])
    if candidates:
        chosen = candidates[0]
        if f.line is not None:
            for c in candidates:
                if c.obf_start is not None and c.obf_start <= f.line <= (c.obf_end or c.obf_start):
                    chosen = c
                    break
        method = chosen.original
        if f.line is not None and chosen.obf_start is not None and chosen.orig_start is not None:
            line = chosen.orig_start + (f.line - chosen.obf_start)
    simple = cls.split(".")[-1].split("$")[0]
    file = f"{simple}.kt"  # R8 strips source names; Kotlin is the likely source in a modern app
    loc = f"{file}:{line}" if line is not None else file
    return Frame(cls, method, loc, classify_origin(cls, app_packages), file, line)


def retrace(parsed: ParsedCrash, mapping_text: str, app_packages: list[str]) -> ParsedCrash:
    mapping = load_mapping(mapping_text)
    for link in parsed.chain:
        link.frames = [_retrace_frame(f, mapping, app_packages) for f in link.frames]
        # messages often mention obfuscated class names too
        for obf, cm in mapping.items():
            if "." not in obf:  # a bare "a" would rewrite ordinary words
                continue
            link.message = re.sub(rf"\b{re.escape(obf)}\b", cm.original, link.message)
    for t in (parsed.main_thread, parsed.lock_holder):
        if t:
            t.frames = [_retrace_frame(f, mapping, app_packages) for f in t.frames]
    parsed.retraced = True
    parsed.obfuscated = False
    return parsed
