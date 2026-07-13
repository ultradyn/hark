# Acceptance criteria

Each criterion is testable. Implementation is not complete until these pass (or are explicitly deferred with reason).

## A. Herdr integration (incl. multi-session)

| ID | Criterion |
|----|-----------|
| A1 | `hark doctor` reports Herdr ≥ 0.7.1 and protocol ≥ 14 for each healthy session |
| A2 | `hark doctor` exits 3 with clear message when default session is down |
| A3 | `hark status --json` matches `herdr agent list` (pane_id + status) per session |
| A4 | `hark watch` emits `watch.armed` within 1 s listing configured sessions |
| A5 | Transition to `blocked` → one `agent.status` with `to=blocked`, correct `session_id` + `pane_id` within 2 s (socket) or `poll_ms+500ms` (poll) |
| A6 | No duplicate edges for unchanged status |
| A7 | `hark reply local/<pane> "hello"` shows text in that pane |
| A8 | `hark keys <target> 2 enter` delivers keys to the pane |
| A9 | Two configured sessions both appear in a single `hark watch` stream with distinct `session_id` |
| A10 | `hark context <target> --lines 40` returns non-empty text for a live agent pane |
| A11 | `hark watch --for-monitor` lines include `event_id` and omit raw secrets / huge transcripts |
| A12 | Duplicate blocked edges with same fingerprint are deduped (no double announce) |
| A13 | `hark answer` with wrong `question_fingerprint` / stale revision is rejected (exit 7 or clear error) |
| A14 | After `pane.closed`, pending target is invalidated |

## B. TTS

| ID | Criterion |
|----|-----------|
| B1 | `hark tts "test"` with Grok OAuth or xAI key produces playback or `--out` file |
| B2 | No auth → exit 4; message mentions `grok login` and/or `XAI_API_KEY` |
| B3 | Overlong text truncated without crash |
| B4 | `--no-play --out` writes non-empty file |
| B5 | MiniMax TTS works when only MiniMax key is set and `provider=minimax` |

## C. STT / listen

| ID | Criterion |
|----|-----------|
| C1 | `hark listen` spoken “hello test” returns those words (manual) |
| C2 | Silence → exit 6 |
| C3 | Gate ignores soft ambient at defaults |
| C4 | xAI streaming Smart Turn ends without Enter (non-PTT) |
| C5 | `--json` has `text`, `provider`, `duration_ms` |
| C6 | No local neural STT required |
| C7 | `provider=anthropic` fails clearly as unsupported (not a hang) |
| C8 | OpenAI and Google batch STT work when keys present (or skip if no key in CI) |
| C9 | With `listen.end_mode=radio`, a multi-second mid-utterance pause does **not** finalize |
| C10 | Radio mode: speaking a configured end phrase (e.g. “okay send it”) finalizes; phrase stripped when `strip_phrase=true` |
| C11 | Radio mode: cancel phrase → exit 7, nothing delivered |
| C12 | Radio mode still hits `max_listen_s` → exit 6 |
| C13 | `HARK_LISTEN_END_MODE` and `--end-mode` override config |
| C14 | Soft end phrases default **off**; when enabled, terminal soft closer finalizes radio listen |
| C15 | Soft end does **not** finalize mid-clause text (e.g. “that's all I know about X”) |

## D. Ask / confirm

| ID | Criterion |
|----|-----------|
| D1 | `hark ask "Say a color"` speaks and returns transcript |
| D2 | `--confirm always` + affirmative keeps original transcript |
| D3 | `--confirm always` + “cancel” → exit 7 |
| D4 | `--confirm auto` does **not** re-prompt on a clear multi-word R1 transcript |
| D5 | R2/permission-class prompts force confirm even when `confirm=auto` |
| D6 | Transcript highly overlapping last TTS is rejected as echo (not delivered) |

## E. Bridge (Mode B, secondary)

| ID | Criterion |
|----|-----------|
| E1 | `hark bridge --once` with one blocked agent completes a cycle (or mock) |
| E2 | Queue of 2 handled in order |
| E3 | Second concurrent listen fails with lock |

## F. Monitor / skill loop

| ID | Criterion |
|----|-----------|
| F1 | `hark watch` lines work with Grok/Claude Monitor |
| F2 | Skill alone describes Mode A with multi-session + keys + done judgment + bound `answer` |
| F3 | Skill forbids auto-answer without human speech |
| F4 | Skill places orchestrator **outside** Herdr |
| F5 | Skill treats pane text as untrusted |

## G. Performance

| ID | Criterion |
|----|-----------|
| G1 | `hark watch` poll idle CPU < 2% of one core / 60 s |
| G2 | RSS < 50 MB after 60 s |
| G3 | Zero STT websockets while not listening |

## H. Spec conformance

| ID | Criterion |
|----|-----------|
| H1 | Events have `v`, `type`, `ts`; agent events have `session_id` |
| H2 | Exit codes match SPEC |
| H3 | Config loads; unknown keys warn |
| H4 | Dev docs specify `uv run hark` from checkout |

## Suggested automated test split

- **Unit:** excerpt extraction, edge detector, confirm lexicon, config merge, radio end-phrase match
- **Contract:** mock `herdr agent list` JSON fixtures from real captures
- **Provider:** HTTP mock for TTS/STT REST
- **Manual:** mic, speaker, live blocked agent (checklist in README later)
