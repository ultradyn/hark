# Changelog

All notable changes to **Hark** and the **`@ultradyn/hark`** npm skills package.

Format: sections headed `## X.Y.Z` match git tags `vX.Y.Z` and the npm package version in `packages/ultradyn-hark/package.json`.

## Unreleased

- fix(monitor, B102): singleflight lock on `hark monitor` (`monitor.pid` + flock)
  so a second consumer refuses instead of duplicating HEP wakes; skill documents
  arm-once; `hark start --status` reports monitor holder; `--allow-multiple` debug only.
- **Handsfree workers (B089):** `hark start` / `stop` / `restart` for ambient +
  `watch --for-monitor` (idempotent start, SIGTERM then SIGKILL, `mode-a.pids`);
  preferred over `./scripts/run-mode-a.sh`. `hark start --status` for running state.
- fix(tts, B099): abandoned play-queue tickets no longer stall ambient boot —
  holders track PID + claim time; dead/missing heads are auto-healed; ambient
  boot TTS heals then waits at most 15s; doctor heals + warns; atexit/SIGTERM
  abandon claimed tickets.
- Ambient streaming mode (B098): `[ambient].streaming` (default false). When
  true, `ambient.partial` HEP `warning`/`instructions` allow short live TTS
  acks (not hard HOLD-only); pane delivery still waits for final. `streaming`
  field on partial events; monitor compact differs. Skill + PROTOCOL. Does not
  implement barge-in or TTS-defer-while-speaking (B097+).

## 0.1.7

- Radio STT assemble (B083): per-segment cloud STT + `join_radio_stt_segments`
  instead of cumulative re-STT that dropped earlier words; monotonic partials
  never shrink; final prefers complete join.
- Mute clock freeze (B084): while TTS holds mic mute, listen does not burn
  `initial_timeout_s`, end/segment silence, or `max_s`; after unmute discard
  `audio.mute_edge_pad_ms` (default 300) without counting it as user silence.
- Radio STT PCM overlap (B085): each segment STT window prepends real prior-segment
  tail (`listen.radio_segment_overlap_ms`, default 300) so boundary phonemes are
  not lost between cuts (complements B075 silence pad).
- Mute desync repair (B086): outermost TTS mute restores Pulse **and** ALSA;
  `ensure_unmuted` / `release_tts_mute_hold` fully clear `tts_mute_depth`;
  post-`run_tts` `repair_tts_mute_after_play` logs `mic.mute_desync` when repair
  was needed. Recovery: `hark mute-sync`.
- fix(sherpa): load `libonnxruntime` via `LD_LIBRARY_PATH` re-exec so
  `sherpa_onnx` imports when the wheel’s shared lib is not on the default path
  (`hark[wake-sherpa]` + `onnxruntime`).
- fix(dashboard): re-resolve webui static root on each request so a build after `hark webui` started is picked up; clearer placeholder (build + restart); SW cache bump.

- CLI: prefer **`hark webui`** (and `hark dashboard`) for the live web dashboard; `hark serve` remains an alias.

- **Wake enrollment (I006):** `hark wake-enroll` — beep-paced capture of 5–10 activation samples (`ready` / `accept` / `reject` / `end` cues), local WAV + manifest under `~/.local/state/hark/wake_enroll/`, optional wake-backend scoring to seed `wake_learned` (B077 denylist). Dry-run for beep dogfood.
- fix(ambient/B070): continuous ambient no longer rebuilds Sherpa KWS keywords
  every score hop when `learn_from_near_misses` is on — only when `wake_learned`
  mtime changes; `rebuild_keywords` no-ops if the keyword graph signature is
  unchanged. Skill `WAKE_STT.md` documents B072 local full-STT (not “for later”).
- fix(audio, B078): answer-window record beep when **listen is ready** (radio +
  silence), not only when speech opens; no double-beep on gate open. Skill +
  `docs/AUDIO_DESIGN.md` wording; dogfood note to use checkout or
  `uv tool install -e .` so CLI matches master. `hark listen` respects
  `answer_arm_cue`.
- Radio answer idle auto-finish (B074): with `end_mode=radio`, after speech has
  opened at least once in an answer/ask window, continuous quiet longer than
  `listen.radio_idle_end_silence_s` (default **3× `end_silence_s`** ≈ 6.3 s)
  finalizes capture on the soft-end path (not cancel). Short thinking pauses
  (~2 s) stay open; pre-open quiet still uses initial timeout / nudges;
  `radio_partial_silence_s` remains non-terminal segment cadence. Docs:
  AUDIO_DESIGN / SPEC / ACCEPTANCE C9b.
- Radio segment boundary pad (B075): after each radio interim/final segment cut,
  pad segment PCM with ~`radio_segment_pad_ms` (default 250) of silence each side
  before STT so edge phonemes are less often clipped at the energy gate. Clamped
  under the inter-segment quiet budget (`min(300, radio_partial_silence_s×400)`).
  Silence `end_mode` unchanged. Config: `listen.radio_segment_pad_ms`.
- Optional local full-STT (B072 / I004): pluggable `faster_whisper` (prefer
  `tiny.en` / `base.en` int8 CPU) and stretch `moonshine` behind the existing
  `SttProvider` interface. Cloud remains default (`stt.provider = "auto"`,
  ADR-004). Config/env: `local_model`, `local_device`, `local_compute_type`,
  `local_fail_open` (default true → cloud auto when local missing),
  `HARK_STT_PROVIDER` / `HARK_STT_LOCAL_*`. Extra: `pip install 'hark[local-stt]'`.
  Documented B069 RTF (~0.1–0.15 tiny.en on short clips). Not for continuous
  ambient wake. `hark doctor` / `hark providers` surface readiness.
- **Sherpa-ONNX KWS wake backend** (B070): optional `[ambient] engine = "sherpa_kws"`
  beside default Vosk — open-vocab keyword spotting (English GigaSpeech 3.3M int8)
  with keywords built from `WakePolicy` (rebuild on config watch / SIGHUP);
  `./scripts/download-sherpa-kws-model.sh`; `uv sync --extra wake-sherpa`;
  doctor readiness (`status=ready|missing_model|package_missing`); optional
  `@pytest.mark.sherpa_kws` fixture tests. Guided setup: `hark setup` writes
  config + `~/.local/state/hark/setup-complete.json` (`hark_version`); skill docs
  `skill/hark/SETUP.md` + `WAKE_STT.md` (package mirrors). Fail-open if model
  missing; Vosk remains product default until dogfood.
- Ambient continuous mic stream (B079): idle wake holds one `MicLease` +
  `InputStream` + ring buffer instead of open/close per snippet; overlapping
  score windows (`snippet_s` / `snippet_hop_s`, `ring_s`). Answer/post-wake
  capture seeds ≥250 ms pre-speech via `listen.pre_roll_ms` (default 300,
  clamped 250–500). Docs: `AUDIO_DESIGN` continuous stream model. Unit tests
  for ring windowing/hop and pre-roll (no hardware).
- **Live web dashboard** (I003 / B060–B067): `hark serve` — REST + SSE backend
  implementing the new versioned `hark.dashboard.v1` contract
  ([docs/DASHBOARD.md](docs/DASHBOARD.md), `schemas/dashboard-v1/`,
  `fixtures/dashboard/`; fixture-driven for Rust-port parity) and a bundled
  static webui (Vite + preact, dark mono operator console): live event tail
  with filters/search/pause, Herdr multi-session map with pane context and
  bound answers (one-tap menu choices + typed), voice-pipeline/queue/usage/
  health panels, and mic dictation (browser MediaRecorder with ffmpeg
  transcode, or host mic via the existing listen flow) with review-then-submit
  through the safe delivery path. Localhost by default; token→cookie auth for
  remote; `tailscale serve` TLS documented for phone use; new `[dashboard]`
  config section and `hark doctor` posture checks; webui ships in the wheel
  via `scripts/build-webui.sh`.

- Defaults: wake names **iris**, **mercury**, **hark**, **herald** (persona
  pairing Iris→TTS **eve**, Mercury→**leo**); guided setup / Sherpa chooser
  folded into B070; enrollment sampling idea I006. Skill: cancel radio on
  unrelated conversation / TTS bleed.
- Radio end UX (B068): clearer operator end signals + Mode A **must** finish
  capture on done-signal partials. Soft list gains `okay over` / `ok over`
  (STT of “okay, over” without comma) and `message done`; sentence-final
  `over` still treats comma as boundary. Partial `HOLD_INSTRUCTIONS`,
  `agent_control` hints, and compact monitor lines use **MUST**
  `hark listen-end` language with false-positive guidance. Skill bootstrap
  reminds: “when you’re done, say over or okay hark send.” Docs:
  `AUDIO_DESIGN` how-to-end table; PROTOCOL/SPEC/ACCEPTANCE; both skill copies.
- Docs (B069 / I004): local STT & wake-ASR survey — constraints, candidate table
  (Vosk, faster-whisper, whisper.cpp, Sherpa-ONNX KWS, Moonshine, Porcupine-class),
  machine probes vs Vosk baseline, recommendation (Sherpa KWS next; keep Vosk+cloud
  interim). See `docs/plans/B069-local-stt-survey.md`. Follow-ups B070–B073.
- Docs/helper (B073): optional larger Vosk via `ambient.model_path`
  (`vosk-model-en-us-0.22-lgraph` ~128M / `0.22` ~1.8G) — RAM/alias trade-offs in
  `docs/AUDIO_DESIGN.md` + `docs/CUSTOM_WAKE.md`;
  `scripts/download-vosk-model.sh --model lgraph|0.22` (default small unchanged).
- Wake eval harness (B071): expand `fixtures/voice/wake/` (live + derived
  noise/gain/pad/silence + text-only dimensions), `hark.wake_eval` hit/miss/FA
  scoring, `scripts/eval-wake-fixtures.py` summary table for text_path / Vosk /
  Sherpa KWS (optional skip when B070 model absent), `scripts/gen-wake-eval-fixtures.py`,
  `tests/test_wake_eval_harness.py` + `@pytest.mark.sherpa_kws`. Capture notes in
  `fixtures/voice/README.md`.
- Ambient live-reload: when the primary wake name/phrase changes (config.toml
  file-watch or SIGHUP), speak a one-shot TTS announce
  (“Wake phrase updated from … to …”) without using the phrase cache
  (`use_cache=False`). `ambient.reloaded` carries `wake_label` /
  `wake_label_prev` / `wake_label_changed`.
- fix(monitor): tolerate string `question`/`target` in `--for-monitor` compact
  (legacy watch lines no longer crash `hark monitor`).
- Voice Herdr agent control (I005 / B055–B059): resolve coding CLIs with safe alias
  preference (`cc`/`cx`/`gk`/`cr` when PATH-safe), `HerdrClient` session ensure +
  `agent start`, CLI `hark session list|ensure` and `hark agent-start` (optional
  kickoff `--prompt`), Mode A skill playbook (clarify session/space with short
  options; one audio question at a time), `[agents]` config + doctor coding-CLI
  readiness.
- Site (B054): replace footer SPEC link with `llms.txt`; add `site/llms.txt`
  (llmstxt.org-style map of Hark docs for AI crawlers).

## 0.1.6

- Docs (B053): align root and npm READMEs with the marketing site — OG hero image,
  outsider-friendly framing, Supports list (Claude Code / Grok Build / Antigravity /
  Pi / OpenCode / Codex), and bare `hark monitor` as the primary Mode A feed.

- Media ducking polish (B047 / I002): config comments + env defaults
  (`HARK_DUCK_MEDIA_DURING_{TTS,STT}`, `HARK_PAUSE_MEDIA_DURING_{TTS,STT}`,
  `HARK_DUCK_LEVEL`, `HARK_MEDIA_CHECK_MPRIS` when TOML keys absent),
  `docs/AUDIO_DESIGN.md` completeness (TTS vs STT, fail-open, conference hold
  precedence, half-duplex / no idle-wake ducking, shipped defaults with
  `duck_level = 0.15`), `hark doctor` soft readiness for `pactl` / `playerctl`
  (degraded warning, not hard fail). Behavior itself shipped in B044–B046.

## 0.1.5

- Site Supports notes: document bare `hark monitor` (compact/`--for-monitor` is default on).
- Media duck/pause during STT capture (B046 / I002): answer-window and post-wake
  listen lower non-Hark sink-input volumes (and optionally pause MPRIS players)
  so background music does not bleed into the mic / energy gate / cloud STT.
  Wired once in `run_listen` via the same nestable `duck_media` primitive as TTS,
  with **explicit** STT flags (not TTS defaults). Continuous idle ambient wake
  (local Vosk) is **not** ducked. Config: `duck_media_during_stt` (default on),
  `pause_media_during_stt` (default **on** for dogfood), reuses `duck_level` /
  `duck_exclude_apps` / `media_check_mpris`. Fail-open + always restore.
  See `docs/AUDIO_DESIGN.md`.
- Media ducking during TTS (B045 / I002): when music/podcasts play, TTS no longer
  has to fight full volume. `duck_media` / `duck_media_during` snapshots non-Hark
  sink-input volumes, lowers each to `prior * duck_level` (default **0.15**) via
  `pactl set-sink-input-volume`, and **always restores** in `finally` (fail-open
  if set fails). Optional `pause_media_during_tts` uses MPRIS/`playerctl` Pause
  on Playing players, then ducks remaining sources and resumes on exit.
  Wired into `run_tts` play path (alongside mic mute); conference skip/hold still
  wins and duck lists use `exclude_conference=True`. Config: `duck_media_during_tts`
  (default on), `pause_media_during_tts` (default off), `duck_level`,
  `duck_exclude_apps`, `media_check_mpris`. Meta: `media_ducked` / `media_duck` on
  `run_tts` result. STT-window duck is B046 (same primitive). See
  `docs/AUDIO_DESIGN.md` and `docs/plans/I002-media-ducking.md`.
- Site Supports section (B052): replace placeholder monochrome marks with
  recognizable official logos (Claude aster, Grok singularity, Pi block mark,
  OpenCode O, Codex, Antigravity arch) under `site/assets/logos/`; Antigravity
  raised among primary orchestrators (after Grok Build); strip/table copy —
  **Antigravity** only (no “agy”), **Grok Build** only, Support column with
  **Native / Monitor** (Claude Code, Grok Build) and **Native / AgentAPI**
  (Antigravity); Pi/OpenCode notes frame plugins as examples of any Monitor
  on `hark monitor`. No backlog IDs in marketing copy.
- Media detection (B044 / I002 foundation): `hark.audio.media` detects active
  non-Hark playback via Pulse/PipeWire sink-inputs (index, volume, mute, corked,
  application.name) plus optional MPRIS (`playerctl`). Public API:
  `MediaMatch`, `is_media_active`, `detect_media`, duckable index/volume helpers
  for B045/B046. Excludes Hark TTS/cue streams (ffplay/paplay/…); fail-open when
  tools are missing. **Conference hold (B017) still wins over duck** — see
  `docs/AUDIO_DESIGN.md` and `docs/plans/I002-media-ducking.md`.
- Site Supports section (B048): local SVG marks under `site/assets/logos/` for
  Claude Code, Grok, Pi, OpenCode, Codex, plus Antigravity (agy) —
  logo strip + table cells, dark-bg friendly, no CDN. See `site/README.md`.
- First-class orchestrator listing (B050): **Antigravity (`agy`)** joins Claude,
  Grok, Pi, and OpenCode on the homepage Supports table (Monitor: **agentapi**),
  skill Monitor notes, README, and package docs. agentapi Mode A path is B049.
- Antigravity (`agy`) Mode A foundation (B049): experimental **agentapi** wake path
  for harnesses without a native Monitor. New `hark agentapi`
  (`register` / `status` / `send` / `deliver`), module `src/hark/agentapi.py`,
  sidecar script `scripts/hark-agy-deliver.sh`, docs `docs/AGY.md` +
  `docs/plans/B049-agy-agentapi.md`, skill notes listing agy as experimental.
  Pattern inspired by c2c (`AgyAdapter` / agentapi inject); not a c2c dependency.
- Skill: document Herdr **local / SSH / mixed** multi-session setup (`[[herdr.sessions]]`
  with optional per-session `ssh`) for Mode A agents — see `skill/hark/SKILL.md`.
- Site homepage (B043): sticky nav chrome spans full viewport width (content still
  max-width centered); hero pitch pills and marketing copy drop internal “Mode A”
  jargon for outsider-readable voice/fleet framing.
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
