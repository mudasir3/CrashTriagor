import json
import re

import pytest
from conftest import APP, crash

from triager import prompts
from triager.parser import parse
from triager.redact import redact
from triager.rules import apply_rules
from triager.schema import Triage, triage_json_schema


# ------------------------------------------------------------------ redaction

def test_redacts_secrets_and_pii():
    text = ("user mudasir@example.com token=abc123secret GET https://api.example.com/x?page=2&key=1 "
            "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N "
            "ip 192.168.1.20 key AIzaSyA1234567890abcdefghijklmnopqrstu android_id=9774d56d682e549c")
    out, counts = redact(text)
    for secret in ("mudasir@example.com", "abc123secret", "page=2", "eyJhbGci", "192.168.1.20",
                   "AIzaSyA1234567890", "9774d56d682e549c"):
        assert secret not in out
    assert counts


def test_keeps_what_triage_needs():
    native = crash("c30_native_sigsegv")
    out, _ = redact(native)
    assert "0000000000012a4c" in out          # pc values
    assert "fault addr 0x0000000000000010" in out
    anr, _ = redact(crash("c25_anr_lock_contention"))
    assert "<0x0b7e2f11>" in anr              # lock ids
    jvm, _ = redact(crash("c01_fragment_state_loss"))
    assert "CartFragment.kt:88" in jvm        # line numbers


# ------------------------------------------------------------------ rules

EXPECTED_HITS = {
    "c01_fragment_state_loss": "fragment_state_loss",
    "c02_binding_after_destroy_view": "view_binding_after_destroy",
    "c08_network_on_main_login": "network_on_main",
    "c09_strictmode_disk_read": "strictmode_violation",
    "c11_coroutine_uncaught_network": "coroutine_uncaught",
    "c15_transaction_too_large": "transaction_too_large",
    "c23_room_missing_migration": "room_missing_migration",
    "c25_anr_lock_contention": "anr_lock_contention",
    "c27_anr_binder_contentresolver": "anr_binder_main",
    "c29_reflection_class_stripped": "reflection_class_stripped",
    "c30_native_sigsegv": "native_crash",
}


@pytest.mark.parametrize("cid,rule", EXPECTED_HITS.items())
def test_rule_hits(cid, rule):
    hits = [h.id for h in apply_rules(parse(crash(cid), [APP]))]
    assert rule in hits


def test_no_rule_for_plain_npe():
    # c07 has no signature on purpose: the model has to reason
    assert apply_rules(parse(crash("c07_gson_null_field"), [APP])) == []


def test_rules_do_not_cross_fire():
    hits = [h.id for h in apply_rules(parse(crash("c13_bitmap_oom"), [APP]))]
    assert hits == ["oom"]


# ------------------------------------------------------------------ schema

def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def test_schema_is_strict_mode_compatible():
    schema = triage_json_schema()
    assert "$defs" not in json.dumps(schema) and "$ref" not in json.dumps(schema)
    for node in _walk(schema):
        t = node.get("type")
        if t == "object" or (isinstance(t, list) and "object" in t):
            assert node["additionalProperties"] is False
            assert set(node["required"]) == set(node["properties"])


def test_few_shot_examples_validate():
    """The worked examples in the system prompt must themselves match the schema."""
    blobs = re.findall(r"output:\n(\{.*?\})\n\n", prompts.SYSTEM_PROMPT + "\n\n", re.S)
    assert len(blobs) == 2
    for b in blobs:
        Triage.model_validate(json.loads(b))
