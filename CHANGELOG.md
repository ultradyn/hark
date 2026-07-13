# Changelog

All notable changes to **Hark** and the **`@ultradyn/hark`** npm skills package.

Format: sections headed `## X.Y.Z` match git tags `vX.Y.Z` and the npm package version in `packages/ultradyn-hark/package.json`.

## Unreleased

- Site typography (B042): replace generic system stacks with curated webfonts —
  **Fraunces** (display), **Source Sans 3** (body), **JetBrains Mono** (mono) —
  loaded with `preconnect` + `display=swap` on the marketing site and OG card.
  See `site/README.md` and `site/css/tokens.css`.
- Site OG card (B030 follow-up): social preview is designed as `site/og-image.html`
  and rendered to `site/og.png` via `~/.llm-general/skills/` (`og-social-previews` +
  `headless-browser-screenshots` + visual review). See `site/README.md`.
- Config.toml live-reload (B036): ambient Mode A watches the active config path
  (`HARK_CONFIG` / `~/.config/hark/config.toml`) by mtime poll + debounce and applies
  the same `apply_config_reload` path as SIGHUP (phrases, names, `listen.end_mode`,
  `surface_timeouts`, etc.). Emits `ambient.reloaded` with `source` (`config_watch`
  or `sighup`). Defaults: `ambient.config_watch = true`, `config_watch_poll_ms = 1000`,
  `config_watch_debounce_ms = 400`; env `HARK_CONFIG_WATCH=0|1`. See
  `docs/CUSTOM_WAKE.md` (file-watch vs SIGHUP vs restart).
- Radio partial cadence (B037): radio mode uses a shorter, radio-only
  `listen.radio_partial_silence_s` (default **0.6 s**) to cut segments for
  interim STT / `ambient.partial` updates. Does **not** finalize the turn
  (end phrases / agent `listen-end` still required) and does **not** change
  silence-mode `end_silence_s`. Legacy `radio_end_silence_s` kept for config BC.
  See `docs/AUDIO_DESIGN.md`.
- STT request timeline (B038): every cloud STT upload (silence + radio partials) emits `stt.request` / `stt.response` on `system.jsonl` with `stream_id`, `seq`, `provider`, `bytes`/`audio_ms`, `latency_ms`, `ok`/`error`. Radio `listen.partial` / ambient.partial include `stt_seq` for correlation.
- Radio soft finalize (B039): soft end phrases default **on** for radio dogfood.
  Bare utterance-final `send it` / `send that` finalize; bare `over` finalizes only
  when sentence-final (sole utterance or after `.`/`!`/`?`) — not “turn it over” /
  “over the weekend”. Product phrases (`okay hark send`, `hark over`, …) unchanged.
  Disable with `listen.soft_end_phrases_enabled = false` or
  `HARK_SOFT_END_PHRASES_ENABLED=0`. Monitor compact `ambient.partial` lines already
  include `text_len`. Partial cadence density remains B037.
- Ambient timeout heartbeat (B033): continuous Mode A still cycles on
  `ambient.timeout_s` (default 300s), but emission of `ambient.timeout` to
  monitor NDJSON/syslog is gated by `ambient.surface_timeouts` (default **on**).
  Set `surface_timeouts = false` (alias `emit_timeout_events`) to quiet long-running
  idle cycles; leave on as a heartbeat when watching provider cache / dogfood.
  `timeout_s = 0` means wait indefinitely (no timeout cycle). See
  `docs/AUDIO_DESIGN.md`.
- Post-wake listen gate soften + no-open recovery (B031): energy-gate absolute open
  floor default softened from -38 dB to **`-48` dB** (`listen.abs_open_db`) so quiet
  close-talk speech after ambient wake opens the gate (dogfood peak≈-45 never opened).
  Configurable `open_margin_db`, `initial_timeout_s`, `no_open_retry` / `no_open_nudge`
  re-listen when the gate never opens (not only empty STT after open). Ambient post-wake
  knobs: `post_wake_lead_in_ms`, `post_wake_arm_cue`, `post_wake_abs_open_db`,
  `post_wake_timeout_s` (default 15s for faster nudge), `post_wake_no_open_nudge` +
  TTS *"I heard the wake but not your prompt."*; clear `ambient.error` / `speech.no_open`
  metrics.
- Self-detection (B029): when `hark watch` runs inside a herdr pane it now
  detects its own pane (via `HERDR_ENV`/`HERDR_PANE_ID`/`HERDR_SOCKET_PATH`) and
  excludes it from watch — no self events, no self pane reads (prevents feedback
  loops). Excluded pane is surfaced on `watch.armed` as `self_target`; escape
  hatch `HARK_WATCH_INCLUDE_SELF=1` disables exclusion.
- Pluggable silence-mode endpointing (B007): `listen.endpoint_strategy` selects the
  turn-end detector. Default `"energy"` reduces exactly to the previous fixed
  `end_silence_s` gate; optional `"smart_turn"` consults a Smart Turn v3 model
  (optional `[smart-turn]` extra + model) to finish early or hold through
  mid-thought pauses, with transparent fallback to the energy gate. New
  `endpoint_probe_silence_s`, `endpoint_max_silence_s`, `smart_turn_model_path`,
  `smart_turn_threshold` config; env `HARK_LISTEN_ENDPOINT_STRATEGY`,
  `HARK_SMART_TURN_MODEL`. See `docs/ENDPOINTING.md`.
- Multi-session voice queue UX (B009): `hark queue --announce` speaks the waiting-agent count
  by TTS when more than one is waiting (JSON adds `count` / `announcement` / distinct `targets`);
  queue now counts by distinct session/pane and excludes delivered/skipped/rejected/invalidated
  events. New spoken meta-command lexicon (`repeat` / `skip` / `next` / `status` / `cancel`) —
  `hark tts --listen`, `hark listen`, and `hark ask` return a `meta_command` field for
  whole-utterance control phrases, and `hark ask` short-circuits (no confirm/send) on one.
- Optional TTS/listen overlap pre-arm (`audio.overlap_prearm`, `overlap_discard_ms`): start
  capture near TTS end while discarding audio until TTS ends + residual (B004). Half-duplex
  remains the default.

## 0.1.4

- Fix npm OIDC publish: `package.json` `repository.url` must match live GitHub remote (`clankercode/hark` until transfer).
- release.yml verifies repository ↔ Actions repo before publish.
- Install picker: skills | bash | npm | pnpm | bun (default skills).

## 0.1.3

- Skills discovery: monorepo `skills/` symlinks for `npx skills`; internal skill shim.
- Harden package skill sync (`HARK_SYNC_REQUIRED`, frontmatter checks).
- CI: `npm-package.yml` validates pack + skills list on skill/package changes.
- Near-miss wake monitor events (B019), custom wake SIGHUP reload (B020).
- Homepage install picker bash/npm/pnpm/bun (B023).
- Repo transfer prep tooling (B024).

## 0.1.2

- Automated release via GitHub Actions `release.yml` (OIDC trusted publishing, no NPM_TOKEN).
- Package validation gate (skills + `npm pack --dry-run`) before publish.
- Site install picker (bash/npm/pnpm/bun) on hark.xk.io (docs surface; not in tarball).

## 0.1.1

- Polish npm package README for npmjs.com (skills vs CLI, install via npm/pnpm/bun, `install.sh`).
- Release pipeline: GitHub Actions `release.yml` with OIDC trusted publishing (no `NPM_TOKEN`).

## 0.1.0

- Initial `@ultradyn/hark` skills package (`hark` + `handsfree` skills, `hark-skill` bin).
- `npx skills add clankercode/hark` documented as the recommended skill install path.
