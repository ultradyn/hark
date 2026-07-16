"""Agent-facing contracts in the canonical Hark skill."""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HARK_SKILL = ROOT / "skill" / "hark" / "SKILL.md"


def _skill_text() -> str:
    return HARK_SKILL.read_text(encoding="utf-8")


def _b141_contract() -> str:
    text = _skill_text()
    start_marker = "<!-- b141-chat-question-contract:start -->"
    end_marker = "<!-- b141-chat-question-contract:end -->"
    start = text.index(start_marker)
    end = text.index(end_marker, start)
    return text[start:end]


def _normalized(text: str) -> str:
    return " ".join(text.split())


def test_blocking_question_requires_visible_verbatim_preamble_first():
    contract = _b141_contract()
    normalized = _normalized(contract)

    emit_step = "1. **Emit Q verbatim"
    invoke_step = "2. **Only after that update is visible"
    assert contract.index(emit_step) < contract.index(invoke_step)
    assert "two separate, ordered harness actions" in normalized
    assert "exactly Q as the prompt" in normalized
    assert "summary, shortened version, or alternate wording" in normalized


def test_direct_example_uses_one_identical_question():
    contract = _b141_contract()
    visible = re.search(r"> Question: (?P<question>.+\?)", contract)
    invoked = re.search(r'hark ask --confirm never "(?P<question>.+\?)"', contract)

    assert visible is not None
    assert invoked is not None
    assert visible.group("question") == invoked.group("question")
    assert contract.count(visible.group("question")) == 2


def test_confirmation_and_retry_paths_preserve_question_identity():
    contract = _b141_contract()
    normalized = _normalized(contract)

    assert "**Confirmation path:**" in contract
    assert "visible preamble is still Q, emitted once before the call" in normalized
    assert "a new Q and needs its own visible preamble" in normalized
    assert "**Retry / repeat path:**" in contract
    assert "For a literal repeat, reuse the identical Q" in normalized
    assert "make the revision the new Q in both places" in normalized


def test_contract_does_not_promise_cli_flush_can_render_codex_chat():
    contract = _b141_contract()
    normalized = _normalized(contract)

    assert "no CLI flush can make it appear in Codex chat" in normalized
    assert "Do not substitute `echo`, `printf`" in contract
    assert "Do not add the preamble to Hark stdout" in normalized


def test_contract_precedes_examples_and_repeat_path_reemits_question():
    text = _skill_text()
    start_marker = "<!-- b141-chat-question-contract:start -->"
    end_marker = "<!-- b141-chat-question-contract:end -->"
    start = text.index(start_marker)
    end = text.index(end_marker, start)

    # The hard rule must appear before concrete workflow examples outside it.
    assert end < text.index("hark ask --confirm never", end)
    repeat = text[text.index("- **repeat** →") : text.index("- **skip** →")]
    assert "re-emit the identical question Q" in repeat
    assert "same Q in a new blocking invocation" in repeat
