import copy
import json

from conftest import APP, crash, mapping

from triager.llm_client import LLMResult
from triager.pipeline import TriageOptions, run
from eval.grade import frame_matches

GOOD = {
    "crash_kind": "jvm_exception",
    "exception": "java.lang.IllegalStateException",
    "root_cause_category": "lifecycle_state",
    "root_cause": "Dialog shown after onSaveInstanceState from a coroutine callback.",
    "culprit_frame": {"class_name": f"{APP}.cart.CartFragment", "method": "showCouponDialog",
                      "file": "CartFragment.kt", "line": 88, "is_app_code": True},
    "severity": "P1",
    "severity_reason": "Inferred from the trace alone: cart flow.",
    "confidence": "high",
    "suggested_fix": {"summary": "Collect with repeatOnLifecycle(STARTED).", "steps": ["a", "b"], "code_snippet": None},
    "needs_more_info": [],
    "rule_hits": ["fragment_state_loss"],
}


class FakeClient:
    """Returns queued payloads and records what it was sent."""

    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.sent = []

    def complete(self, system, messages, schema=None, stream=False, on_delta=None):
        self.sent.append({"system": system, "messages": messages, "schema": schema})
        p = self.payloads.pop(0)
        text = p if isinstance(p, str) else json.dumps(p)
        data = None if isinstance(p, str) else p
        return LLMResult(text, data, 1000, 200, 0.5, "fake", "fake-1")


def opts(**kw):
    return TriageOptions(app_packages=[APP], log_path=None, **kw)


def test_rules_only_needs_no_client():
    tr = run(crash("c01_fragment_state_loss"), opts(mode="rules_only"))
    assert tr.triage.root_cause_category.value == "lifecycle_state"
    assert tr.triage.culprit_frame.method == "showCouponDialog"
    assert tr.calls == []


def test_full_mode_happy_path_sends_parsed_and_hints():
    client = FakeClient(GOOD)
    tr = run(crash("c01_fragment_state_loss"), opts(mode="full"), client)
    assert tr.valid_first_try and not tr.repaired and tr.grounding_flags == []
    user = client.sent[0]["messages"][0]["content"]
    assert "<parsed>" in user and "fragment_state_loss" in user
    assert "abc123secret" not in user  # redaction happened before sending


def test_llm_only_mode_sends_raw_log_without_parse():
    client = FakeClient(GOOD)
    run(crash("c01_fragment_state_loss"), opts(mode="llm_only"), client)
    user = client.sent[0]["messages"][0]["content"]
    assert "<parsed>" not in user and "<rule_hints>" not in user


def test_invalid_output_is_repaired_once():
    bad = copy.deepcopy(GOOD)
    bad["severity"] = "critical"  # not in the enum
    client = FakeClient(bad, GOOD)
    tr = run(crash("c01_fragment_state_loss"), opts(mode="full"), client)
    assert tr.valid_first_try is False and tr.repaired is True
    assert "severity" in client.sent[1]["messages"][0]["content"]
    assert tr.triage.severity.value == "P1"


def test_gives_up_after_one_repair():
    client = FakeClient("not json at all", "still not json")
    tr = run(crash("c01_fragment_state_loss"), opts(mode="full"), client)
    assert tr.triage is None and "invalid output" in tr.error
    assert len(tr.calls) == 2


def test_hallucinated_culprit_is_flagged():
    fake = copy.deepcopy(GOOD)
    fake["culprit_frame"]["class_name"] = f"{APP}.checkout.MadeUpPresenter"
    tr = run(crash("c01_fragment_state_loss"), opts(mode="full"), FakeClient(fake))
    assert tr.grounding_flags == ["culprit_class_not_in_input"]
    assert tr.triage.confidence.value == "low"


def test_mapping_applies_in_full_mode():
    client = FakeClient(GOOD)
    run(crash("c28_obfuscated_with_mapping"), opts(mode="full", mapping_text=mapping("c28_obfuscated_with_mapping")), client)
    assert "QueryValidator.isValid" in client.sent[0]["messages"][0]["content"]


def test_dry_run_calls_nothing():
    tr = run(crash("c01_fragment_state_loss"), opts(mode="full", dry_run=True))
    assert tr.request_preview["schema"]["type"] == "object"
    assert tr.calls == []


def test_impact_overrides_rules_severity():
    tr = run(crash("c18_dialog_dismiss_detached"), opts(mode="rules_only", impact={"crash_count": 3, "affected_users": 2}))
    assert tr.triage.severity.value == "P3"


def test_frame_matching_handles_kotlin_lambdas():
    out = {"class_name": f"{APP}.list.ProductListFragment$onItemClick$1", "method": "invoke"}
    assert frame_matches(out, [f"{APP}.list.ProductListFragment#onItemClick"])
    assert not frame_matches(out, [f"{APP}.list.ProductAdapter#bind"])
    assert not frame_matches(None, [f"{APP}.x#y"])
