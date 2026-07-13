# Changelog

All notable changes to **Hark** and the **`@ultradyn/hark`** npm skills package.

Format: sections headed `## X.Y.Z` match git tags `vX.Y.Z` and the npm package version in `packages/ultradyn-hark/package.json`.

## Unreleased

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
