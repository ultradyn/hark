"""Stateful pane classifier (former EdgeTracker).

No Herdr client calls — pane text arrives via ``question_for`` (compat) or
``PaneObservation.pane_text`` (``process_observations``).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from hark.events import (
    DEFAULT_PANE_CAPTURE_LINES,
    DEFAULT_PANE_CAPTURE_MAX_CHARS,
    extract_question_excerpt,
    is_idle_like_status,
    make_agent_busy_subagent,
    make_agent_needs_input,
    make_agent_question_changed,
    make_agent_status_event,
    prepare_pane_capture,
)
from hark.fingerprint import question_fingerprint
from hark.herdr.client import AgentInfo
from hark.pane_understanding.heuristics import (
    detect_active_subagents,
    looks_like_pending_question,
)
from hark.pane_understanding.types import (
    ClassifyPolicy,
    PaneObservation,
    PaneUnderstandingState,
)


class PaneClassifier:
    """Stateful pane classify: status edges → HEP facts (no Herdr I/O).

    Owns false-done menus, busy-subagent suppression, and question_changed.
    See ``docs/plans/P1-M3-pane-understanding.md``.
    """

    def __init__(
        self,
        policy: ClassifyPolicy | None = None,
        *,
        pane_capture: bool | None = None,
        pane_capture_lines: int | None = None,
        pane_capture_max_chars: int | None = None,
        state: PaneUnderstandingState | None = None,
    ) -> None:
        """Create a classifier.

        Prefer ``ClassifyPolicy``. Keyword pane_capture* args remain for
        EdgeTracker-compatible construction used by watch/tests during migration.
        """
        base = policy or ClassifyPolicy(
            pane_capture_lines=DEFAULT_PANE_CAPTURE_LINES,
            pane_capture_max_chars=DEFAULT_PANE_CAPTURE_MAX_CHARS,
        )
        if pane_capture is not None or pane_capture_lines is not None or pane_capture_max_chars is not None:
            base = ClassifyPolicy(
                interest=base.interest,
                detect_false_done=base.detect_false_done,
                pane_capture=base.pane_capture if pane_capture is None else bool(pane_capture),
                pane_capture_lines=max(
                    1,
                    int(
                        base.pane_capture_lines
                        if pane_capture_lines is None
                        else pane_capture_lines
                    ),
                ),
                pane_capture_max_chars=max(
                    64,
                    int(
                        base.pane_capture_max_chars
                        if pane_capture_max_chars is None
                        else pane_capture_max_chars
                    ),
                ),
            )
        else:
            base = ClassifyPolicy(
                interest=base.interest,
                detect_false_done=base.detect_false_done,
                pane_capture=base.pane_capture,
                pane_capture_lines=max(1, int(base.pane_capture_lines)),
                pane_capture_max_chars=max(64, int(base.pane_capture_max_chars)),
            )
        self.policy = base
        self._state = state if state is not None else PaneUnderstandingState.empty()
        # Compat attributes used by tests/watch that read capture flags on tracker.
        self.pane_capture = self.policy.pane_capture
        self.pane_capture_lines = self.policy.pane_capture_lines
        self.pane_capture_max_chars = self.policy.pane_capture_max_chars


    @property
    def state(self) -> PaneUnderstandingState:
        return self._state

    def process(
        self,
        agents: list[AgentInfo],
        *,
        interest: set[str],
        question_for: Callable[[AgentInfo], str | None] | None = None,
        detect_false_done: bool = True,
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for agent in agents:
            key = (agent.session_id, agent.pane_id)
            prev = self._state.status.get(key)
            cur = agent.status

            # Same status: optional question_changed while still blocked, or
            # re-scan idle-like for a newly appeared menu (rare but cheap).
            if prev == cur:
                events.extend(
                    self._same_status_events(
                        agent,
                        key=key,
                        cur=cur,
                        interest=interest,
                        question_for=question_for,
                        detect_false_done=detect_false_done,
                    )
                )
                continue

            self._state.status[key] = cur
            first_seen = prev is None

            # First observation: only surface blocked (or false-done on idle-like).
            if first_seen:
                if cur == "blocked" and "blocked" in interest:
                    events.extend(
                        self._emit_blocked(
                            agent, key=key, prev=prev, cur=cur, question_for=question_for
                        )
                    )
                elif (
                    detect_false_done
                    and is_idle_like_status(cur)
                    and question_for
                    and self._watch_cares_about_input(interest)
                ):
                    events.extend(
                        self._maybe_false_done(
                            agent,
                            key=key,
                            prev=prev,
                            cur=cur,
                            question_for=question_for,
                            also_completed=cur == "done" and "done" in interest,
                        )
                    )
                continue

            # Leaving interest entirely (e.g. working→working never hits here).
            if cur not in interest and prev not in interest:
                # Still catch false done when status becomes idle-like even if
                # "done" was not in interest but blocked was (handsfree often has both).
                if (
                    detect_false_done
                    and is_idle_like_status(cur)
                    and question_for
                    and self._watch_cares_about_input(interest)
                ):
                    events.extend(
                        self._maybe_false_done(
                            agent,
                            key=key,
                            prev=prev,
                            cur=cur,
                            question_for=question_for,
                            also_completed=False,
                        )
                    )
                continue

            if cur == "blocked":
                events.extend(
                    self._emit_blocked(
                        agent, key=key, prev=prev, cur=cur, question_for=question_for
                    )
                )
                continue

            if (
                detect_false_done
                and is_idle_like_status(cur)
                and question_for
                and self._watch_cares_about_input(interest)
            ):
                # Own the idle-like transition entirely (menus, subagents, or
                # real completed). Even an empty result must not fall through:
                # busy-subagent dedupe returns [] while still not finished.
                events.extend(
                    self._maybe_false_done(
                        agent,
                        key=key,
                        prev=prev,
                        cur=cur,
                        question_for=question_for,
                        also_completed=(
                            cur in interest or prev in interest
                        )
                        and (
                            cur == "done"
                            or "done" in interest
                            or prev in interest
                        ),
                    )
                )
                continue

            if cur in interest or prev in interest:
                events.append(
                    make_agent_status_event(
                        agent,
                        from_status=prev,
                        to_status=cur,
                        question_text=None,
                    )
                )
        return events

    @staticmethod
    def _watch_cares_about_input(interest: set[str]) -> bool:
        return bool(interest & {"blocked", "done", "idle"})

    def _split_pane_text(
        self, raw: str | None
    ) -> tuple[str | None, dict[str, Any] | None]:
        """Return (stable question excerpt, optional full pane_capture).

        Fingerprint/answer binding uses the trailing excerpt so scrollback
        noise does not re-fire the same menu. HEP ``pane_capture`` carries the
        full bounded body for Mode A decisions without a second fetch.
        """
        if not raw or not str(raw).strip():
            return None, None
        # Heuristics + FP: trailing ask block (matches answering.py).
        trail = extract_question_excerpt(raw, max_chars=4000)
        excerpt = extract_question_excerpt(raw) or None
        # Prefer longer trail for false-done when excerpt is very short.
        if not excerpt and trail:
            excerpt = trail[:500] if len(trail) > 500 else trail
        capture = None
        if self.pane_capture:
            capture = prepare_pane_capture(
                raw,
                max_lines=self.pane_capture_lines,
                max_chars=self.pane_capture_max_chars,
            )
        # question.text uses excerpt when available; fall back to trail.
        q_text = excerpt or (trail or None)
        return q_text, capture

    def _heuristic_text(self, raw: str | None) -> str | None:
        """Text used for false-done menu heuristics (trailing viewport)."""
        if not raw:
            return None
        trail = extract_question_excerpt(raw, max_chars=4000)
        return trail or raw

    def _emit_blocked(
        self,
        agent: AgentInfo,
        *,
        key: tuple[str, str],
        prev: str | None,
        cur: str,
        question_for: Callable[[AgentInfo], str | None] | None,
    ) -> list[dict[str, Any]]:
        raw = question_for(agent) if question_for else None
        q_text, capture = self._split_pane_text(raw)
        fp_src = extract_question_excerpt(raw or "") if raw else (q_text or "")
        fp = question_fingerprint(fp_src or "", None) if (fp_src or q_text) else ""
        dkey = (agent.session_id, agent.pane_id, cur, fp)
        if fp and dkey in self._state.dedupe:
            return []
        if fp:
            self._state.dedupe.add(dkey)
            self._state.last_fp[key] = fp
        return [
            make_agent_status_event(
                agent,
                from_status=prev,
                to_status=cur,
                question_text=q_text,
                pane_capture=capture,
            )
        ]

    def _emit_busy_subagent(
        self,
        agent: AgentInfo,
        *,
        key: tuple[str, str],
        prev: str | None,
        cur: str,
        hit,
        capture: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        """Suppress completed; emit a single reclassified working event (deduped)."""
        count = max(1, int(hit.count or 1))
        self._state.subagents_busy[key] = count
        # Keep re-checking until the strip clears (do not seal false-done scan).
        self._state.false_done_scanned.pop(key, None)
        dkey = (agent.session_id, agent.pane_id, "busy_subagent", str(count))
        if dkey in self._state.dedupe:
            return []
        self._state.dedupe.add(dkey)
        return [
            make_agent_busy_subagent(
                agent,
                from_status=prev,
                herdr_status=cur,
                hit=hit,
                pane_capture=capture if self.pane_capture else None,
            )
        ]

    def _maybe_false_done(
        self,
        agent: AgentInfo,
        *,
        key: tuple[str, str],
        prev: str | None,
        cur: str,
        question_for: Callable[[AgentInfo], str | None],
        also_completed: bool,
    ) -> list[dict[str, Any]]:
        raw = question_for(agent)
        q_text, capture = self._split_pane_text(raw)
        heuristic = self._heuristic_text(raw)

        def _completed_only(
            q: str | None = None, cap: dict[str, Any] | None = None
        ) -> list[dict[str, Any]]:
            if not also_completed:
                return []
            return [
                make_agent_status_event(
                    agent,
                    from_status=prev,
                    to_status=cur,
                    question_text=q,
                    pane_capture=cap if self.pane_capture else None,
                )
            ]

        # Full-pane scan: task strip is often near the top (not trailing excerpt).
        sub = detect_active_subagents(raw)
        if sub:
            return self._emit_busy_subagent(
                agent, key=key, prev=prev, cur=cur, hit=sub, capture=capture
            )

        # Subagents just cleared while still idle-like → allow real completion.
        self._state.subagents_busy.pop(key, None)
        self._state.false_done_scanned[key] = cur

        if not heuristic and not q_text:
            return _completed_only()

        hit = looks_like_pending_question(heuristic)
        if not hit:
            return _completed_only(q_text, capture)

        fp_src = extract_question_excerpt(raw or "") if raw else (q_text or "")
        fp = question_fingerprint(fp_src or q_text or "", list(hit.choices) or None)
        dkey = (agent.session_id, agent.pane_id, "needs_input", fp)
        if fp and dkey in self._state.dedupe:
            return _completed_only(q_text, capture)
        if fp:
            self._state.dedupe.add(dkey)
            self._state.last_fp[key] = fp

        out: list[dict[str, Any]] = [
            make_agent_needs_input(
                agent,
                from_status=prev,
                to_status=cur,
                question_text=q_text or heuristic or "",
                hit=hit,
                pane_capture=capture,
            )
        ]
        if also_completed and cur == "done":
            completed = make_agent_status_event(
                agent,
                from_status=prev,
                to_status=cur,
                question_text=q_text,
                pane_capture=capture,
            )
            # Lower priority so needs_input wins attention in sorted UIs.
            completed["priority"] = min(int(completed.get("priority") or 50), 40)
            completed["false_done"] = True
            out.append(completed)
        elif also_completed and cur != "done":
            out.extend(_completed_only(q_text, capture))
        return out

    def _same_status_events(
        self,
        agent: AgentInfo,
        *,
        key: tuple[str, str],
        cur: str,
        interest: set[str],
        question_for: Callable[[AgentInfo], str | None] | None,
        detect_false_done: bool,
    ) -> list[dict[str, Any]]:
        """While status is unchanged: question_changed, busy-subagent, or late false-done.

        Idle-like menu re-scan only once per status epoch (avoids pane-read spam).
        Active Tasks/subagents keep re-checking until the strip clears, then
        completion / needs_input may fire.
        """
        if not question_for:
            return []

        # Re-block heuristic: still blocked, question text changed.
        if cur == "blocked" and "blocked" in interest:
            raw = question_for(agent)
            q_text, capture = self._split_pane_text(raw)
            if not q_text and not raw:
                return []
            fp_src = extract_question_excerpt(raw or "") if raw else (q_text or "")
            fp = question_fingerprint(fp_src or "", None)
            prev_fp = self._state.last_fp.get(key)
            if not fp or fp == prev_fp:
                if fp:
                    self._state.last_fp[key] = fp
                return []
            self._state.last_fp[key] = fp
            dkey = (agent.session_id, agent.pane_id, "question_changed", fp)
            if dkey in self._state.dedupe:
                return []
            self._state.dedupe.add(dkey)
            return [
                make_agent_question_changed(
                    agent,
                    to_status=cur,
                    question_text=q_text or fp_src,
                    pane_capture=capture,
                )
            ]

        if not (
            detect_false_done
            and is_idle_like_status(cur)
            and self._watch_cares_about_input(interest)
        ):
            return []

        was_busy = key in self._state.subagents_busy
        needs_late_menu = self._state.false_done_scanned.get(key) != cur
        if not was_busy and not needs_late_menu:
            return []

        raw = question_for(agent)
        q_text, capture = self._split_pane_text(raw)
        sub = detect_active_subagents(raw)
        if sub:
            return self._emit_busy_subagent(
                agent, key=key, prev=cur, cur=cur, hit=sub, capture=capture
            )

        if was_busy:
            # Tasks/subagents settled while Herdr still idle-like → complete now.
            self._state.subagents_busy.pop(key, None)
            return self._maybe_false_done(
                agent,
                key=key,
                prev=cur,
                cur=cur,
                question_for=question_for,
                also_completed=cur == "done" and "done" in interest,
            )

        # One late re-check after status settled on done/idle (menu may paint
        # after the status edge). Skip if transition path already inspected.
        self._state.false_done_scanned[key] = cur
        heuristic = self._heuristic_text(raw)
        if not heuristic:
            return []
        hit = looks_like_pending_question(heuristic)
        if not hit:
            return []
        fp_src = extract_question_excerpt(raw or "") if raw else (q_text or "")
        fp = question_fingerprint(
            fp_src or q_text or "", list(hit.choices) or None
        )
        dkey = (agent.session_id, agent.pane_id, "needs_input", fp)
        if not fp or dkey in self._state.dedupe:
            return []
        self._state.dedupe.add(dkey)
        self._state.last_fp[key] = fp
        return [
            make_agent_needs_input(
                agent,
                from_status=cur,
                to_status=cur,
                question_text=q_text or heuristic,
                hit=hit,
                pane_capture=capture,
            )
        ]

    def process_observations(
        self,
        observations: Sequence[PaneObservation],
        *,
        interest: set[str] | None = None,
        detect_false_done: bool | None = None,
    ) -> list[dict[str, Any]]:
        """Classify pre-read observations (unit-testable pure seam).

        Uses each observation's ``pane_text`` and ``raw_agent`` (AgentInfo) for
        HEP packing. When ``raw_agent`` is missing, a minimal AgentInfo is built
        from observation fields.
        """
        interest_set = set(interest) if interest is not None else set(self.policy.interest)
        dfd = (
            self.policy.detect_false_done
            if detect_false_done is None
            else bool(detect_false_done)
        )

        def question_for(agent: AgentInfo) -> str | None:
            for obs in observations:
                if obs.session_id == agent.session_id and obs.pane_id == agent.pane_id:
                    return obs.pane_text
            return None

        agents: list[AgentInfo] = []
        for obs in observations:
            if obs.raw_agent is not None:
                agents.append(obs.raw_agent)
            else:
                agents.append(
                    AgentInfo(
                        session_id=obs.session_id,
                        pane_id=obs.pane_id,
                        agent=obs.agent,
                        status=obs.status,
                        revision=obs.revision,
                    )
                )
        return self.process(
            agents,
            interest=interest_set,
            question_for=question_for,
            detect_false_done=dfd,
        )


# Back-compat name used by watch + existing tests.
EdgeTracker = PaneClassifier
