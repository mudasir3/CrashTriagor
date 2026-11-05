"""Output schema for a crash triage. Pydantic is the source of truth; the JSON
schema sent to the model is derived from it, so the two can never drift."""
from __future__ import annotations

import copy
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class CrashKind(str, Enum):
    jvm_exception = "jvm_exception"
    anr = "anr"
    native = "native"
    unknown = "unknown"


class RootCauseCategory(str, Enum):
    null_safety = "null_safety"
    lifecycle_state = "lifecycle_state"
    main_thread_violation = "main_thread_violation"
    concurrency = "concurrency"
    coroutine = "coroutine"
    memory = "memory"
    resources = "resources"
    permission_security = "permission_security"
    parcel_binder = "parcel_binder"
    database = "database"
    network = "network"
    window_ui = "window_ui"
    compose_runtime = "compose_runtime"
    anr_blocking = "anr_blocking"
    r8_build_config = "r8_build_config"
    third_party_sdk = "third_party_sdk"
    native = "native"
    unknown = "unknown"


class Severity(str, Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class Confidence(str, Enum):
    high = "high"
    medium = "medium"
    low = "low"


class CulpritFrame(BaseModel):
    class_name: str = Field(description="Fully qualified class of the frame most responsible for the crash")
    method: str
    file: str | None = None
    line: int | None = None
    is_app_code: bool


class SuggestedFix(BaseModel):
    summary: str = Field(description="One sentence naming the concrete change")
    steps: list[str] = Field(description="2-5 concrete steps")
    code_snippet: str | None = Field(default=None, description="Short Kotlin snippet, or null")


class Triage(BaseModel):
    crash_kind: CrashKind
    exception: str = Field(description="Primary exception type, e.g. java.lang.IllegalStateException")
    root_cause_category: RootCauseCategory
    root_cause: str = Field(description="One or two sentences: the actual cause, not the symptom")
    culprit_frame: CulpritFrame | None = Field(
        default=None, description="null when no frame in the input is responsible (e.g. all framework, native without symbols)"
    )
    severity: Severity
    severity_reason: str
    confidence: Confidence
    suggested_fix: SuggestedFix
    needs_more_info: list[str] = Field(default_factory=list)
    rule_hits: list[str] = Field(default_factory=list, description="Ids of rule hints you agreed with")


SEVERITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


def _inline_refs(node: Any, defs: dict) -> Any:
    if isinstance(node, dict):
        if "$ref" in node:
            name = node["$ref"].split("/")[-1]
            return _inline_refs(copy.deepcopy(defs[name]), defs)
        return {k: _inline_refs(v, defs) for k, v in node.items() if k != "$defs"}
    if isinstance(node, list):
        return [_inline_refs(v, defs) for v in node]
    return node


def _strictify(node: Any) -> Any:
    """Make the schema acceptable to strict structured-output modes:
    every object closed, every property required, nullability via type arrays."""
    if isinstance(node, list):
        return [_strictify(v) for v in node]
    if not isinstance(node, dict):
        return node
    node = {k: _strictify(v) for k, v in node.items() if k not in ("title", "default")}
    # Optional[X] comes out of pydantic as anyOf [X, {type: null}]
    if "anyOf" in node and len(node["anyOf"]) == 2 and {"type": "null"} in node["anyOf"]:
        other = next(o for o in node["anyOf"] if o != {"type": "null"})
        merged = {k: v for k, v in node.items() if k != "anyOf"}
        merged.update(other)
        t = merged.get("type")
        if isinstance(t, str):
            merged["type"] = [t, "null"]
        if "enum" in merged and None not in merged["enum"]:
            merged["enum"] = merged["enum"] + [None]
        node = merged
    if node.get("type") == "object" or (isinstance(node.get("type"), list) and "object" in node["type"]):
        props = node.get("properties", {})
        node["required"] = list(props.keys())
        node["additionalProperties"] = False
    return node


def triage_json_schema() -> dict:
    raw = Triage.model_json_schema()
    defs = raw.get("$defs", {})
    return _strictify(_inline_refs(raw, defs))
