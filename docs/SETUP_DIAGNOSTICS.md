# Setup and speech diagnostics

Run `hark doctor --plan` for an ordered list of repairs, commands, and documentation links. The same list appears as `next_actions` in `hark doctor --json`.

Doctor reports STT and TTS separately. Credential discovery establishes that configuration is present; it does not verify token validity, audio scopes, quota, or a working synthesis/transcription request. Cloud audio access is labelled **untested**. In particular, Codex credentials can lack audio scopes even while other API calls work. Recent failures appear as bounded metadata, without exposing request bodies, recordings, or provider response text. A later logged success clears that failure hint.

Disabled providers, unsupported STT choices such as MiniMax/Anthropic, and missing MiniMax TTS consent cannot make a leg appear configured. A Custom STT configuration says nothing about TTS availability, and vice versa. Configuration warnings report unsupported STT selections before recording begins.

MiniMax consent is explicit. Run `hark setup --minimax-ok` to save `tts.minimax_ok = true`, or set `HARK_TTS_MINIMAX_OK=1` for a process. Setup preserves the existing TTS provider. Selecting a provider or applying a session profile does not grant consent.

The setup completion marker records saved interview answers. It is not proof of runtime audio readiness. Setup returns provider exit code 4 when the saved speech policy has no configured usable STT or TTS candidate, and keeps the answers so they can be repaired. Audio access remains untested even when both legs are configured.

Before starting a missing ambient worker, Hark checks the wake backend package and model path. A failed preflight starts no new workers and prints the missing prerequisite. Already running roles remain untouched; watch-only startup does not require a wake model. Doctor also shows whether conference hold is active and what matched, so silent held TTS has an explanation.
