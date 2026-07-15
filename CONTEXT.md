# Hark

Voice bridge between a human operator and coding agents running in Herdr panes.
Hark captures spoken answers, speaks agent questions, and delivers only through
library-enforced safety checks.

## Language

### Operating modes

**Mode A**:
The primary product shape: an external supervisory agent (orchestrator) uses the
`hark` CLI/library for watch, listen, TTS, and bound delivery, while the agent
retains judgment (menus, false-done, summaries).
_Avoid_: handsfree daemon, autonomous loop, Mode B as default

**Mode B**:
Optional always-on daemon dialogue (`harkd`) without an external orchestrator.
Experimental and out of Mode A’s critical path.
_Avoid_: treating harkd as required for v1

### Events and delivery

**HEP** (Hark Event Protocol):
Normalized event records (`hark.event.v1`) emitted for agent state, ambient
prompts, partials, and related surfaces so monitors and tools share one schema.
_Avoid_: raw Herdr wire as the product event model; ad-hoc JSON per tool

**Bound delivery**:
Sending text or keys to a Herdr pane only for a previously registered event,
after fingerprint, pane liveness, revision, and compatible-status checks.
_Avoid_: free-form “reply to session”; guessing the target pane from the LLM

**Answerability**:
Pure live-compatible gate (`hark.answerability`) for bound delivery: status ×
HEP kind × pane heuristics → deliver or refuse (shared by answer, queue, dashboard).
_Avoid_: scattering `status==blocked` checks; treating done/idle as always unanswerable

**Bound event**:
A pending interaction registered from HEP (typically blocked or needs-input)
that carries the delivery target and question fingerprint for later answer.
_Avoid_: unbound chat message; generic notification

**False-done**:
Herdr reports done/idle while the pane still shows a menu or ask; surfaced as
`agent.needs_input` and treated as still needing an operator turn. Bound answer
may still deliver when Answerability re-check finds menu + fingerprint match.
_Avoid_: trusting status=done alone; “completed” without pane judgment; refusing
`hark answer` solely because status is not `blocked`

**Pane Understanding**:
Deep module (`hark.pane_understanding`) that turns agent status + pane text into
HEP facts (blocked, needs_input, busy-subagent, question_changed). Watch only
does I/O. Former informal name: EdgeTracker.
_Avoid_: embedding false-done policy in `watch.py`; calling EdgeTracker the
product name for the deep module

### Listening and speech

**Answer Window**:
A single armed listen capture after Hark asks or after ambient activation—cloud
STT runs only inside this window (or the post-wake path that opens one).
_Avoid_: continuous cloud ambient transcription; “always listening” cloud STT

**Post-wake listen**:
The answer window opened after a local wake phrase hits, for the operator’s
spoken prompt body (often ambient.partial → ambient.prompt).
_Avoid_: conflating wake detection itself with cloud STT

**Radio end mode**:
Answer-window mode that streams interim transcripts and finalizes on end/cancel
phrases, soft-end heuristics, agent listen-end, idle quiet, or max duration—not
on ordinary short silence alone.
_Avoid_: walkie-talkie; PTT (push-to-talk) as the product name

**Silence end mode**:
Answer-window mode that finalizes when energy/endpointing decides the utterance
ended (end_silence / optional Smart Turn).
_Avoid_: VAD-only as the product term for the whole mode

**Soft end**:
Conservative, utterance-final phrase heuristics (e.g. “over”, “that’s all”) that
may finish a radio window without the orchestrator calling listen-end.
_Avoid_: hard end phrases; mid-clause false triggers treated as finishes

**Partial** (radio partial):
An interim HEP transcript for an open radio answer window (`partial=true`); not
a final prompt and not a delivery authorization.
_Avoid_: final prompt; complete answer

**SpeakThenListen** (half-duplex handoff):
The deep module that owns TTS → arm → listen transitions (and optional confirm
turns): near-end arm, half-duplex vs overlap pre-arm discard, and attaching
`tts_info` to listen errors. Answer Window is listen-only; this module opens it
via profiles after speaking.
_Avoid_: barge-in as default; scattering mute/duck/overlap thread state across CLI call sites

**Overlap pre-arm**:
Optional early capture near TTS end (`audio.overlap_prearm`) while discarding
frames until TTS finishes plus `overlap_discard_ms` (ADR-009 no barge-in).
_Avoid_: true full-duplex; treating discarded TTS tail as operator speech

**State Feed Follower**:
Deep multi-path JSONL follower for state files (watch, ambient, …) with partial
buffer, rotation, and composite cursor; monitor and dashboard are adapters.
_Avoid_: a second hand-rolled tail loop per consumer; double monitor_profile

**present_for_monitor** (Mode-A compact):
Single presentation function at the harness-monitor emit edge (agent + ambient +
tts compact). Prefer full events on disk.
_Avoid_: compacting at write *and* read for the same pipeline

### Orchestration

**Orchestrator** (handsfree agent):
The external coding agent that runs the Mode A loop—judgment, tool calls, and
when to speak—while the library owns routing safety.
_Avoid_: “the LLM inside hark”; mixing library safety with agent judgment

**Herdr**:
The multi-pane agent host Hark watches and delivers into (local socket and
optional remote tunnel).
_Avoid_: tmux as the product name; equating Herdr with Hark

## See also

- Domain decisions: [docs/DECISIONS.md](docs/DECISIONS.md)
- Event schema: [docs/PROTOCOL.md](docs/PROTOCOL.md)
- Product goals: [docs/PRODUCT.md](docs/PRODUCT.md)
- Answer Window deepen design: [docs/plans/P1-M1-answer-window.md](docs/plans/P1-M1-answer-window.md)
- Bound Answerability design: [docs/plans/P1-M2-answerability.md](docs/plans/P1-M2-answerability.md)
- SpeakThenListen deepen design: [docs/plans/P1-M4-speak-then-listen.md](docs/plans/P1-M4-speak-then-listen.md)
- State Feed Follower design: [docs/plans/P1-M5-state-feed-follower.md](docs/plans/P1-M5-state-feed-follower.md)
