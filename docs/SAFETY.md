# Safety and routing integrity

Merged from prior `SECURITY.md` + risk policy, adapted for handsfree + library.

## Assets

- Correct mapping from spoken answer → Herdr pane  
- Transcript integrity  
- API / OAuth credentials  
- Microphone audio privacy  
- Prevention of unintended destructive approvals  

## Threats

1. Background speech mistaken for the operator  
2. TTS loops into STT (echo self-answer)  
3. Agent question changes while the human speaks  
4. Pane moves/closes; stale IDs  
5. Disconnect → duplicate delivery  
6. Pane text tricks a supervisory LLM into acting  
7. Secrets in logs or Monitor lines  
8. Ambient TV / other people during answer window  

## Controls (normative for `hark` library)

### Routing

- Targets are **opaque** session + pane (+ terminal) IDs — never “the focused pane.”  
- Prefer **bound delivery**: `event_id` + `expected.pane_revision` + `question_fingerprint`.  
- Before send: re-check pane exists, still **compatible state**, fingerprint unchanged.  
- On mismatch: refuse send; speak/notify “question changed; repeating…”  
- **Idempotency key** for each successful logical send; reconnect must reconcile, not blindly resend.  

**Compatible state** (implemented by `hark.answerability`, used by `hark answer`,
`hark queue` live filter, and dashboard `/answer`):

| Compatible when | Not compatible |
|-----------------|----------------|
| Herdr live status is **`blocked`**, and fingerprint (+ revision when set) match | Status is **`working`** (or unknown) |
| Bound HEP kind is **`agent.needs_input`** (false-done / false-idle), Herdr status is **idle-like** (`done` / `idle` / `completed` / `complete`), fingerprint matches, and the live pane **still looks like a pending menu/ask** | Idle-like with an **`agent.blocked`** bind only (no needs_input re-bind) |
| | Pane gone, stale revision, or fingerprint mismatch |

False-done: watch may emit `agent.needs_input` while Herdr reports done/idle but a
menu remains. Bound answer **must still deliver** when the live re-check agrees
(menu + fingerprint). Empty idle chrome (e.g. bare Claude `❯`) is **not**
compatible — refuse. Write failures stay **`uncertain`** (never blind-retry).
See [plans/P1-M2-answerability.md](plans/P1-M2-answerability.md).

### Voice

- Answer window only after TTS (+ post-TTS guard).  
- Half-duplex: discard gated samples during TTS.  
- Adaptive noise gate + min speech duration + min non-filler tokens.  
- Reject transcripts that highly overlap the just-spoken TTS text.  
- TTS playback posts a desktop notification with a **Skip** action by default
  (`[tts] notify_skip`); the operator can stop playback mid-speech.  
- Optional wake prefix later.  

### Confirmation policy

| Risk | Examples | Confirm |
|------|----------|---------|
| **R0** informational | status ack | never (or no answer) |
| **R1** ordinary | free text, simple choice | **only when unsure** (short/noisy/low confidence/ambiguous multi-target) |
| **R2** authorization | “allow?”, permission menus | **always** readback + confirm/cancel by configured policy |
| **R3** destructive / secrets / deploy / publish / credentials | high-impact | **always** verbatim scope readback by configured policy; optional second factor later |

Classifier is **conservative**: when unsure whether R1 vs R2, treat as R2.

The table describes the default `auto` policy. Configured
`[confirm].mode = "always"` confirms every risk class; configured `"never"`
suppresses R0/R1 confirmation but does not bypass R2/R3. An explicit
`hark ask --confirm never` or `run_ask(..., confirm="never")` is a one-call
operator override and skips the second confirmation for every risk class,
including R2/R3. Omitting the per-call option applies configured policy; the
explicit override does not persist or weaken later asks.

Spoken confirm replies are classified **fail-closed**
(`src/hark/confirm_lexicon.py`, B151/B157/B159): the classifier returns
`yes` / `no` / `unclear`, and only an unambiguous `yes` authorizes.
**Negations win over affirmatives** — a transcript containing a negation
(`no`, `don't`, `never mind`, `negative`, …) is never an approval.
Defer/condition/hedge markers (`but`, `wait`, `hold on`, `if`, `unless`,
`maybe`, `later`, …) block immediate approval, so multi-clause replies like
"yes but wait" fail closed to `unclear`. Unicode Format (Cf) and Mark (M*)
code points are stripped before matching, and quoted negative contractions
plus a documented set of apostrophe variants are normalized, so zero-width or
lookalike obfuscation cannot smuggle a fake "yes" past the readback.

### Agent-content distrust

All text read from panes is **untrusted data**. The bridge may speak it and route a human answer. It must **not** execute commands embedded in questions. Supervisory agents must not treat pane text as human authorization.

### Local authority

- No public TCP by default.  
- Control sockets (if any) mode `0600`, current user only.  
- Never log tokens, cookies, or raw audio by default.  

### Monitor lines

`--for-monitor` payloads: compact, **no full transcripts**, no secrets, include `event_id` and instruction “use hark skill; do not invent answers.”
