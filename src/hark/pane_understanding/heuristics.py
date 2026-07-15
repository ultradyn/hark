"""Pure pane text heuristics for false-done menus and busy subagents.

Lives with Pane Understanding (P1.M3). No Herdr I/O.
``hark.events`` re-exports public names for back-compat.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Shared with events.extract_question_excerpt (kept local to avoid import cycles).
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# Numbered / lettered choice lines (menus). Optional leading ❯ for Claude-style
# selection (``❯ 1. Yes``); bare empty ``❯`` does not match.
_MENU_LINE = re.compile(r"^\s*(?:❯[ \t]+)?(?:\d+|[A-Da-d])[\).\]]\s+\S", re.M)
# Explicit choice / reply prompts.
_CHOICE_PHRASE = re.compile(
    r"(?i)\b("
    r"which\s+option|which\s+one|choose\s+one|select\s+(an?\s+)?option|"
    r"pick\s+one|reply\s+with|respond\s+with|enter\s+(a\s+)?number|"
    r"type\s+(a\s+)?number|press\s+\d|select\s+\d|"
    r"option\s*[1-9]|choices?\s*:|menu\s*:"
    r")\b"
)
# Yes/no style still awaiting input.
_YN_PROMPT = re.compile(
    r"(?i)(\[?\s*y\s*/\s*n\s*\]?|\byes\s*/\s*no\b|\b\(y/n\)\b|"
    r"\ballow\b.+\?|\bdo\s+you\s+want\b.+\?|\bproceed\b.+\?)"
)
# Trailing question that looks like it needs an answer (not rhetorical dump).
_TRAILING_QUESTION = re.compile(r"\?\s*$")

# Idle input chrome (Claude Code / Grok Build empty prompt). Box borders optional.
_EMPTY_PROMPT_LINE = re.compile(
    r"^[ \t]*[│|┃]?[ \t]*❯[ \t]*[│|┃]?[ \t]*$"
)
# Prompt with typed/selected content (active menu or partial input).
_PROMPT_WITH_INPUT = re.compile(
    r"^[ \t]*[│|┃]?[ \t]*❯[ \t]+\S"
)
# Pure box-drawing / horizontal rules — not menu content (B111).
_BOX_DRAWING_LINE = re.compile(
    r"^[ \t]*[─━═╌╍┄┅┈┉╭╮╯╰┌┐└┘├┤┬┴┼╱╲╳│┃+\-|]{2,}[ \t]*$"
)
# Status footer chrome under the prompt box.
_STATUS_CHROME_LINE = re.compile(
    r"(?i)^[ \t]*(?:"
    r"⏵|bypass\s+permissions|shift\+tab|"
    r"(?:opus|sonnet|haiku|grok|claude|codex)\b|"
    r"\d+(?:\.\d+)?%\s*(?:context|used)?|"
    r"[~.]?/[^\n]{0,120}\b(?:main|master|trunk|dev|develop|HEAD)\b"
    r")"
)


def _strip_ansi(text: str | None) -> str:
    return _ANSI_RE.sub("", text or "")


@dataclass(frozen=True)
class PendingQuestionHit:
    """Result of trailing-pane pending-question heuristics (false-done path)."""

    matched: bool
    reasons: tuple[str, ...]
    choices: tuple[str, ...] = ()
    confidence: float = 0.0

    def __bool__(self) -> bool:
        return self.matched


def _is_prompt_chrome_line(line: str) -> bool:
    """True for empty prompt, box-drawing, or status footer — not ask body."""
    s = line.rstrip()
    if not s.strip():
        return True
    if _EMPTY_PROMPT_LINE.match(s):
        return True
    if _BOX_DRAWING_LINE.match(s):
        return True
    if _STATUS_CHROME_LINE.search(s):
        return True
    return False


def _menu_scan_region(raw: str) -> str:
    """Text region that may hold an *active* numbered menu (B111).

    Numbered lists in assistant scrollback above an idle empty ``❯`` prompt
    must not count as ``numbered_menu``. Real menus either:

    * sit on a content prompt (``❯ 1. Yes`` / following ``2. No``), or
    * form a contiguous block immediately above an empty prompt (typed reply), or
    * appear with no empty-prompt chrome (classic multi-line ask, B016).

    Box-drawing / trailing ``❯`` alone never produce menu lines.
    """
    lines = raw.splitlines()
    if not lines:
        return raw

    # Prefer the lowest content prompt (Claude selection UI) in the viewport.
    last_content_i: int | None = None
    last_empty_i: int | None = None
    # Search near the bottom — scrollback prompts are noise.
    start = max(0, len(lines) - 50)
    for i in range(len(lines) - 1, start - 1, -1):
        ln = lines[i]
        if _PROMPT_WITH_INPUT.match(ln):
            last_content_i = i
            break
        if _EMPTY_PROMPT_LINE.match(ln):
            # Empty idle prompt at the bottom wins; older content prompts above
            # are scrollback and must not re-open the menu region.
            last_empty_i = i
            break

    if last_content_i is not None:
        # Active selection UI from the selected line through following options.
        return "\n".join(lines[last_content_i:])

    if last_empty_i is not None:
        # Idle empty prompt: only a contiguous menu block immediately above it
        # (blank / chrome lines allowed between items; any other prose stops).
        block: list[str] = []
        j = last_empty_i - 1
        while j >= 0 and _is_prompt_chrome_line(lines[j]):
            j -= 1
        while j >= 0:
            ln = lines[j]
            if not ln.strip() or _BOX_DRAWING_LINE.match(ln):
                j -= 1
                continue
            if _MENU_LINE.match(ln):
                block.append(ln)
                j -= 1
                continue
            break
        return "\n".join(reversed(block))

    return raw


def looks_like_pending_question(text: str | None) -> PendingQuestionHit:
    """Heuristic: does trailing pane text still look like it needs human input?

    Used when Herdr reports done/idle but the bottom of the pane still shows a
    multi-option menu or explicit ask (false done / false idle).

    Idle Claude Code empty ``❯`` prompts (with box-drawing / status chrome) are
    *not* pending. Numbered lists in completed assistant prose above that
    chrome do not trigger ``numbered_menu`` (B111). Real menus still match
    (inverse of B016).
    """
    # Strip ANSI so prompt-chrome detection sees bare ``❯`` (Herdr may still
    # hand colorized captures depending on source).
    raw = _strip_ansi(text or "").strip()
    if not raw:
        return PendingQuestionHit(matched=False, reasons=())

    reasons: list[str] = []
    choices: list[str] = []
    confidence = 0.0

    # Menu lines only from the active ask region — not scrollback above idle ❯.
    menu_region = _menu_scan_region(raw)
    menu_lines = [
        ln.strip()
        for ln in menu_region.splitlines()
        if ln.strip() and _MENU_LINE.match(ln)
    ]
    if len(menu_lines) >= 2:
        reasons.append("numbered_menu")
        choices = menu_lines[:12]
        confidence = max(confidence, 0.9)
    elif len(menu_lines) == 1:
        reasons.append("single_menu_line")
        choices = menu_lines
        confidence = max(confidence, 0.55)

    if _CHOICE_PHRASE.search(raw):
        reasons.append("choice_phrase")
        confidence = max(confidence, 0.85)

    if _YN_PROMPT.search(raw):
        reasons.append("yes_no_prompt")
        confidence = max(confidence, 0.8)

    # Question mark in trailing chunk + menu/choice already strong; alone weaker
    # unless there is also a short last line ending in ?
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    trailing = "\n".join(lines[-8:]) if lines else raw
    if _TRAILING_QUESTION.search(trailing) and (
        "numbered_menu" in reasons
        or "choice_phrase" in reasons
        or "yes_no_prompt" in reasons
        or re.search(r"(?i)\b(which|what|should\s+i|do\s+you|allow|confirm)\b", trailing)
    ):
        if "trailing_question" not in reasons:
            reasons.append("trailing_question")
        confidence = max(confidence, 0.75 if "numbered_menu" in reasons else 0.65)

    # Require a solid signal: menu (≥2 lines), or choice/yn phrase, or
    # single menu line + question-ish trail.
    # Idle empty prompt alone (or box-drawing alone) never matches.
    matched = False
    if "numbered_menu" in reasons:
        matched = True
    elif "choice_phrase" in reasons and (
        "trailing_question" in reasons
        or "single_menu_line" in reasons
        or "yes_no_prompt" in reasons
        or len(raw) < 800
    ):
        matched = True
        confidence = max(confidence, 0.8)
    elif "yes_no_prompt" in reasons:
        matched = True
    elif "single_menu_line" in reasons and "trailing_question" in reasons:
        matched = True
        confidence = max(confidence, 0.7)

    if not matched:
        return PendingQuestionHit(matched=False, reasons=tuple(reasons), confidence=confidence)

    return PendingQuestionHit(
        matched=True,
        reasons=tuple(reasons),
        choices=tuple(choices),
        confidence=min(1.0, confidence or 0.7),
    )


# ---------------------------------------------------------------------------
# Active subagent / background Task strip (false done while Herdr says idle)
# ---------------------------------------------------------------------------
#
# Grok Build keeps a task strip near the top of the pane while subagents run:
#   ▾ Tasks 1
#   ⁙ Task Install rclone on Windows…   (21+) 33m14s [↗][✗]
#   ▾ Watchers 1          ← watchers alone do NOT mean still working
# Live status while the main turn is busy also shows:
#   "⠧ Waiting on subagent… 2.8s … [stop]"
# Herdr may still report done/idle once the main spinner clears even though
# Tasks N (N≥1) remains — Mode A must not treat that as agent.completed.

@dataclass(frozen=True)
class ActiveSubagentsHit:
    """Result of pane heuristics for running subagent/background tasks."""

    matched: bool
    count: int = 0
    reasons: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return self.matched


# Header: "▾ Tasks 1" / "▸ Tasks 2" / plain "Tasks 3" on its own line.
_TASKS_HEADER = re.compile(
    r"(?m)^[ \t]*(?:[▾▸▼▶▽▷▿▹]|[+\*])?[ \t]*Tasks[ \t]+(\d+)[ \t]*$"
)
# Row under the strip: special bullet + "Task <label>…" (not Watchers/Monitor).
_TASK_ROW = re.compile(
    r"(?m)^[ \t]*(?:[⸬⁙‧∙•●○◉◆▪\*\-]|[\u2022\u25cf\u25e6\u2b24\u2e2c\u205b])"
    r"[ \t]+Task[ \t]+(\S[^\n]{0,180})$"
)
# Explicit subagent wait / running phrases (Grok and others).
_SUBAGENT_ACTIVE_PHRASE = re.compile(
    r"(?i)\b("
    r"waiting\s+on\s+sub-?agents?|"
    r"sub-?agents?\s+(?:still\s+)?(?:running|active|in\s*progress)|"
    r"(?:running|active)\s+sub-?agents?|"
    r"\d+\s+sub-?agents?\s+(?:running|active|in\s*progress)|"
    r"background\s+tasks?\s+(?:running|active|in\s*progress)"
    r")\b"
)


def detect_active_subagents(text: str | None) -> ActiveSubagentsHit:
    """Heuristic: does pane text show active subagent/tasks still in flight?

    Inspects the full pane body (task strip is often near the **top** of the
    Herdr/Grok UI, not the trailing ask block used for menus). Watchers-only
    chrome does not count as busy.
    """
    raw = _strip_ansi(text or "")
    if not raw.strip():
        return ActiveSubagentsHit(matched=False, count=0, reasons=())

    reasons: list[str] = []
    count = 0
    labels: list[str] = []

    header_n = 0
    for m in _TASKS_HEADER.finditer(raw):
        n = int(m.group(1))
        if n > header_n:
            header_n = n
    if header_n >= 1:
        reasons.append("tasks_header")
        count = max(count, header_n)

    for m in _TASK_ROW.finditer(raw):
        label = m.group(1).strip()
        # Drop trailing chrome like duration / action chips when present.
        label = re.split(r"[ \t]{2,}|\s+\(\d", label, maxsplit=1)[0].strip()
        if not label or len(label) > 160:
            continue
        labels.append(label[:120])
    if labels:
        reasons.append("task_rows")
        count = max(count, len(labels))

    if _SUBAGENT_ACTIVE_PHRASE.search(raw):
        reasons.append("subagent_phrase")
        count = max(count, 1)

    if not reasons or count < 1:
        return ActiveSubagentsHit(
            matched=False,
            count=0,
            reasons=tuple(reasons),
            labels=tuple(labels[:12]),
        )

    return ActiveSubagentsHit(
        matched=True,
        count=count,
        reasons=tuple(reasons),
        labels=tuple(labels[:12]),
    )
