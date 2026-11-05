from conftest import APP, crash, mapping

from triager.parser import classify_origin, detect_kind, extract_crash_block, parse, strip_logcat_prefix
from triager.retrace import load_mapping, retrace


def test_logcat_prefix_threadtime_and_brief():
    assert strip_logcat_prefix("11-02 14:03:11.482 12345 12345 E AndroidRuntime: FATAL EXCEPTION: main") == (
        "AndroidRuntime", "FATAL EXCEPTION: main")
    assert strip_logcat_prefix("E/AndroidRuntime( 1234): at foo.Bar.baz(Bar.kt:1)") == (
        "AndroidRuntime", "at foo.Bar.baz(Bar.kt:1)")
    assert strip_logcat_prefix("plain line") == (None, "plain line")


def test_extract_drops_logcat_noise():
    block = extract_crash_block(crash("c01_fragment_state_loss"))
    assert block.startswith("FATAL EXCEPTION: main")
    assert "OkHttp" not in block and "Choreographer" not in block


def test_origin_tagging():
    assert classify_origin(f"{APP}.cart.CartFragment", [APP]) == "app"
    assert classify_origin("androidx.fragment.app.FragmentManager", [APP]) == "androidx"
    assert classify_origin("android.os.Looper", [APP]) == "framework"
    assert classify_origin("okhttp3.internal.connection.RealCall", [APP]) == "sdk"
    # no package given: unknown classes are assumed to be the app
    assert classify_origin("com.acme.Foo", []) == "app"


def test_jvm_basic():
    p = parse(crash("c01_fragment_state_loss"), [APP])
    assert p.kind == "jvm_exception"
    assert p.thread == "main"
    assert p.primary.type == "java.lang.IllegalStateException"
    assert "onSaveInstanceState" in p.primary.message
    f = p.first_app_frame()
    assert (f.class_name, f.method, f.line) == (f"{APP}.cart.CartFragment", "showCouponDialog", 88)


def test_caused_by_chain_uses_deepest_cause():
    p = parse(crash("c06_platform_type_npe_startup"), [APP])
    assert [l.type for l in p.chain] == ["java.lang.RuntimeException", "java.lang.NullPointerException"]
    assert p.primary.type == "java.lang.NullPointerException"
    assert p.first_app_frame().method == "getLanguage"


def test_suppressed_is_not_part_of_chain():
    p = parse(crash("c11_coroutine_uncaught_network"), [APP])
    types = [l.type for l in p.chain]
    assert "kotlinx.coroutines.internal.DiagnosticCoroutineContextException" not in types
    assert types[-1] == "android.system.GaiException"
    assert p.first_app_frame() is None  # no app frame in this trace


def test_multiline_message():
    p = parse(crash("c12_flow_invariant"), [APP])
    assert "Emission from another coroutine" in p.primary.message


def test_strictmode_report_parsed():
    p = parse(crash("c09_strictmode_disk_read"), [APP])
    assert p.kind == "jvm_exception"
    assert p.primary.type == "android.os.strictmode.DiskReadViolation"
    assert p.first_app_frame().method == "currentTheme"


def test_anr_lock_holder():
    p = parse(crash("c25_anr_lock_contention"), [APP])
    assert p.kind == "anr"
    assert p.anr_reason == "Input dispatching timed out"
    assert p.main_thread.state == "Blocked"
    assert p.main_thread.held_by_tid == 23
    assert p.lock_holder.name == "DefaultDispatcher-worker-3"
    assert any(f.method == "trimToSize" for f in p.lock_holder.frames)


def test_native_tombstone():
    p = parse(crash("c30_native_sigsegv"), [APP])
    assert p.kind == "native"
    assert p.native_signal.startswith("signal 11 (SIGSEGV)")
    assert len(p.native_backtrace) == 5


def test_obfuscation_detected_then_retraced():
    p = parse(crash("c28_obfuscated_with_mapping"), [APP])
    assert p.obfuscated
    p = retrace(p, mapping("c28_obfuscated_with_mapping"), [APP])
    f = p.first_app_frame()
    assert (f.class_name, f.method, f.line) == (f"{APP}.search.QueryValidator", "isValid", 23)
    assert not p.obfuscated and p.retraced


def test_mapping_loader():
    m = load_mapping(mapping("c28_obfuscated_with_mapping"))
    assert m["c.f.a.b"].original.endswith("QueryValidator")
    assert m["c.f.a.b"].methods["a"][0].orig_start == 21


def test_detect_kind_unknown():
    assert detect_kind("hello world") == "unknown"


def test_prompt_dict_is_compact():
    d = parse(crash("c01_fragment_state_loss"), [APP]).to_prompt_dict()
    assert d["primary_exception"] == "java.lang.IllegalStateException"
    assert d["first_app_frame"].startswith(f"{APP}.cart.CartFragment.showCouponDialog")
    assert all(len(l["frames"]) <= 13 for l in d["exception_chain"])
