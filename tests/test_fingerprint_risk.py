from hark.fingerprint import normalize_question_text, question_fingerprint
from hark.risk import classify_question, confirm_required
from hark.targets import parse_target


def test_fingerprint_stable():
    a = question_fingerprint("Allow rm -rf?", ["Yes", "No"])
    b = question_fingerprint("  allow   RM -rf? ", ["yes", "no"])
    assert a == b
    assert a.startswith("blake2b:")


def test_normalize():
    assert normalize_question_text("  Hello\tWorld  ") == "hello world"


def test_risk_r3():
    r = classify_question("Allow running rm -rf build/?")
    assert r.risk == "R3"


def test_risk_r2():
    r = classify_question("Allow network access for this command?")
    assert r.risk == "R2"


def test_confirm_policy():
    assert confirm_required("R2", "auto") is True
    assert confirm_required("R3", "never") is True
    assert confirm_required("R2", "never", explicit_override=True) is False
    assert confirm_required("R3", "never", explicit_override=True) is False
    assert confirm_required("R1", "auto") is False
    assert confirm_required("R1", "always") is True
    assert confirm_required("R0", "always") is True


def test_parse_target():
    t = parse_target("work/w1:p6")
    assert t.session_id == "work"
    assert t.pane_id == "w1:p6"
    t2 = parse_target("w1:p1", default_session="local")
    assert str(t2) == "local/w1:p1"
