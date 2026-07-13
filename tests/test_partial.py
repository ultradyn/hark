from hark.partial import HOLD_INSTRUCTIONS, HOLD_WARNING, make_final_event, make_partial_event


def test_partial_has_hold_warning():
    ev = make_partial_event(stream_id="s1", seq=1, text="hello world")
    assert ev["partial"] is True
    assert ev["final"] is False
    assert "HOLD" in ev["warning"] or "PARTIAL" in ev["warning"]
    assert "HOLD" in ev["instructions"]
    assert HOLD_WARNING
    assert HOLD_INSTRUCTIONS


def test_partial_instructions_require_listen_end_on_done():
    """B068: Mode A backup is MUST, not optional."""
    assert "MUST" in HOLD_INSTRUCTIONS
    assert "end_recording" in HOLD_INSTRUCTIONS
    assert "over" in HOLD_INSTRUCTIONS.lower()
    assert "false" in HOLD_INSTRUCTIONS.lower() or "mid-clause" in HOLD_INSTRUCTIONS.lower()
    ev = make_partial_event(stream_id="s1", seq=1, text="please ship it. over")
    assert "MUST" in ev["instructions"]
    assert "listen-end" in ev["agent_control"]["end_recording"]
    assert "MUST" in ev["agent_control"]["hint"]


def test_final_supersedes():
    fin = make_final_event(stream_id="s1", text="hello world done", partials_emitted=2)
    assert fin["partial"] is False
    assert fin["final"] is True
    assert fin["stream_id"] == "s1"
    assert "FINAL" in fin["instructions"]
