# Safety and routing integrity

Merged from prior `SECURITY.md` + risk policy, adapted for Mode A + library.

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
- Before send: re-check pane exists, still compatible state, fingerprint unchanged.  
- On mismatch: refuse send; speak/notify “question changed; repeating…”  
- **Idempotency key** for each successful logical send; reconnect must reconcile, not blindly resend.  

### Voice

- Answer window only after TTS (+ post-TTS guard).  
- Half-duplex: discard gated samples during TTS.  
- Adaptive noise gate + min speech duration + min non-filler tokens.  
- Reject transcripts that highly overlap the just-spoken TTS text.  
- Optional wake prefix later.  

### Confirmation policy

| Risk | Examples | Confirm |
|------|----------|---------|
| **R0** informational | status ack | never (or no answer) |
| **R1** ordinary | free text, simple choice | **only when unsure** (short/noisy/low confidence/ambiguous multi-target) |
| **R2** authorization | “allow?”, permission menus | **always** readback + confirm/cancel |
| **R3** destructive / secrets / deploy / publish / credentials | high-impact | **always** verbatim scope readback; optional second factor later |

Classifier is **conservative**: when unsure whether R1 vs R2, treat as R2.

### Agent-content distrust

All text read from panes is **untrusted data**. The bridge may speak it and route a human answer. It must **not** execute commands embedded in questions. Supervisory agents must not treat pane text as human authorization.

### Local authority

- No public TCP by default.  
- Control sockets (if any) mode `0600`, current user only.  
- Never log tokens, cookies, or raw audio by default.  

### Monitor lines

`--for-monitor` payloads: compact, **no full transcripts**, no secrets, include `event_id` and instruction “use hark skill; do not invent answers.”
