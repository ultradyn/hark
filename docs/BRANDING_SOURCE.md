# Hark

> **When your agents need a word.**

**Hark, the herald agents sing:  
“Input, please, O human king!”  
Blocked in Herdr, questions rise;  
Hark relays your voice replies.  
Mic awake and speakers clear,  
Answer agents far and near.  
Code resumes and workers spring—  
“Send the prompt and ship the thing.”**

Hark is a lightweight voice bridge for agent sessions running in [Herdr](https://herdr.dev/).

When an agent becomes blocked, Hark detects the state change, reads the question aloud, listens for your response, transcribes it, verifies the intended target, and sends the answer back to the agent. This keeps long-running agent workflows moving while you are away from the keyboard or busy doing hands-on work.

## Why Hark?

Agent sessions often stop for small but necessary decisions:

- “Should I update the lock file?”
- “Which implementation should I use?”
- “May I run this destructive command?”
- “The test failed in an unexpected way. How should I proceed?”

Hark turns those interruptions into short voice conversations instead of requiring you to return to the terminal.

```text
Agent becomes blocked
        ↓
Hark reads the question aloud
        ↓
You answer by voice
        ↓
Hark transcribes and confirms the response
        ↓
The answer is sent to the correct agent
        ↓
Work continues
```

## Design goals

- Fast, low-overhead, and suitable for always-on use
- Event-driven integration with Herdr protocol v0.7.1 or newer
- Reliable targeting across multiple concurrent agents
- Pluggable speech-to-text and text-to-speech providers
- No local speech model required
- Explicit confirmation for destructive or security-sensitive actions
- Recoverable behavior across disconnects, restarts, and stale questions

## Taglines

Primary:

> **Hark — when your agents need a word.**

Alternatives:

- **Blocked agents call. Hark answers.**
- **Your agents have questions. Hark lets them ask.**
- **Human-in-the-loop, without hands on the keyboard.**
- **Because “needs input” should not mean “wait until you get back.”**
- **Speak softly and carry a large agent fleet.**
- **The voice of human supervision.**

## Short description

Use this for package metadata, repository descriptions, and directory listings:

> A lightweight voice bridge that lets blocked Herdr agents ask questions aloud and receive spoken answers.

## Longer description

> Hark monitors agent sessions running in Herdr and reacts when an agent becomes blocked or needs human input. It reads the request aloud, captures a bounded spoken response, transcribes it through a configurable cloud provider, verifies that the target session is still waiting for the same answer, and safely delivers the response. Hark is designed for low-resource, always-on operation and for supervising multiple agent sessions while away from the keyboard.

## Command name

The primary command is:

```bash
hark
```

Suggested command structure:

```bash
hark start
hark status
hark queue
hark answer
hark mute
hark unmute
hark devices
hark providers
hark doctor
```

The background service may be named `harkd`.

## Naming conventions

| Component | Suggested name |
|---|---|
| Project | Hark |
| CLI | `hark` |
| Daemon | `harkd` |
| Configuration directory | `~/.config/hark/` |
| State directory | `~/.local/state/hark/` |
| Environment-variable prefix | `HARK_` |
| Agent skill | `hark` or `herdr-voice` |
| Herdr integration module | `hark-herdr` |
| Internal event protocol | Hark Event Protocol (`HEP`) |

## A more irreverent alternate verse

**Hark, the herald agents sing:  
“Boss, we need one tiny thing.”  
Build is green but plans are stalled;  
One small question blocks them all.  
From the workshop, shed, or stair,  
Speak your answer through the air.  
Hark transcribes your wise decree—  
Then the agents work till three.**

---

The verse is intentionally playful; the software should not be. Hark's routing, confirmation, and delivery behavior must remain deterministic, inspectable, and safe.
