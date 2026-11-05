"""Deterministic crash parsing. No LLM here: everything in this file is unit-tested.

Handles three shapes of input:
  * JVM exceptions (plain trace, or buried in a logcat dump)
  * ANR thread dumps (traces.txt / bugreport style)
  * native tombstones (detected and summarised, not symbolicated)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict

# "11-02 14:03:11.482 12345 12345 E AndroidRuntime: ..."  (threadtime format)
LOGCAT_THREADTIME = re.compile(
    r"^\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3}\s+\d+\s+\d+\s+[VDIWEFA]\s+([^:]+?)\s*:\s?(.*)$"
)
# "E/AndroidRuntime( 1234): ..."  (brief format)
LOGCAT_BRIEF = re.compile(r"^[VDIWEFA]/([^(]+?)\(\s*\d+\):\s?(.*)$")

FRAME = re.compile(r"^\s*at\s+([\w$.<>\-]+)\.([\w$<>\-]+)\(([^)]*)\)")
CAUSED_BY = re.compile(r"^\s*Caused by:\s+(.*)$")
EXC_HEADER = re.compile(r"^([a-zA-Z_][\w$]*(?:\.[\w$]+)+)(?::\s?(.*))?$")
MORE = re.compile(r"^\s*\.\.\.\s+\d+\s+more")
SUPPRESSED = re.compile(r"^\s*Suppressed:\s+(.*)$")

FRAMEWORK_PREFIXES = (
    "android.", "java.", "javax.", "kotlin.", "dalvik.", "libcore.", "sun.", "jdk.",
    "com.android.internal.", "com.android.server.", "org.apache.harmony.",
)
ANDROIDX_PREFIXES = ("androidx.", "kotlinx.", "com.google.android.material.")
SDK_PREFIXES = (
    "com.google.", "okhttp3.", "okio.", "retrofit2.", "com.squareup.", "io.reactivex.",
    "com.facebook.", "com.bumptech.", "coil.", "io.ktor.", "com.airbnb.", "dagger.",
    "com.jakewharton.", "io.sentry.", "com.firebase.", "org.json.",
)


@dataclass
class Frame:
    class_name: str
    method: str
    location: str  # "Foo.kt:42", "SourceFile:3", "Native Method", "Unknown Source"
    origin: str = "unknown"  # app | androidx | framework | sdk | unknown
    file: str | None = None
    line: int | None = None

    @property
    def short(self) -> str:
        return f"{self.class_name}.{self.method}({self.location})"


@dataclass
class ExceptionLink:
    type: str
    message: str
    frames: list[Frame] = field(default_factory=list)


@dataclass
class ThreadDump:
    name: str
    tid: int | None
    state: str
    frames: list[Frame] = field(default_factory=list)
    waiting_on: str | None = None  # "<0x0b7e2f11> (a java.lang.Object)"
    held_by_tid: int | None = None


@dataclass
class ParsedCrash:
    kind: str  # jvm_exception | anr | native | unknown
    raw_block: str
    thread: str | None = None
    process: str | None = None
    chain: list[ExceptionLink] = field(default_factory=list)
    obfuscated: bool = False
    anr_reason: str | None = None
    main_thread: ThreadDump | None = None
    lock_holder: ThreadDump | None = None
    native_signal: str | None = None
    native_abort: str | None = None
    native_backtrace: list[str] = field(default_factory=list)
    retraced: bool = False

    @property
    def primary(self) -> ExceptionLink | None:
        """Deepest cause: usually the real exception; the top one is often a wrapper."""
        return self.chain[-1] if self.chain else None

    def all_frames(self) -> list[Frame]:
        out: list[Frame] = []
        for link in self.chain:
            out.extend(link.frames)
        if self.main_thread:
            out.extend(self.main_thread.frames)
        if self.lock_holder:
            out.extend(self.lock_holder.frames)
        return out

    def first_app_frame(self) -> Frame | None:
        if self.kind == "anr":
            for t in (self.main_thread, self.lock_holder):
                if t:
                    for f in t.frames:
                        if f.origin == "app":
                            return f
            return None
        # deepest cause first, then walk up the chain
        for link in reversed(self.chain):
            for f in link.frames:
                if f.origin == "app":
                    return f
        return None

    def to_prompt_dict(self) -> dict:
        """Compact, model-friendly view. Frames beyond the first 12 per link are cut:
        the raw block is also sent, so nothing is truly lost."""
        def frames(fs: list[Frame], limit: int = 12) -> list[str]:
            out = [f"[{f.origin.upper()}] {f.short}" for f in fs[:limit]]
            if len(fs) > limit:
                out.append(f"... {len(fs) - limit} more frames")
            return out

        d: dict = {"kind": self.kind, "obfuscated": self.obfuscated, "retraced": self.retraced}
        if self.thread:
            d["thread"] = self.thread
        if self.chain:
            d["exception_chain"] = [
                {"type": l.type, "message": l.message, "frames": frames(l.frames)} for l in self.chain
            ]
            d["primary_exception"] = self.primary.type
        app = self.first_app_frame()
        d["first_app_frame"] = app.short if app else None
        if self.kind == "anr":
            d["anr_reason"] = self.anr_reason
            if self.main_thread:
                d["main_thread"] = {
                    "state": self.main_thread.state,
                    "waiting_on": self.main_thread.waiting_on,
                    "held_by_tid": self.main_thread.held_by_tid,
                    "frames": frames(self.main_thread.frames),
                }
            if self.lock_holder:
                d["lock_holder"] = {
                    "name": self.lock_holder.name,
                    "tid": self.lock_holder.tid,
                    "state": self.lock_holder.state,
                    "frames": frames(self.lock_holder.frames),
                }
        if self.kind == "native":
            d["signal"] = self.native_signal
            d["abort_message"] = self.native_abort
            d["backtrace"] = self.native_backtrace[:12]
        return d

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- helpers

def strip_logcat_prefix(line: str) -> tuple[str | None, str]:
    """Returns (tag, message). tag is None when the line has no logcat prefix."""
    m = LOGCAT_THREADTIME.match(line) or LOGCAT_BRIEF.match(line)
    if m:
        return m.group(1).strip(), m.group(2)
    return None, line


def classify_origin(class_name: str, app_packages: list[str]) -> str:
    if app_packages and any(class_name == p or class_name.startswith(p + ".") for p in app_packages):
        return "app"
    if class_name.startswith(FRAMEWORK_PREFIXES):
        return "framework"
    if class_name.startswith(ANDROIDX_PREFIXES):
        return "androidx"
    if class_name.startswith(SDK_PREFIXES):
        return "sdk"
    # No package given: anything unrecognised is most likely the app itself.
    return "app" if not app_packages else "unknown"


def parse_frame(line: str, app_packages: list[str]) -> Frame | None:
    m = FRAME.match(line)
    if not m:
        return None
    cls, method, loc = m.group(1), m.group(2), m.group(3)
    file, lineno = None, None
    if ":" in loc:
        f, _, n = loc.rpartition(":")
        file = f or None
        lineno = int(n) if n.isdigit() else None
    elif loc and loc not in ("Native Method", "Unknown Source"):
        file = loc
    return Frame(cls, method, loc, classify_origin(cls, app_packages), file, lineno)


def _looks_obfuscated(frames: list[Frame]) -> bool:
    if len(frames) < 2:
        return False
    def obf(f: Frame) -> bool:
        if f.origin in ("framework", "androidx"):
            return False
        parts = f.class_name.split(".")
        short_parts = sum(1 for p in parts if len(p.split("$")[0]) <= 2)
        return (short_parts >= 2 and len(f.method) <= 2) or f.location in ("SourceFile", "Unknown Source") and len(f.method) <= 2
    candidates = [f for f in frames if f.origin not in ("framework", "androidx")]
    if not candidates:
        return False
    return sum(obf(f) for f in candidates) / len(candidates) >= 0.4


# ---------------------------------------------------------------- detection

def detect_kind(text: str) -> str:
    if re.search(r"\*\*\* \*\*\* \*\*\*|signal \d+ \(SIG\w+\)|Fatal signal \d+", text):
        return "native"
    if re.search(r"^\s*\"main\" prio=\d+ tid=\d+", text, re.M) or re.search(r"\bANR in\b", text):
        return "anr"
    if re.search(r"^\s*at\s+[\w$.]+\(", text, re.M) or re.search(r"FATAL EXCEPTION", text):
        return "jvm_exception"
    return "unknown"


def extract_crash_block(text: str) -> str:
    """From a noisy logcat dump keep only the crash. Plain traces pass through."""
    lines = text.splitlines()
    cleaned: list[str] = []
    in_crash = False
    for raw in lines:
        tag, msg = strip_logcat_prefix(raw)
        if tag is None:
            cleaned.append(raw)
            continue
        if "FATAL EXCEPTION" in msg or tag in ("AndroidRuntime", "DEBUG", "StrictMode", "ActivityManager", "libc"):
            if "FATAL EXCEPTION" in msg:
                in_crash = True
                cleaned = []  # start fresh at the crash; earlier lines were noise
            cleaned.append(msg)
        elif in_crash and tag == "AndroidRuntime":
            cleaned.append(msg)
        # every other tagged line is noise
    block = "\n".join(cleaned).strip("\n")
    return block if block.strip() else text


# ---------------------------------------------------------------- JVM

def parse_jvm(block: str, app_packages: list[str]) -> ParsedCrash:
    pc = ParsedCrash(kind="jvm_exception", raw_block=block)
    current: ExceptionLink | None = None
    in_suppressed = False
    for line in block.splitlines():
        s = line.strip()
        if not s:
            continue
        m = re.match(r"FATAL EXCEPTION:\s*(.+)", s)
        if m:
            pc.thread = m.group(1).strip()
            continue
        m = re.match(r"Process:\s*([\w.:]+)", s)
        if m:
            pc.process = m.group(1)
            continue
        m = re.match(r'Exception in thread "([^"]+)"\s+(.*)', s)
        if m:
            pc.thread = m.group(1)
            s = m.group(2)
        if SUPPRESSED.match(s):
            in_suppressed = True
            continue
        cb = CAUSED_BY.match(s)
        if cb:
            in_suppressed = False
            current = _new_link(cb.group(1))
            pc.chain.append(current)
            continue
        if line.lstrip().startswith("at "):
            if in_suppressed or current is None:
                continue
            f = parse_frame(s, app_packages)
            if f:
                current.frames.append(f)
            continue
        if MORE.match(s):
            continue
        sm = re.search(r"StrictMode policy violation;.*?:\s*([\w.$]+)", s)
        if sm and current is None:
            current = ExceptionLink(type=sm.group(1), message="StrictMode policy violation (not a crash)")
            pc.chain.append(current)
            continue
        if current is None:
            hm = EXC_HEADER.match(s)
            if hm and "." in hm.group(1):
                current = _new_link(s)
                pc.chain.append(current)
            continue
        # a non-frame line right after a header with no frames yet = message continuation
        if current is not None and not current.frames and not in_suppressed:
            current.message = (current.message + " " + s).strip()
    pc.obfuscated = _looks_obfuscated(pc.all_frames())
    return pc


def _new_link(header: str) -> ExceptionLink:
    t, _, msg = header.partition(": ")
    if not msg and t.endswith(":"):
        t = t[:-1]
    return ExceptionLink(type=t.strip(), message=msg.strip())


# ---------------------------------------------------------------- ANR

THREAD_HEADER = re.compile(r'^"([^"]+)"(?:\s+daemon)?\s+prio=\d+\s+tid=(\d+)\s+(\w+)')
WAITING = re.compile(r"-\s+waiting to lock\s+(<0x[0-9a-f]+>\s+\([^)]*\))(?:\s+held by thread\s+(\d+))?")


def parse_anr(block: str, app_packages: list[str]) -> ParsedCrash:
    pc = ParsedCrash(kind="anr", raw_block=block, thread="main")
    m = re.search(r"Reason:\s*(.+)", block)
    if m:
        pc.anr_reason = m.group(1).strip()
    threads: list[ThreadDump] = []
    cur: ThreadDump | None = None
    for line in block.splitlines():
        _, line = strip_logcat_prefix(line)
        h = THREAD_HEADER.match(line.strip())
        if h:
            cur = ThreadDump(name=h.group(1), tid=int(h.group(2)), state=h.group(3))
            threads.append(cur)
            continue
        if cur is None:
            continue
        if not line.strip():
            cur = None
            continue
        w = WAITING.search(line)
        if w:
            cur.waiting_on = w.group(1)
            cur.held_by_tid = int(w.group(2)) if w.group(2) else None
            continue
        f = parse_frame(line.strip(), app_packages)
        if f:
            cur.frames.append(f)
    pc.main_thread = next((t for t in threads if t.name == "main"), None)
    if pc.main_thread and pc.main_thread.held_by_tid is not None:
        pc.lock_holder = next((t for t in threads if t.tid == pc.main_thread.held_by_tid), None)
    pc.obfuscated = _looks_obfuscated(pc.all_frames())
    return pc


# ---------------------------------------------------------------- native

def parse_native(block: str) -> ParsedCrash:
    pc = ParsedCrash(kind="native", raw_block=block)
    for line in block.splitlines():
        _, line = strip_logcat_prefix(line)
        s = line.strip()
        m = re.search(r"(signal \d+ \(SIG\w+\).*)", s)
        if m and not pc.native_signal:
            pc.native_signal = m.group(1)
        m = re.search(r"Abort message:\s*'?(.*?)'?$", s)
        if m:
            pc.native_abort = m.group(1)
        if re.match(r"#\d+\s+pc\s+[0-9a-f]+", s):
            pc.native_backtrace.append(s)
        m = re.search(r">>> ([\w.:]+) <<<", s)
        if m:
            pc.process = m.group(1)
    return pc


# ---------------------------------------------------------------- entry

def parse(text: str, app_packages: list[str] | None = None) -> ParsedCrash:
    app_packages = app_packages or []
    kind = detect_kind("\n".join(strip_logcat_prefix(l)[1] for l in text.splitlines()))
    if kind == "native":
        return parse_native(text)
    if kind == "anr":
        return parse_anr(text, app_packages)
    block = extract_crash_block(text)
    if kind == "jvm_exception":
        return parse_jvm(block, app_packages)
    return ParsedCrash(kind="unknown", raw_block=block)
