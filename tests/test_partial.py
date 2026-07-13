from hark.partial import (
    HOLD_COMPACT_INSTRUCTIONS,
    HOLD_INSTRUCTIONS,
    HOLD_WARNING,
    STREAMING_COMPACT_INSTRUCTIONS,
    STREAMING_INSTRUCTIONS,
    STREAMING_WARNING,
    make_final_event,
    make_partial_event,
    partial_compact_instructions,
    partial_instructions,
    partial_warning,
)


def test_partial_has_hold_warning():
    ev = make_partial_event(stream_id="s1", seq=1, text="hello world")
    assert ev["partial"] is True
    assert ev["final"] is False
    assert ev["streaming"] is False
    assert "HOLD" in ev["warning"] or "PARTIAL" in ev["warning"]
    assert "HOLD" in ev["instructions"]
    assert HOLD_WARNING
    assert HOLD_INSTRUCTIONS
    assert "speak to the user yet" in ev["warning"]
    assert "HOLD" in ev["instructions"]


def test_partial_instructions_require_listen_end_on_done():
    """B068: orchestrator backup is MUST, not optional."""
    assert "MUST" in HOLD_INSTRUCTIONS
    assert "end_recording" in HOLD_INSTRUCTIONS
    assert "over" in HOLD_INSTRUCTIONS.lower()
    assert "false" in HOLD_INSTRUCTIONS.lower() or "mid-clause" in HOLD_INSTRUCTIONS.lower()
    ev = make_partial_event(stream_id="s1", seq=1, text="please ship it. over")
    assert "MUST" in ev["instructions"]
    assert "listen-end" in ev["agent_control"]["end_recording"]
    assert "MUST" in ev["agent_control"]["hint"]


def test_streaming_partial_allows_short_live_tts():
    """B098: ambient.streaming → different instructions than HOLD."""
    hold = make_partial_event(stream_id="s1", seq=1, text="hello", streaming=False)
    live = make_partial_event(stream_id="s1", seq=1, text="hello", streaming=True)

    assert hold["streaming"] is False
    assert live["streaming"] is True
    assert hold["instructions"] != live["instructions"]
    assert hold["warning"] != live["warning"]

    assert "HOLD" in hold["instructions"]
    assert "Do NOT TTS a full answer" in hold["instructions"]
    assert "STREAMING" in live["instructions"]
    assert "short live" in live["instructions"].lower() or "brief" in live["instructions"].lower()
    assert "pane" in live["instructions"].lower()
    assert "MUST" in live["instructions"]
    assert "end_recording" in live["instructions"]

    assert STREAMING_WARNING != HOLD_WARNING
    assert STREAMING_INSTRUCTIONS != HOLD_INSTRUCTIONS
    assert partial_instructions(streaming=False) == HOLD_INSTRUCTIONS
    assert partial_instructions(streaming=True) == STREAMING_INSTRUCTIONS
    assert partial_warning(streaming=True) == STREAMING_WARNING
    assert partial_compact_instructions(streaming=False) == HOLD_COMPACT_INSTRUCTIONS
    assert partial_compact_instructions(streaming=True) == STREAMING_COMPACT_INSTRUCTIONS
    assert "HOLD" in HOLD_COMPACT_INSTRUCTIONS
    assert "STREAMING" in STREAMING_COMPACT_INSTRUCTIONS
    assert HOLD_COMPACT_INSTRUCTIONS != STREAMING_COMPACT_INSTRUCTIONS


def test_final_supersedes():
    fin = make_final_event(stream_id="s1", text="hello world done", partials_emitted=2)
    assert fin["partial"] is False
    assert fin["final"] is True
    assert fin["stream_id"] == "s1"
    assert "FINAL" in fin["instructions"]
