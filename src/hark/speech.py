"""tts / listen / ask orchestration."""

from __future__ import annotations

import os
import re
import signal
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import ContextVar
from pathlib import Path
from typing import Any, TextIO

from hark.audio.capture import (
    MicLease,  # noqa: F401 — open_answer_window / test monkeypatch seam
    capture_utterance,  # noqa: F401 — open_answer_window / test monkeypatch seam
    pad_pcm16_silence,  # noqa: F401 — open_answer_window late-bind
    radio_stt_window_pcm,  # noqa: F401 — open_answer_window late-bind
    write_wav_bytes,  # noqa: F401 — open_answer_window late-bind
)
from hark.audio.cues import (
    configure_cues_from_config,  # noqa: F401 — open_answer_window / tests
    lookup_cached_tts,
    play_record_start,  # noqa: F401 — open_answer_window / tests
    play_record_stop,  # noqa: F401 — open_answer_window / tests
    store_cached_tts,
)
from hark.audio.media import duck_media  # also open_answer_window late-bind
from hark.audio.mic_mute import mic_muted_during_tts, repair_tts_mute_after_play
from hark.audio.playback import (
    TtsPlayLockTimeout,
    TtsPlayTimeout,
    abandon_tts_play_ticket,
    claim_tts_play_ticket,
    defer_tts_play_ticket_abandon,
    exclusive_playback,
    play_wav_bytes,
    playback_skip_generation,
    playback_skip_invocations,
    write_wav,
)
from hark.config import HarkConfig
from hark.exitcodes import ABORT, OK, PROVIDER, TIMEOUT  # noqa: F401 — answer_window / tests
from hark.lifecycle import BusySection  # noqa: F401 — open_answer_window / tests
from hark.listen_control import (
    clear_active_listen,  # noqa: F401 — open_answer_window / tests
    consume_listen_action,  # noqa: F401 — open_answer_window / tests
    poll_listen_action,  # noqa: F401 — open_answer_window / tests
    register_active_listen,  # noqa: F401 — open_answer_window / tests
    touch_voice_activity,  # noqa: F401 — open_answer_window / tests
)
from hark.endpointing import build_endpoint_strategy  # noqa: F401 — open_answer_window
from hark.mic_coord import (
    pause_ambient_for_mic,  # noqa: F401 — open_answer_window / tests
    user_capture_active,
    wait_until_tts_play_allowed,
)
from hark.providers.base import ProviderError, safe_exception_detail
from hark.providers.auth_fallback import is_auth_failure
from hark.providers.resolve import (
    resolve_stt,  # noqa: F401 — open_answer_window / test monkeypatch seam
    resolve_tts,
)
from hark.providers.tts_fallback import (
    TEXT_INSTEAD_OF_AUDIO,
    FallbackStep,
    classify_tts_error,
    fallback_provider_chain,
    synthesize_with_fallback,
    tts_error_http_status,
)
from hark.signal_safety import SigintMaskGuard
from hark.syslog import log as syslog
from hark.tts_interrupt_policy import (
    TtsSynthesisInterrupted,
    cli_process_exit_expected,
    register_cli_terminal_wait,
)
from hark.tts_notify import tts_skip_notification
from hark.tts_isolation import (
    InProcessSynthTransport,
    SubprocessSynthTransport,
    SynthProcessLifecycle,
    SynthRequest,
    SynthTransport,
    synth_worker_command,
)
from hark.usage import UsageStore
from hark.answer_window.policy import AnswerWindowProfile
from hark.answer_window.result import ListenResult  # canonical; facade re-export
from hark.answer_window.silence import (
    EMPTY_STT_NUDGE_TEXT,  # noqa: F401 — re-export for tests
    NO_OPEN_NUDGE_TEXT,  # noqa: F401 — re-export for tests
    _echo_overlap,  # noqa: F401 — re-export for tests
    is_no_open_timeout as _is_no_open_timeout_impl,
    log_empty_stt as _log_empty_stt_impl,
    log_no_open as _log_no_open_impl,
)
from hark.answer_window.text_join import (  # noqa: F401 — re-export for back-compat
    join_radio_stt_segments,
    monotonic_partial_text,
    prefer_complete_transcript,
)


_SYNTH_INTERRUPT_CLEANUP_GRACE_S = 0.75
_CURRENT_SYNTH_OWNER: ContextVar[_InterruptibleSynthPool | None] = ContextVar(
    "hark_current_synth_owner",
    default=None,
)
_synth_worker_command_factory = synth_worker_command


def _subprocess_synth_transport_factory(owner: Any) -> SynthTransport:
    return SubprocessSynthTransport(
        owner,
        command_factory=_synth_worker_command_factory,
    )


def _in_process_synth_transport_factory(owner: Any) -> SynthTransport:
    del owner
    return InProcessSynthTransport(resolve_tts)


_synth_transport_factory = _subprocess_synth_transport_factory


class _InterruptibleSynthPool:
    """One-worker synth pool with a bounded repeated-SIGINT escape hatch.

    ``ThreadPoolExecutor.shutdown(wait=False)`` does not make a stuck worker
    disposable: ``concurrent.futures.thread`` registers every worker for an
    interpreter-exit join.  If a provider ignores cancellation, ordinary
    interpreter shutdown therefore hangs after the first Ctrl-C.

    Keep the first SIGINT fully cooperative by delegating to the handler that
    was active on entry.  If that interrupt unwinds past a still-running synth,
    detach the executor without waiting and retain this handler long enough for
    a repeated SIGINT to terminate the process without entering the executor's
    traceback-producing shutdown path.  No escalation is armed when all
    tracked work has completed, or when called outside the main thread where
    Python does not permit signal-handler ownership.
    """

    def __init__(self) -> None:
        self._pool = ThreadPoolExecutor(max_workers=1)
        self._futures: list[Future[Any]] = []
        self._previous_sigint: Any = None
        self._handler: Any = None
        self._sigint_count = 0
        self._escalation_authorized = False
        self._terminal_exit_armed = False
        self._repeated_sigint_pending = False
        self._signal_installed = False
        self._submission_in_progress = False
        self._submission_uncertain = False
        self._cleanup_timer: threading.Timer | None = None
        self._process_exit_expected = cli_process_exit_expected()
        self._typed_interrupt_pending = False
        self._process_lifecycle = SynthProcessLifecycle()

    def __enter__(self) -> _InterruptibleSynthPool:
        guard: SigintMaskGuard | None = None
        expected_signal_error = False
        try:
            self._previous_sigint = signal.getsignal(signal.SIGINT)
            self._handler = self._handle_sigint
            guard = SigintMaskGuard.acquire()
            try:
                signal.signal(signal.SIGINT, self._handler)
            finally:
                # Publish the actual handler state before unblocking SIGINT. A
                # pending signal must never enter _handle_sigint while the
                # corresponding ownership flag still says false.
                self._reconcile_handler_truth()
        except (OSError, ValueError):
            expected_signal_error = True
            # Signal ownership is main-thread-only. Background TTS keeps the
            # executor's existing cleanup behavior rather than broadening this
            # process-level policy to an unsafe caller.
        except BaseException as exc:
            self._rollback_failed_handler_install(guard)
            if type(exc) is KeyboardInterrupt and self._previous_sigint in (
                None,
                signal.SIG_DFL,
                signal.default_int_handler,
            ):
                # SIGINT can arrive immediately before pthread_sigmask takes
                # effect. Match the default-handler behavior the pool would
                # have provided one instruction later, without publishing an
                # unmasked intermediate handler.
                raise TtsSynthesisInterrupted from None
            raise
        finally:
            if expected_signal_error:
                self._rollback_failed_handler_install(guard)
            elif guard is not None:
                try:
                    guard.restore()
                except BaseException:
                    # A pending SIGINT delivered by the unmask is the primary.
                    # Since __enter__ cannot complete, restore the previous
                    # owner without replacing that typed interrupt.
                    self._rollback_failed_handler_install(guard)
                    raise
        if expected_signal_error and not self._signal_installed:
            self._handler = None
        return self

    def _reconcile_handler_truth(self) -> None:
        """Make the publication flag agree with the process signal table."""
        self._signal_installed = signal.getsignal(signal.SIGINT) is self._handler

    def _rollback_failed_handler_install(
        self,
        guard: SigintMaskGuard | None,
    ) -> None:
        """Best-effort rollback that cannot replace an entering exception."""
        primary = sys.exception()
        try:
            try:
                if (
                    self._handler is not None
                    and signal.getsignal(signal.SIGINT) is self._handler
                ):
                    signal.signal(signal.SIGINT, self._previous_sigint)
            finally:
                self._reconcile_handler_truth()
        except BaseException:
            if primary is None:
                raise
        finally:
            if guard is not None:
                guard.restore_preserving_primary()

    def submit(self, fn: Any, /, *args: Any, **kwargs: Any) -> Future[Any]:
        guard: SigintMaskGuard | None = None
        try:
            self._submission_in_progress = True
            if self._signal_installed:
                guard = SigintMaskGuard.acquire()
            # Preclaim before the executor can publish work. Ownership remains
            # uncertain until the returned Future is durably tracked.
            self._submission_uncertain = True
            future = self._pool.submit(self._run_owned, fn, args, kwargs)
            self._futures.append(future)
            self._submission_uncertain = False
            return future
        finally:
            self._submission_in_progress = False
            if guard is not None:
                guard.restore_preserving_primary()

    def _run_owned(self, fn: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        token = _CURRENT_SYNTH_OWNER.set(self)
        try:
            return fn(*args, **kwargs)
        finally:
            _CURRENT_SYNTH_OWNER.reset(token)

    def register_synth_process(self, process: Any) -> None:
        self._process_lifecycle.preclaim(process)

    def spawn_synth_process(
        self,
        process: Any,
        command: list[str],
        **kwargs: Any,
    ) -> None:
        self._process_lifecycle.spawn(process, command, **kwargs)

    def publish_synth_process_pidfd(self, process: Any, identity: Any) -> bool:
        return self._process_lifecycle.publish(process, identity)

    def unregister_synth_process(self, process: Any) -> None:
        self._process_lifecycle.release(process)

    def wait_and_unregister_synth_process(
        self, process: Any, *, deadline: float | None = None,
    ) -> int:
        return self._process_lifecycle.wait_and_release(process, deadline=deadline)

    def cancel_synth_process(self, process: Any) -> bool:
        return self._process_lifecycle.cancel()

    def close_synth_identity_if_unowned(self, process: Any, identity: Any) -> None:
        self._process_lifecycle.close_identity_if_unowned(process, identity)

    def _terminate_synth_process_for_exit(self) -> bool:
        return self._process_lifecycle.cancel()

    def _hard_exit(self) -> bool:
        try:
            reaped = self._terminate_synth_process_for_exit()
        except BaseException:
            # Keep the handler and lifecycle authority live. A nested/repeated
            # signal can resume and strengthen the same cancellation.
            return False
        if not reaped:
            return False
        if self._process_exit_expected:
            os._exit(TtsSynthesisInterrupted.exit_code)
        return True

    def _unfinished(self) -> bool:
        return (
            self._submission_in_progress
            or self._submission_uncertain
            or any(not future.done() for future in self._futures)
        )

    def _handle_sigint(self, signum: int, frame: Any) -> None:
        self._sigint_count += 1
        if self._sigint_count >= 2 and self._escalation_authorized:
            if not self._process_exit_expected:
                if not self._hard_exit():
                    return
                self._restore_handler()
                self._delegate_previous(signum, frame)
                return
            if self._terminal_exit_armed and self._unfinished():
                self._hard_exit()
                return
            if not self._terminal_exit_armed:
                # Ticket abandonment and mute repair live outside the executor
                # context. Give those ownership releases a bounded grace
                # interval, then honor the repeat even if cleanup itself stalls.
                self._repeated_sigint_pending = True
                if self._sigint_count >= 3:
                    self._hard_exit()
                    return
                self._start_cleanup_deadline()
                return
            # The provider completed between the first unwind and this signal.
            self._restore_handler()
            if not self._process_exit_expected:
                self._delegate_previous(signum, frame)
            return

        previous = self._previous_sigint
        if previous in (None, signal.SIG_DFL, signal.default_int_handler):
            self._escalation_authorized = True
            self._typed_interrupt_pending = True
            raise TtsSynthesisInterrupted
        if previous == signal.SIG_IGN:
            return
        try:
            previous(signum, frame)
        except BaseException:
            # A custom owner (for example a structured CLI interrupt policy)
            # authorizes escalation only by actually beginning an unwind.
            self._escalation_authorized = True
            if not self._unfinished():
                self._restore_handler()
            raise

    def _delegate_previous(self, signum: int, frame: Any) -> None:
        previous = self._previous_sigint
        if previous in (None, signal.SIG_DFL, signal.default_int_handler):
            signal.default_int_handler(signum, frame)
            return
        if previous == signal.SIG_IGN:
            return
        previous(signum, frame)

    def _restore_handler(self) -> None:
        if not self._signal_installed:
            return
        entry_primary = sys.exception()
        guard: SigintMaskGuard | None = None
        try:
            guard = SigintMaskGuard.acquire()
            try:
                if signal.getsignal(signal.SIGINT) is self._handler:
                    signal.signal(signal.SIGINT, self._previous_sigint)
            finally:
                # Reconcile while SIGINT is still masked. signal.signal may
                # raise after taking effect, so success-by-return is not truth.
                self._reconcile_handler_truth()
        except (OSError, ValueError):
            pass
        except BaseException:
            # Restoration called while another exception is unwinding must
            # preserve that first primary. Actual-handler truth was reconciled
            # in the inner finally, so a later cleanup attempt remains safe.
            if entry_primary is None:
                raise
        finally:
            try:
                if guard is not None:
                    guard.restore_preserving_primary()
            finally:
                if not self._signal_installed:
                    self._cancel_cleanup_deadline()

    def _start_cleanup_deadline(self) -> None:
        if self._cleanup_timer is not None:
            return
        try:
            timer = threading.Timer(
                _SYNTH_INTERRUPT_CLEANUP_GRACE_S,
                self._cleanup_deadline_expired,
            )
            timer.daemon = True
            self._cleanup_timer = timer
            timer.start()
        except BaseException:
            # The repeated interrupt is an explicit terminal request. If even
            # the bounded grace watchdog cannot start, fail closed immediately.
            self._hard_exit()

    def _cancel_cleanup_deadline(self) -> None:
        timer = self._cleanup_timer
        self._cleanup_timer = None
        if timer is not None:
            try:
                timer.cancel()
            except BaseException:
                # Deadline cancellation is idempotent bookkeeping. It must
                # never replace the interrupt or cleanup exception that made
                # the watchdog obsolete.
                pass

    def _cleanup_deadline_expired(self) -> None:
        if self._escalation_authorized and self._repeated_sigint_pending:
            self._hard_exit()

    def arm_terminal_exit(self) -> None:
        """Open the hard-exit gate after outer run_tts cleanup has completed."""
        if not self._escalation_authorized:
            return
        self._terminal_exit_armed = True
        if self._process_exit_expected and self._unfinished():
            register_cli_terminal_wait(self._wait_for_cli_terminal_exit)
        if self._repeated_sigint_pending and self._unfinished():
            self._hard_exit()
            return
        self._repeated_sigint_pending = False
        self._cancel_cleanup_deadline()
        if not self._unfinished() and not self._typed_interrupt_pending:
            self._restore_handler()

    def _wait_for_cli_terminal_exit(self) -> None:
        """Keep Python signal dispatch live after the CLI emits cancellation.

        Executor interpreter-exit joins ignore shutdown(wait=False). A native
        SIGINT delivered on another thread can leave Python's main-thread
        handler pending forever in that join. Timed application waits provide
        dispatch points without resending signals or abandoning library work.
        """
        if not self._unfinished():
            if not self._typed_interrupt_pending:
                self._restore_handler()
            return
        guard = SigintMaskGuard.acquire()
        try:
            # Ask's capture scope may have handed SIGINT back to the CLI after
            # run_tts unwound. Own only this terminal wait and preserve that
            # live predecessor, rather than reviving a closed capture scope.
            current = signal.getsignal(signal.SIGINT)
            if current is not self._handler:
                self._previous_sigint = current
                try:
                    signal.signal(signal.SIGINT, self._handler)
                finally:
                    self._reconcile_handler_truth()
        finally:
            guard.restore_preserving_primary()
        wakeup = threading.Event()
        try:
            while self._unfinished():
                wakeup.wait(0.05)
        finally:
            if not self._unfinished() and not self._typed_interrupt_pending:
                self._restore_handler()

    def _detach_after_interrupt(self) -> None:
        """Stop waiting without replacing the exception already unwinding."""
        try:
            self._pool.shutdown(wait=False, cancel_futures=True)
        except BaseException:
            # A repeated SIGINT remains the terminal fallback even if executor
            # bookkeeping itself fails. Never replace the first primary here.
            pass
        if not self._unfinished() and not self._typed_interrupt_pending:
            self._restore_handler()

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        if self._escalation_authorized:
            self._detach_after_interrupt()
            # If work is still running, intentionally retain the handler for
            # the repeated-SIGINT terminal path during interpreter shutdown.
            return False

        try:
            self._pool.shutdown(wait=True)
        finally:
            # SIGINT can begin while shutdown(wait=True) is joining a worker.
            # Re-read the state here rather than restoring from the stale
            # pre-wait snapshot that entered this branch.
            if self._escalation_authorized:
                self._detach_after_interrupt()
            else:
                self._restore_handler()
        return False


def soft_truncate_text(text: str, max_chars: int) -> str:
    """Cut *text* to ≤ ``max_chars`` at the last word/sentence boundary if possible."""
    text = text or ""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    window = text[:max_chars]
    for sep in (". ", "? ", "! ", "; ", ", ", " "):
        cut = window.rfind(sep)
        if cut >= max(1, max_chars // 4):
            return window[: cut + len(sep)].rstrip()
    return window.rstrip()


# B095: operator visual quick-reference for TTS questions (ask / Mode A).
_ITEM_PREFIX_RE = re.compile(
    r"^\s*(?:"
    r"(?:Q\s*)?\d+\s*[\.\)\:\-–—]"  # 1.  1)  Q1:  2-
    r"|[A-Za-z]\s*[\.\)]"  # A.  b)
    r"|[-*•·]"  # bullets
    r")\s+",
    re.IGNORECASE,
)
# Capitalized ordinals only, at start or after sentence/line break — avoids
# mid-clause false hits like "the second: attempt".
_WORD_ORDINAL_SPLIT_RE = re.compile(
    r"(?:(?<=^)|(?<=[.!?\n][ \t])|(?<=\n))"
    r"(?:"
    r"One|Two|Three|Four|Five|Six|Seven|Eight|Nine|Ten|"
    r"First|Second|Third|Fourth|Fifth|Sixth|Seventh|Eighth|Ninth|Tenth"
    r")"
    r"\s*[:.—–-]\s+"
)
_Q_LABEL_SPLIT_RE = re.compile(
    r"(?:(?<=^)|(?<=\s))Q\s*\d+\s*[:.\-–—]\s*",
    re.IGNORECASE,
)


def _strip_item_prefix(item: str) -> str:
    """Remove a leading list/number prefix so we can renumber cleanly."""
    s = (item or "").strip()
    if not s:
        return s
    return _ITEM_PREFIX_RE.sub("", s, count=1).strip() or s


def extract_question_items(text: str) -> list[str]:
    """Split multi-item interview prompts when the pattern is obvious.

    Returns a single-element list when no multi-item structure is detected.
    """
    text = (text or "").strip()
    if not text:
        return []

    # 1) Explicit multi-line list (numbered, lettered, or bulleted)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) >= 2:
        prefixed = sum(1 for ln in lines if _ITEM_PREFIX_RE.match(ln))
        if prefixed >= max(2, (len(lines) + 1) // 2):
            return [_strip_item_prefix(ln) for ln in lines]

    # 2) Q1: / Q2: labels in a single block
    q_parts = [p.strip() for p in _Q_LABEL_SPLIT_RE.split(text) if p.strip()]
    if len(q_parts) >= 2 and _Q_LABEL_SPLIT_RE.search(text):
        return q_parts

    # 3) Word ordinals: "One: … Two: …" / "First — … Second — …"
    if _WORD_ORDINAL_SPLIT_RE.search(text):
        word_parts = [
            p.strip() for p in _WORD_ORDINAL_SPLIT_RE.split(text) if p.strip()
        ]
        if len(word_parts) >= 2:
            return word_parts

    # 4) Several distinct questions (sentence-ending '?')
    q_sents = re.findall(r"[^?]+?\?", text)
    if len(q_sents) >= 2:
        cleaned = [s.strip() for s in q_sents if s.strip()]
        # Require leftover after last ? is short/empty so we don't chop prose badly
        tail = text[sum(len(s) for s in q_sents) :].strip()
        if cleaned and len(tail) < 40:
            if tail:
                cleaned[-1] = f"{cleaned[-1]} {tail}".strip()
            return cleaned

    # 5) Plain multi-line paragraphs (2+ non-trivial lines, not one prose block)
    if len(lines) >= 2:
        # Prefer list-like short lines over a reflowed paragraph
        if all(len(ln) <= 200 for ln in lines) and len(lines) <= 12:
            return lines

    return [text]


def format_tts_question_text(text: str) -> str:
    """Readable operator form of a TTS question; numbers multi-item prompts."""
    text = (text or "").strip()
    if not text:
        return ""
    items = extract_question_items(text)
    if len(items) <= 1:
        return text
    return "\n".join(
        f"{i}. {_strip_item_prefix(item)}" for i, item in enumerate(items, 1)
    )


def print_tts_question_text(
    text: str,
    *,
    stream: TextIO | None = None,
) -> None:
    """Print full question text to the controlling terminal as TTS starts (B095).

    Uses stderr so JSON results / radio partials on stdout stay machine-parseable.
    """
    body = format_tts_question_text(text)
    if not body:
        return
    out = stream if stream is not None else sys.stderr
    bar = "=" * 36
    print(f"{bar} hark question {bar}", file=out, flush=True)
    print(body, file=out, flush=True)
    print("=" * (36 * 2 + len(" hark question ")), file=out, flush=True)


def maybe_print_tts_question(cfg: HarkConfig, text: str) -> None:
    """Print TTS question when ``tts.print_prompt`` is enabled (default on)."""
    if not bool(getattr(cfg.tts, "print_prompt", True)):
        return
    try:
        print_tts_question_text(text)
    except Exception:
        # Never fail speak/listen because the terminal write failed.
        pass


def surface_tts_event(kind: str, **fields: Any) -> None:
    """Syslog + ambient.jsonl so ``hark monitor`` can surface TTS lifecycle (B091)."""
    try:
        from hark.syslog import log

        log(kind, component="tts", **fields)
    except Exception:
        pass
    try:
        from hark.events import new_event_id, utc_now_iso
        from hark.monitor_feed import append_ambient_jsonl

        event = {
            "schema": "hark.event.v1",
            "kind": kind,
            "event_id": new_event_id(),
            "observed_at": utc_now_iso(),
            **fields,
        }
        append_ambient_jsonl(event)
    except Exception:
        pass


def pack_tts_chunks(text: str, max_chars: int) -> list[str]:
    """Split *text* into ≤ ``max_chars`` pieces at sentence/word boundaries (B091).

    Never mid-word cuts when a space exists in the window. ``max_chars <= 0``
    means a single chunk (no limit).
    """
    text = (text or "").strip()
    if not text:
        return []
    if max_chars <= 0 or len(text) <= max_chars:
        return [text]

    # Prefer sentence ends, then clauses, then words.
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks: list[str] = []
    buf = ""

    def _flush() -> None:
        nonlocal buf
        if buf.strip():
            chunks.append(buf.strip())
        buf = ""

    def _append_piece(piece: str) -> None:
        nonlocal buf
        piece = piece.strip()
        if not piece:
            return
        if len(piece) > max_chars:
            _flush()
            # Word-pack oversized sentences
            words = piece.split()
            for w in words:
                if not buf:
                    if len(w) > max_chars:
                        # single token longer than limit — hard split (rare)
                        while len(w) > max_chars:
                            chunks.append(w[:max_chars])
                            w = w[max_chars:]
                        buf = w
                    else:
                        buf = w
                elif len(buf) + 1 + len(w) <= max_chars:
                    buf = f"{buf} {w}"
                else:
                    _flush()
                    buf = w
            return
        if not buf:
            buf = piece
        elif len(buf) + 1 + len(piece) <= max_chars:
            buf = f"{buf} {piece}"
        else:
            _flush()
            buf = piece

    for s in sentences:
        _append_piece(s)
    _flush()
    return chunks or [text[:max_chars]]


def run_tts(
    cfg: HarkConfig,
    text: str,
    *,
    provider: str | None = None,
    voice: str | None = None,
    play: bool = True,
    out: Path | None = None,
    max_chars: int | None = None,
    mute_mic: bool | None = None,
    on_near_end: Any | None = None,
    near_end_ms: int | None = None,
    conference_policy: str | None = None,
    use_cache: bool = True,
    play_wait_timeout_s: float | None = None,
) -> dict[str, Any]:
    """Synthesize and optionally play TTS.

    ``conference_policy`` (B017):
      - ``None`` / default: ``hold`` when ``audio.hold_during_conference``, else ``force``
      - ``hold``: wait for Zoom/Teams/Meet etc. to end (soft chime optional)
      - ``skip``: do not speak while conference active (lifecycle cues)
      - ``force``: always speak immediately

    ``use_cache``: when False, skip on-disk TTS phrase cache lookup and store
    (one-shot announces such as wake-label live-reload).

    ``play_wait_timeout_s`` (B099/B146): max cumulative seconds spent acquiring
    the play ticket lock and waiting for the exclusive queue turn. Synthesis and
    listen deferral do not consume this queue-wait budget. On timeout the ticket
    is abandoned and ``TimeoutError`` is raised. Ambient boot uses a short timeout
    so a stuck queue never blocks wake arming.

    Long text (B091): speak the full agent reply by default.

    - ``tts.max_chars`` (0 = unlimited): optional **total** cap; soft word-boundary
      cut + ``tts.truncated`` HEP/syslog (monitor-visible) if exceeded.
    - ``tts.chunk_chars``: per-provider synth size; multi-chunk play under one
      mute/duck hold so long replies are not mid-word chopped.
    - ``tts.playback_speed``: pitch-preserving playback tempo (default 1.0).
    """
    full_text = (text or "").strip()
    if not full_text:
        raise ProviderError("empty TTS text")

    total_limit = max_chars if max_chars is not None else int(cfg.tts.max_chars or 0)
    chunk_limit = int(getattr(cfg.tts, "chunk_chars", 1500) or 1500)
    if chunk_limit <= 0:
        chunk_limit = 1500

    truncated = False
    original_chars = len(full_text)
    if total_limit > 0 and len(full_text) > total_limit:
        full_text = soft_truncate_text(full_text, total_limit)
        truncated = True
        surface_tts_event(
            "tts.truncated",
            original_chars=original_chars,
            kept_chars=len(full_text),
            max_chars=total_limit,
            text_preview=full_text[:160],
            instructions=(
                "TTS text was truncated to tts.max_chars. Full agent text was NOT spoken. "
                "Set [tts].max_chars = 0 (unlimited) or raise the limit."
            ),
        )

    chunks = pack_tts_chunks(full_text, chunk_limit)
    chunked = len(chunks) > 1
    if chunked:
        surface_tts_event(
            "tts.chunked",
            chars=len(full_text),
            chunk_chars=chunk_limit,
            n_chunks=len(chunks),
            chunk_lens=[len(c) for c in chunks],
            instructions="Long TTS multi-chunk play (informational).",
        )

    # Handsfree path (hark tts / ask): hold full question speech during conference.
    hold_meta: dict[str, Any] | None = None
    if play:
        from hark.conference import apply_conference_hold

        policy = conference_policy
        if policy is None:
            policy = "hold" if cfg.audio.hold_during_conference else "force"
        hold = apply_conference_hold(cfg, full_text, policy=policy)
        hold_meta = hold.as_meta()
        if hold.skipped:
            return {
                "ok": True,
                "provider": "skipped",
                "voice": voice or cfg.tts.voice or "eve",
                "truncated": truncated,
                "chunked": chunked,
                "chunks": len(chunks),
                "chars": len(full_text),
                "words": len(full_text.split()),
                "out": None,
                "content_type": None,
                "audio_ms": 0,
                "latency_ms": 0,
                "mic_muted": False,
                "from_cache": False,
                "conference": hold_meta,
                "skipped": True,
                "reason": "conference",
            }

    do_mute = cfg.audio.mute_mic_during_tts if mute_mic is None else mute_mic
    store = UsageStore()
    t0 = time.monotonic()
    # Voice order: explicit param → custom_voice (when TTS pins custom) →
    # tts.voice (provider-generic default) → "eve" fallback.
    _requested = provider or cfg.tts.provider
    _req_norm = (_requested or "auto").lower().strip()
    if _req_norm in ("custom", "custom_tts", "custom-tts"):
        voice_id = voice or getattr(cfg.tts, "custom_voice", None) or cfg.tts.voice or "eve"
    else:
        voice_id = voice or cfg.tts.voice or "eve"
    # Resolve once in the parent (TTY MiniMax consent + disabled list) so the
    # isolated synth worker receives a concrete provider name.
    _chosen = resolve_tts(
        _requested,
        voice=voice_id,
        language=cfg.tts.language,
        tts_cfg=cfg.tts,
        allow_prompt=True,
        config_path=cfg.path,
    )
    resolved_provider = getattr(_chosen, "name", None) or _requested
    provider_name = resolved_provider
    content_type = "audio/mpeg"
    used_voice = voice_id
    from_cache = False
    audio_parts: list[bytes] = []
    play_ms = 0
    mute_applied = False
    mute_was_muted: bool | None = None
    mute_skipped_capture = False
    mute_repair: dict[str, Any] | None = None
    synth_pool: _InterruptibleSynthPool | None = None
    duck_meta: dict[str, Any] | None = None
    defer_meta: dict[str, Any] | None = None
    user_skipped = False
    near = near_end_ms if near_end_ms is not None else int(cfg.audio.listen_pre_arm_ms)
    do_duck = bool(getattr(cfg.audio, "duck_media_during_tts", True))
    # B177 fallback state. `speaking_provider` is who the *next* chunk asks
    # first; chunks may synthesize concurrently (the no-play branch), so the
    # lock guards the handover and the step log.
    fallback_lock = threading.Lock()
    speaking_provider = resolved_provider
    fallback_chain: list[str] | None = None
    fallback_steps: list[dict[str, Any]] = []

    def _transport_synth(prov: str, piece_text: str) -> Any:
        owner = _CURRENT_SYNTH_OWNER.get()
        if owner is None:
            raise RuntimeError("missing synth process owner")
        transport = _synth_transport_factory(owner)
        return transport.synthesize(
            SynthRequest(
                provider=prov,
                # Voice ids are per-provider namespaces, not a shared vocabulary:
                # "eve" is xAI's and OpenAI / MiniMax reject it outright, while
                # Google ignores the field entirely. Carrying the requested id
                # onto a fallback rung would turn a content-shaped 400 into a
                # voice-not-found 400 — in the gh-9 configuration that was the
                # only rung available. Blank it so the new provider uses its own
                # default; the result reports whatever voice actually spoke.
                voice=voice_id if prov == resolved_provider else "",
                language=cfg.tts.language,
                text=piece_text,
            )
        )

    def _fallback_chain() -> list[str]:
        """Resolve the fallback walk once, on first eligible failure only."""
        nonlocal fallback_chain
        with fallback_lock:
            if fallback_chain is None:
                try:
                    fallback_chain = fallback_provider_chain(
                        speaking_provider,
                        voice=voice_id,
                        language=cfg.tts.language,
                        tts_cfg=cfg.tts,
                        config_path=cfg.path,
                    )
                except Exception:
                    fallback_chain = []
            return list(fallback_chain)

    def _on_fallback_step(step: FallbackStep, piece: str) -> None:
        """Make a downgrade loud: HEP + syslog + one operator-visible line."""
        with fallback_lock:
            fallback_steps.append(step.as_meta())
        # Keep the usage/audit trail complete: the failed attempt cost a
        # request even though it produced no audio.
        _record_synth_fail(piece, step.error, provider=step.from_provider)
        if step.error_class == TEXT_INSTEAD_OF_AUDIO:
            why = "failed on a content-shaped provider error"
        elif step.error_class == "authentication":
            why = "rejected its credentials or required audio scope"
        else:
            why = (
                "was resolvable but not usable (its credentials are present "
                "but the call failed), so it is skipped as a fallback target"
            )
        surface_tts_event(
            "tts.provider_fallback",
            **step.as_meta(),
            instructions=(
                f"TTS provider {step.from_provider!r} {why}; Hark is "
                f"continuing with {step.to_provider!r} (B164/B177). The voice the "
                "operator hears may differ from the configured provider — "
                "report which provider spoke, never a plain success."
            ),
        )
        try:
            print(
                # Not "hark tts:" — this fires under ask / ambient too.
                f"hark: TTS {step.from_provider} failed ({step.error_class}); "
                f"{step.step} with {step.to_provider}",
                file=sys.stderr,
                flush=True,
            )
        except Exception:
            # Never fail speech because the terminal write failed.
            pass

    def _log_fallback_declined(exc: BaseException) -> None:
        """Surface the classifier saying NO to a 4xx (B177).

        The eligible class is matched on substrings of an upstream response
        body. If Google rewords its 400, the fallback silently stops applying
        and `ask` dead-ends again with nothing to indicate anything changed.
        Publishing the refusal turns a reworded body into a diagnosable event
        instead of a regression rediscovered by an operator who cannot ask a
        question. This does NOT widen the classifier — a declined error still
        fails fast.
        """
        status = tts_error_http_status(exc)
        if status is None or not (400 <= status < 500):
            return
        surface_tts_event(
            "tts.fallback_declined",
            provider=resolved_provider,
            http_status=status,
            error_class="none",
            error=safe_exception_detail(exc),
            instructions=(
                f"A {status} from {resolved_provider!r} was NOT classified as "
                "fallback-eligible, so TTS failed fast (B177). Correct for an "
                "auth or quota problem. If this is the Gemini 'tried to "
                "generate text' 400 with reworded text, the classifier in "
                "hark/providers/tts_fallback.py needs the new wording — check "
                "the error field against _TEXT_NOT_AUDIO_MARKERS."
            ),
        )

    def _synth_one(piece: str) -> tuple[bytes, str, str, str, bool]:
        """Return (audio, provider, content_type, voice, from_cache)."""
        nonlocal speaking_provider
        cached = lookup_cached_tts(voice_id, piece) if use_cache else None
        if cached is not None:
            return cached, "cache", "audio/mpeg", voice_id, True
        with fallback_lock:
            start_provider = speaking_provider
        # B177 retries content-shaped errors once; B164 skips rejected auth
        # directly to the auto walk. Later chunks start from whoever succeeded,
        # so a long reply does not re-fail on every chunk.
        try:
            result, steps = synthesize_with_fallback(
                provider=start_provider,
                text=piece,
                call=_transport_synth,
                chain=_fallback_chain,
                on_step=lambda step: _on_fallback_step(step, piece),
                enabled=bool(getattr(cfg.tts, "fallback_on_error", True)),
                language=cfg.tts.language,
                auto_auth=_req_norm == "auto",
            )
        except BaseException as exc:
            if classify_tts_error(exc) is None and not (
                _req_norm == "auto" and is_auth_failure(exc)
            ):
                # Only a refusal is news: an eligible error that exhausted the
                # ladder is already reported hop by hop.
                _log_fallback_declined(exc)
            raise
        switched = any(step.step == "fallback" for step in steps)
        if switched and result.provider:
            # Only a real provider switch hands over. A same-provider retry
            # leaves the chosen provider in place.
            with fallback_lock:
                speaking_provider = result.provider
        # The phrase cache keys on (voice, text) with no provider, so storing a
        # fallback provider's audio would make every later run replay it as
        # provider="cache" with no fallback record — a silent downgrade that
        # reads as a clean success. Cache only what the CHOSEN provider spoke:
        # no switch in this chunk, and no earlier chunk already handed over
        # (a later chunk starts straight on the fallback and takes no step of
        # its own). A hardened retry that the chosen provider answered is still
        # its own audio, so it caches normally.
        chosen_spoke = not switched and start_provider == resolved_provider
        if use_cache and len(piece) <= 120 and chosen_spoke:
            try:
                # Cache under the ORIGINAL piece: a hardened retry changes the
                # request, never what the operator asked to be spoken.
                store_cached_tts(result.voice or voice_id, piece, result.audio)
            except Exception:
                pass
        return (
            result.audio,
            result.provider,
            result.content_type,
            result.voice or voice_id,
            False,
        )

    # B092: synth may run in parallel across processes; play is exclusive.
    # First chunk is synthesized *before* taking the speaker lock so a second
    # `hark tts` started in quick succession can hit xAI while we still play.
    # Multi-chunk: start synth(i+1) while playing chunk i (pipeline).
    def _record_synth_fail(
        piece: str,
        exc: BaseException | str,
        *,
        provider: str | None = None,
    ) -> None:
        # The ladder attaches the provider it asked last; without it a fallback
        # exhaustion would be filed against the originally requested provider.
        attributed = provider or getattr(exc, "tts_last_provider", None)
        store.record_tts(
            text=piece,
            provider=attributed or resolved_provider,
            voice=voice_id,
            ok=False,
            error=str(exc)[:200],
            latency_ms=int(1000 * (time.monotonic() - t0)),
        )

    def _apply_synth(
        audio_bytes: bytes,
        p_name: str,
        c_type: str,
        v_used: str,
        fc: bool,
    ) -> bytes:
        nonlocal provider_name, content_type, used_voice, from_cache
        provider_name = p_name
        content_type = c_type
        used_voice = v_used
        from_cache = from_cache or fc
        audio_parts.append(audio_bytes)
        return audio_bytes

    try:
        if play:
            # Claim FIFO slot *before* synth so 5 concurrent launches keep order
            # even if later jobs finish synthesizing first (B092).
            claim_wait_started = time.monotonic()
            if play_wait_timeout_s is None:
                play_ticket = claim_tts_play_ticket()
            else:
                play_ticket = claim_tts_play_ticket(
                    lock_timeout_s=max(0.0, float(play_wait_timeout_s))
                )
            claim_wait_elapsed = time.monotonic() - claim_wait_started
            try:
                synth_pool = _InterruptibleSynthPool()
                with synth_pool as pool:
                    # Kick first synth outside the play hold (parallel with other jobs)
                    fut: Future[tuple[bytes, str, str, str, bool]] = pool.submit(
                        _synth_one, chunks[0]
                    )
                    try:
                        audio_bytes, p_name, c_type, v_used, fc = fut.result()
                    except Exception as exc:
                        _record_synth_fail(chunks[0], exc)
                        raise
                    _apply_synth(audio_bytes, p_name, c_type, v_used, fc)

                    # Prefetch chunk 1 while we may still wait for our play turn
                    next_fut: Future[tuple[bytes, str, str, str, bool]] | None = None
                    if len(chunks) > 1:
                        next_fut = pool.submit(_synth_one, chunks[1])

                    # B097 / B105: if operator listen/radio is open, defer play +
                    # mic mute. HOLD mode waits until capture ends; streaming mode
                    # waits for streaming_ack_min_quiet_s of operator quiet (or
                    # stream end) so short acks do not barge into continuous speech.
                    # Synth may already be done; same-PID capture is ignored so
                    # in-listen nudges still speak.
                    if bool(getattr(cfg.audio, "defer_tts_while_listening", True)):
                        # P1.M6: quiet-gate from active listen policy / explicit
                        # seam — not raw [ambient].streaming getattr (bound HOLD).
                        streaming_ack, min_quiet = _tts_defer_streaming_params(cfg)
                        defer = wait_until_tts_play_allowed(
                            streaming=streaming_ack,
                            min_quiet_s=min_quiet,
                            max_wait_s=float(
                                getattr(cfg.audio, "defer_tts_max_wait_s", 45.0)
                            ),
                            poll_ms=int(getattr(cfg.audio, "defer_tts_poll_ms", 100)),
                            quiet_ms=int(getattr(cfg.audio, "defer_tts_quiet_ms", 200)),
                        )
                        defer_meta = defer.as_meta()
                        if defer.deferred:
                            if streaming_ack and defer.gate == "quiet":
                                instructions = (
                                    "TTS play waited for operator quiet "
                                    f"(≥{min_quiet:g}s) or listen end so "
                                    "streaming acks do not barge into continuous "
                                    "speech (B105). "
                                    "Set [audio].defer_tts_while_listening = false "
                                    "to disable; policy streaming_ack_min_quiet_s "
                                    "tunes the quiet gate."
                                )
                            else:
                                instructions = (
                                    "TTS play waited for operator listen/radio to "
                                    "finish so mic mute would not cut off capture. "
                                    "Set [audio].defer_tts_while_listening = false "
                                    "to disable."
                                )
                            surface_tts_event(
                                "tts.deferred_for_listen",
                                **defer_meta,
                                instructions=instructions,
                            )

                    remaining_play_wait_s = (
                        None
                        if play_wait_timeout_s is None
                        else max(
                            0.0,
                            float(play_wait_timeout_s) - claim_wait_elapsed,
                        )
                    )
                    with exclusive_playback(
                        ticket=play_ticket,
                        wait_timeout_s=remaining_play_wait_s,
                    ):
                        # B119: re-probe at play time. Streaming quiet-gate (B105)
                        # may allow short acks while listen is STILL ACTIVE — that
                        # must not system-mute the mic (cuts open radio / mid-thought).
                        # Same-PID capture is ignored so in-listen nudges can mute.
                        play_mute = bool(do_mute)
                        if play_mute:
                            try:
                                cap = user_capture_active(ignore_own_pid=True)
                                if cap.active:
                                    play_mute = False
                                    mute_skipped_capture = True
                                    syslog(
                                        "tts.mute_skipped_capture",
                                        component="tts",
                                        level="info",
                                        reason=cap.reason,
                                        sources=list(cap.sources),
                                        stream_id=cap.stream_id,
                                        mode=cap.mode,
                                        message=(
                                            "skip mic mute while operator "
                                            "listen/radio capture is open (B119)"
                                        ),
                                    )
                            except Exception:
                                pass
                        with mic_muted_during_tts(enabled=play_mute) as mute_state:
                            mute_applied = bool(getattr(mute_state, "applied", False))
                            mute_was_muted = getattr(mute_state, "was_muted", None)
                            with duck_media(
                                cfg, enabled=do_duck, exclude_conference=True
                            ) as duck_state:
                                duck_meta = duck_state.as_meta()
                                # B161: desktop notification with the full text
                                # and a Skip action for the playback span.
                                # Snapshot the skip generation *before* showing
                                # the notification so a click during spawn is
                                # still honored.
                                skip_gen = playback_skip_generation()
                                skip_inv = playback_skip_invocations()
                                chunks_played = 0
                                with tts_skip_notification(cfg, full_text):
                                    for i in range(len(chunks)):
                                        if playback_skip_generation() != skip_gen:
                                            # Skip clicked while we were between
                                            # chunks (e.g. waiting on prefetch
                                            # synth) — do not start the next one.
                                            break
                                        is_last = i == len(chunks) - 1
                                        pr = play_wav_bytes(
                                            audio_parts[i],
                                            playback_speed=cfg.tts.playback_speed,
                                            on_near_end=on_near_end
                                            if is_last
                                            else None,
                                            near_end_ms=near
                                            if (on_near_end and is_last)
                                            else 0,
                                            exclusive=False,
                                        )
                                        play_ms += pr.duration_ms
                                        chunks_played = i + 1
                                        if playback_skip_generation() != skip_gen:
                                            # User clicked Skip on the TTS
                                            # notification (B161): stop the
                                            # remaining chunks too.
                                            break
                                        if i + 1 >= len(chunks):
                                            break
                                        # Resolve prefetched next; kick following while we play
                                        assert next_fut is not None
                                        try:
                                            ab, pn, ct, vu, fch = next_fut.result()
                                        except Exception as exc:
                                            _record_synth_fail(chunks[i + 1], exc)
                                            raise
                                        _apply_synth(ab, pn, ct, vu, fch)
                                        if i + 2 < len(chunks):
                                            next_fut = pool.submit(
                                                _synth_one, chunks[i + 2]
                                            )
                                        else:
                                            next_fut = None
                                if playback_skip_generation() != skip_gen and (
                                    playback_skip_invocations() != skip_inv
                                    or chunks_played < len(chunks)
                                ):
                                    # A skip was requested AND actually cut
                                    # playback short (a stopper was invoked,
                                    # or unplayed chunks remained). A stray
                                    # click landing after the final chunk
                                    # finished is ignored — the full message
                                    # was heard.
                                    user_skipped = True
                                    surface_tts_event(
                                        "tts.skipped",
                                        reason="user_notification",
                                        chunk=chunks_played,
                                        chunks=len(chunks),
                                        chars=len(full_text),
                                        instructions=(
                                            "TTS playback skipped from the "
                                            "desktop notification (B161). The "
                                            "operator may not have heard the "
                                            "full message."
                                        ),
                                    )
            except BaseException as primary_exc:
                # Don't stall the FIFO if we never reached exclusive_playback.
                # Claim, queue wait, and cleanup share one acquisition budget;
                # synthesis/listen deferral intentionally do not consume it.
                queue_wait_elapsed = (
                    max(0.0, float(primary_exc.elapsed_s))
                    if isinstance(primary_exc, TtsPlayTimeout)
                    else 0.0
                )
                cleanup_lock_timeout_s = (
                    None
                    if play_wait_timeout_s is None
                    else max(
                        0.0,
                        float(play_wait_timeout_s)
                        - claim_wait_elapsed
                        - queue_wait_elapsed,
                    )
                )
                try:
                    abandon_tts_play_ticket(
                        play_ticket,
                        lock_timeout_s=cleanup_lock_timeout_s,
                    )
                except TtsPlayLockTimeout:
                    if not defer_tts_play_ticket_abandon(play_ticket):
                        # One bounded retry mirrors exclusive_playback. If both
                        # publications fail, the retained request is recovered
                        # by the next successful same-process lock transaction.
                        defer_tts_play_ticket_abandon(play_ticket)
                except BaseException:
                    # Swallow secondary cleanup faults (incl. KeyboardInterrupt)
                    # so the primary exception remains the process exit cause.
                    pass
                raise
        else:
            synth_pool = _InterruptibleSynthPool()
            with synth_pool as pool:
                futs = [pool.submit(_synth_one, piece) for piece in chunks]
                for piece, f in zip(chunks, futs):
                    try:
                        ab, pn, ct, vu, fch = f.result()
                    except Exception as exc:
                        _record_synth_fail(piece, exc)
                        raise
                    _apply_synth(ab, pn, ct, vu, fch)
    finally:
        run_primary = sys.exception()
        cleanup_primary: tuple[BaseException, Any] | None = None
        try:
            if play:
                # B086 / B120 / B123: never leave depth>0 or Pulse stuck muted after TTS
                try:
                    mute_repair = repair_tts_mute_after_play(
                        mute_was_enabled=bool(do_mute),
                        mute_applied=mute_applied,
                        was_muted_before=mute_was_muted,
                    )
                except BaseException as exc:
                    mute_repair = None
                    if run_primary is None:
                        cleanup_primary = (exc, exc.__traceback__)
        finally:
            if synth_pool is not None:
                try:
                    synth_pool.arm_terminal_exit()
                except BaseException as exc:
                    if run_primary is None and cleanup_primary is None:
                        cleanup_primary = (exc, exc.__traceback__)
        if cleanup_primary is not None:
            exc, traceback = cleanup_primary
            raise exc.with_traceback(traceback)

    latency_ms = int(1000 * (time.monotonic() - t0))
    out_path = None
    if out and audio_parts:
        # Single-chunk: write as before. Multi: write first part only (formats
        # may not concatenate cleanly); full speech was already played if play.
        out_path = str(write_wav(out, audio_parts[0]))

    store.record_tts(
        text=full_text,
        provider=provider_name,
        voice=used_voice,
        audio_ms=play_ms,
        latency_ms=latency_ms,
        ok=True,
        meta={
            # dashboard TTS audit trail (B067): what was actually spoken
            "text_preview": full_text[:160],
            "from_cache": from_cache,
            "conference": hold_meta,
            "media_duck": duck_meta,
            "listen_defer": defer_meta,
            "chunked": chunked,
            "chunks": len(chunks),
        },
    )
    result: dict[str, Any] = {
        "ok": True,
        "provider": provider_name,
        "voice": used_voice,
        "truncated": truncated,
        "chunked": chunked,
        "chunks": len(chunks),
        "chars": len(full_text),
        "original_chars": original_chars,
        "words": len(full_text.split()),
        "out": out_path,
        "content_type": content_type,
        "audio_ms": play_ms,
        "latency_ms": latency_ms,
        "mic_muted": mute_applied,
        "from_cache": from_cache,
        "media_ducked": bool(duck_meta.get("media_ducked")) if duck_meta else False,
    }
    if hold_meta is not None:
        result["conference"] = hold_meta
    if duck_meta is not None:
        result["media_duck"] = duck_meta
    if defer_meta is not None:
        result["listen_defer"] = defer_meta
    if mute_skipped_capture:
        result["mute_skipped_capture"] = True
    if user_skipped:
        result["user_skipped"] = True
    if mute_repair is not None and mute_repair.get("repaired"):
        result["mute_repaired"] = mute_repair
    if fallback_steps:
        # B177: a silent downgrade must never read as a clean success. Present
        # who was asked, who actually spoke, and every hop in between.
        result["fallback"] = {
            "requested": resolved_provider,
            "spoke": provider_name,
            "steps": list(fallback_steps),
        }
    return result


# EMPTY_STT_NUDGE_TEXT / NO_OPEN_NUDGE_TEXT: imported from answer_window.silence


def _is_no_open_timeout(exc: BaseException) -> bool:
    """True when energy gate never opened (vs empty STT after open)."""
    return _is_no_open_timeout_impl(exc)


def _log_no_open(
    *,
    peak_rms: float | None = None,
    peak_db: float | None = None,
    open_thresh: float | None = None,
    after_tts: bool,
    attempt: int,
    stream_id: str | None,
    phase: str,
    error: str,
    abs_open_db: float | None = None,
) -> None:
    """Structured metric when capture times out before speech opens."""
    _log_no_open_impl(
        peak_rms=peak_rms,
        peak_db=peak_db,
        open_thresh=open_thresh,
        after_tts=after_tts,
        attempt=attempt,
        stream_id=stream_id,
        phase=phase,
        error=error,
        abs_open_db=abs_open_db,
        syslog_fn=syslog,
    )


def _tag_meta_command(result: "ListenResult") -> "ListenResult":
    """Classify a captured (non-cancelled) transcript as a meta-command (B009).

    Meta-commands (repeat/skip/next/status/cancel) spoken during an answer window
    must be honoured, not delivered to the worker agent as a prompt.
    """
    from hark.meta_commands import classify_meta_command

    if not result.cancelled:
        result.meta_command = classify_meta_command(result.text)
    return result


def _log_empty_stt(
    *,
    duration_ms: int,
    peak_rms: float | None,
    peak_db: float | None,
    wait_speech_ms: int,
    after_tts: bool,
    attempt: int,
    provider: str | None,
    stream_id: str | None,
    phase: str,
) -> None:
    """Structured metric for empty STT rate / residual-TTS diagnosis."""
    _log_empty_stt_impl(
        duration_ms=duration_ms,
        peak_rms=peak_rms,
        peak_db=peak_db,
        wait_speech_ms=wait_speech_ms,
        after_tts=after_tts,
        attempt=attempt,
        provider=provider,
        stream_id=stream_id,
        phase=phase,
        syslog_fn=syslog,
    )


def _estimate_wav_audio_ms(wav_bytes: bytes, *, sample_rate: int = 16000) -> int:
    """Best-effort audio duration from a mono 16-bit PCM WAV payload."""
    n = len(wav_bytes or b"")
    if n <= 44 or sample_rate <= 0:
        return 0
    # Standard PCM WAV header is 44 bytes; 2 bytes/sample mono.
    pcm = max(0, n - 44)
    return int(1000 * pcm / (2 * sample_rate))


def _transcribe_logged(
    stt: Any,
    wav_bytes: bytes,
    *,
    stream_id: str | None,
    seq: int,
    mode: str,
    purpose: str = "listen",
    audio_ms: int | None = None,
    sample_rate: int = 16000,
) -> tuple[Any, int]:
    """Call cloud STT and emit stt.request / stt.response on system.jsonl (B038).

    Every upload is logged — including radio interim segments that never hit
    UsageStore.record_stt — so operators can see partial cadence and failures.
    ``seq`` is the 1-based STT call index within the listen stream (correlate
    with ``listen.partial`` / ambient.partial via stream_id + stt_seq).
    Returns ``(Transcript, latency_ms)``.
    """
    provider = getattr(stt, "name", None) or "unknown"
    nbytes = len(wav_bytes or b"")
    if audio_ms is None:
        audio_ms = _estimate_wav_audio_ms(wav_bytes, sample_rate=sample_rate)
    syslog(
        "stt.request",
        component="stt",
        level="info",
        message="STT upload",
        stream_id=stream_id,
        seq=seq,
        provider=provider,
        bytes=nbytes,
        audio_ms=int(audio_ms or 0),
        mode=mode,
        purpose=purpose,
    )
    t0 = time.monotonic()
    try:
        tr = stt.transcribe(wav_bytes)
        latency_ms = int(1000 * (time.monotonic() - t0))
        text = (getattr(tr, "text", None) or "").strip()
        prov = getattr(tr, "provider", None) or provider
        syslog(
            "stt.response",
            component="stt",
            level="info",
            message="STT ok" if text else "STT empty",
            stream_id=stream_id,
            seq=seq,
            provider=prov,
            latency_ms=latency_ms,
            ok=True,
            bytes=nbytes,
            audio_ms=int(audio_ms or 0),
            chars=len(text),
            empty=not bool(text),
            mode=mode,
            purpose=purpose,
            text=text[:200] if text else "",
        )
        return tr, latency_ms
    except Exception as exc:
        latency_ms = int(1000 * (time.monotonic() - t0))
        syslog(
            "stt.response",
            component="stt",
            level="error",
            message=str(exc)[:200] or "STT failed",
            stream_id=stream_id,
            seq=seq,
            provider=provider,
            latency_ms=latency_ms,
            ok=False,
            error=str(exc)[:300],
            bytes=nbytes,
            audio_ms=int(audio_ms or 0),
            mode=mode,
            purpose=purpose,
        )
        raise


# join_radio_stt_segments / monotonic_partial_text / prefer_complete_transcript
# live in hark.answer_window.text_join (owned by RadioSession); imported above.


def _tts_defer_streaming_params(cfg: HarkConfig) -> tuple[bool, float]:
    """Resolve TTS quiet-gate streaming flags at the TTS call seam (P1.M6).

    Prefer active listen registration (policy written at open). Fall back to
    ``policy_from_config(cfg, \"post_wake\")`` only when no active listen is
    registered (should rarely quiet-gate). Bound opens register ``streaming=False``.
    """
    try:
        from hark.listen_control import read_active

        active = read_active()
    except Exception:
        active = None
    if active and active.get("stream_id"):
        streaming_ack = bool(active.get("streaming", False))
        raw_q = active.get("streaming_ack_min_quiet_s")
        if raw_q is not None:
            try:
                min_quiet = float(raw_q)
            except (TypeError, ValueError):
                min_quiet = 2.0
        else:
            from hark.answer_window.policy import policy_from_config

            min_quiet = float(
                policy_from_config(cfg, "post_wake").streaming_ack_min_quiet_s or 2.0
            )
        return streaming_ack, max(0.0, min_quiet)

    # No active listen: HOLD path (streaming=False); min_quiet unused.
    return False, 2.0


def effective_radio_idle_end_s(
    cfg: HarkConfig,
    *,
    streaming: bool | None = None,
    streaming_ack_min_quiet_s: float | None = None,
) -> float:
    """Post-speech quiet before radio auto-finish (B074 + B112 streaming).

    Prefer :func:`hark.answer_window.policy.effective_radio_idle_s` with an
    :class:`AnswerWindowPolicy` built at the call seam. When kwargs are omitted,
    uses **bound_answer** profile via :func:`policy_from_config` (streaming off
    by default — no ambient leak; P1.M6).
    """
    from hark.answer_window.policy import (
        AnswerWindowPolicy,
        effective_radio_idle_s,
        policy_from_config,
    )
    from hark.listen_end import EndMode as _EM

    if streaming is None and streaming_ack_min_quiet_s is None:
        pol = policy_from_config(cfg, "bound_answer")
        return effective_radio_idle_s(
            AnswerWindowPolicy(
                profile=pol.profile,
                end_mode=_EM.RADIO,
                max_listen_s=1.0,
                end_silence_s=float(pol.end_silence_s),
                radio_idle_end_silence_s=float(pol.radio_idle_end_silence_s or 0.0),
                streaming=bool(pol.streaming),
                streaming_ack_min_quiet_s=float(pol.streaming_ack_min_quiet_s or 2.0),
            )
        )

    pol_base = policy_from_config(cfg, "bound_answer")
    pol = AnswerWindowPolicy(
        profile="bound_answer",
        end_mode=_EM.RADIO,
        max_listen_s=1.0,
        end_silence_s=float(getattr(cfg.listen, "end_silence_s", 2.1) or 2.1),
        radio_idle_end_silence_s=float(
            getattr(cfg.listen, "radio_idle_end_silence_s", 0.0) or 0.0
        ),
        streaming=bool(streaming)
        if streaming is not None
        else bool(pol_base.streaming),
        streaming_ack_min_quiet_s=float(
            streaming_ack_min_quiet_s
            if streaming_ack_min_quiet_s is not None
            else (pol_base.streaming_ack_min_quiet_s or 2.0)
        ),
    )
    return effective_radio_idle_s(pol)


def run_listen(
    cfg: HarkConfig,
    *,
    provider: str | None = None,
    tts_provider: str | None = None,
    end_mode: str | None = None,
    max_s: float | None = None,
    last_tts: str | None = None,
    post_tts_guard_s: float | None = None,
    already_armed: bool = False,
    on_partial: Any | None = None,
    stream_id: str | None = None,
    partial_kind: str = "ambient.partial",
    discard_leading_ms: int = 0,
    audio_ok_after: Any | None = None,
    # Prefer profile builders (policy_from_config) for gate knobs.
    profile: AnswerWindowProfile | None = None,
    policy: Any | None = None,
    streaming: bool | None = None,
) -> ListenResult:
    """Capture speech. Radio mode streams partials via on_partial when enabled.

    Prefer ``profile=`` (``bound_answer`` / ``post_wake`` / ``confirm``) or an
    explicit ``policy=`` so gate knobs come from :func:`policy_from_config`.
    Default profile is **bound_answer** (streaming off — does **not** inherit
    ``[ambient].streaming``; P1.M6). Ambient post-wake must pass
    ``profile="post_wake"``. Optional ``streaming=`` overrides the profile default.

    on_partial(event_dict) is called for each non-final radio transcript so the
    orchestrator can start thinking early. Events always set partial=true.

    Empty STT / no-open recovery (silence mode) and overlap pre-arm
    (``audio_ok_after`` / ``discard_leading_ms``) are unchanged.

    ``provider`` is the STT leg. ``tts_provider`` (B187) is only used by the
    window's spoken nudges; it defaults to the configured TTS provider so an
    STT-only pin can never end up driving a synth call.
    """

    # Thin facade (P1.M1.E4 / P1.M6): build policy + deps, open answer window.
    # No cfg.ambient reads in this function — ambient is only consumed inside
    # policy_from_config at the seam.
    from hark.answer_window.deps import AnswerWindowDeps
    from hark.answer_window.open_window import open_answer_window
    from hark.answer_window.policy import AnswerWindowPolicy, policy_from_config

    if policy is not None:
        if not isinstance(policy, AnswerWindowPolicy):
            raise TypeError(
                f"policy must be AnswerWindowPolicy/ListenSessionPolicy, got {type(policy)!r}"
            )
        deps = AnswerWindowDeps(
            cfg=cfg,
            on_partial=on_partial,
            audio_ok_after=audio_ok_after,
        )
        return open_answer_window(policy, deps=deps)

    # Explicit post_tts_guard always wins. Pre-arm (already_armed) used to zero
    # the guard and race mute-unmute / residual TTS into the energy gate.
    if post_tts_guard_s is not None:
        guard = max(0.0, float(post_tts_guard_s))
    else:
        # already_armed or not: still settle briefly for mute unmute / echo residual
        guard = max(0.0, cfg.audio.post_tts_guard_ms / 1000.0)

    effective_profile: AnswerWindowProfile = (
        profile if profile is not None else "bound_answer"
    )

    overrides: dict[str, Any] = {
        "max_listen_s": float(max_s if max_s is not None else cfg.listen.max_listen_s),
        "last_tts": last_tts,
        "post_tts_guard_s": guard,
        "already_armed": bool(already_armed),
        "stream_id": stream_id,
        "partial_kind": partial_kind,
        "discard_leading_ms": int(discard_leading_ms or 0),
        "stt_provider": provider,
    }
    if tts_provider is not None:
        # The window's nudges speak; keep them on the TTS leg (B187).
        overrides["tts_provider"] = tts_provider
    if end_mode is not None:
        overrides["end_mode"] = end_mode
    if streaming is not None:
        overrides["streaming"] = bool(streaming)

    built = policy_from_config(cfg, effective_profile, **overrides)
    deps = AnswerWindowDeps(
        cfg=cfg,
        on_partial=on_partial,
        audio_ok_after=audio_ok_after,
    )
    return open_answer_window(built, deps=deps)


# SpeakThenListen (P1.M4): half-duplex handoff + confirm live in
# hark.speak_then_listen; thin re-exports keep hark.speech.* import paths stable.
from hark.speak_then_listen import run_ask as run_ask  # noqa: E402
from hark.speak_then_listen import speak_and_listen as speak_and_listen  # noqa: E402
