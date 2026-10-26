"""Offline speech diagnostics: configuration is not proof of audio access."""

from __future__ import annotations

import re
from typing import Any

from hark.config import HarkConfig


def normalize_provider(name: str) -> str:
    name = (name or "auto").strip().lower().replace("-", "_")
    return {"gemini": "google", "custom_stt": "custom", "custom_tts": "custom",
            "local": "faster_whisper", "whisper": "faster_whisper", "fasterwhisper": "faster_whisper"}.get(name, name)


def speech_readiness(cfg: HarkConfig, *, providers=None, custom_stt=None,
                     custom_tts=None, local_stt=None, events=None) -> dict[str, Any]:
    """Describe each configured leg without making provider requests.

    Recent failures expose only fixed metadata, never provider response bodies,
    prompts or credentials. A later success clears the previous failure.
    """
    if providers is None:
        from hark.providers.auth import all_provider_status
        providers = [vars(auth) for auth in all_provider_status()]
    if custom_stt is None:
        from hark.providers.custom_stt import custom_stt_status
        custom_stt = vars(custom_stt_status(**{
            key: getattr(cfg.stt, "custom_" + key, None)
            for key in ("base_url", "api_key", "api_key_file", "api_key_command", "model", "path")
        }))
    if custom_tts is None:
        from hark.providers.custom_tts import custom_tts_status
        custom_tts = vars(custom_tts_status(**{
            key: getattr(cfg.tts, "custom_" + key, None)
            for key in ("base_url", "api_key", "api_key_file", "api_key_command", "model", "path", "voice")
        }))
    if local_stt is None:
        from hark.providers.local_stt import local_stt_statuses
        local_stt = [vars(status) for status in local_stt_statuses(model=cfg.stt.local_model)]
    if events is None:
        from hark.syslog import tail_lines
        try:
            events = tail_lines(200)
        except OSError:
            events = []
    auth = {row["name"]: row for row in providers}
    local = {normalize_provider(row["name"]): row for row in local_stt}
    report = {}
    for leg, policy, supported, order, custom in (
        ("stt", cfg.stt, {"xai", "openai", "google", "custom", "faster_whisper", "moonshine"},
         ("xai", "openai", "google"), custom_stt),
        ("tts", cfg.tts, {"xai", "openai", "google", "minimax", "custom"},
         ("xai", "openai", "minimax", "google"), custom_tts),
    ):
        pin = normalize_provider(policy.provider)
        disabled = {normalize_provider(value) for value in policy.disabled}
        names = order if pin == "auto" else (pin,)
        if leg == "stt" and pin in ("faster_whisper", "moonshine") and cfg.stt.local_fail_open and pin not in disabled:
            names = (pin, *order)
        candidates = []
        for name in names:
            supported_here = name in supported
            configured = bool((custom if name == "custom" else local.get(name, auth.get(name, {}))).get("available"))
            consent = not (leg == "tts" and name == "minimax" and not cfg.tts.minimax_ok)
            row = {"provider": name, "supported": supported_here, "configured": configured,
                   "disabled": name in disabled, "consent": consent, "validation": "untested"}
            if not supported_here:
                row["status"] = "unsupported"
            elif name in disabled:
                row["status"] = "disabled"
            elif not consent:
                row["status"] = "consent_required"
            elif not configured:
                row["status"] = "not_configured"
            else:
                row["status"] = "untested"
            for event in reversed(events):
                if not isinstance(event, dict):
                    continue
                kind = str(event.get("event", ""))
                data = event.get("data") or {}
                if not isinstance(data, dict) or normalize_provider(str(data.get("provider", ""))) != name:
                    continue
                if kind not in (f"{leg}.error", f"{leg}.ok", f"{leg}.response"):
                    continue
                if kind.endswith(".response") and "ok" not in data:
                    continue
                if kind.endswith(".error") or data.get("ok") is False:
                    match = re.search(r"\b(401|403|429|5\d\d)\b", str(event.get("message", "")))
                    row["last_failure"] = {"event": kind, "http_status": int(match[1]) if match else None}
                    if row["status"] == "untested" and match and match[1] in ("401", "403"):
                        row["status"] = "last_auth_failed"
                break
            if name == "openai" and auth.get(name, {}).get("source") == "codex":
                row["hint"] = "Codex credentials may lack audio scope; use an OpenAI API key if speech returns 401."
            candidates.append(row)
        viable = [row for row in candidates if row["status"] == "untested"]
        hint = (f"{leg.upper()} credentials/configuration present; audio access untested."
                if viable else f"No configured {leg.upper()} provider is usable under the selected policy.")
        if any(row["status"] == "unsupported" for row in candidates):
            hint += f" {pin} does not support {leg.upper()}; choose " + "|".join(order) + "|custom."
        if any(row["status"] == "consent_required" for row in candidates):
            hint += " Grant MiniMax TTS consent explicitly with hark setup --minimax-ok."
        if any(row["status"] == "last_auth_failed" for row in candidates):
            hint += " Recent authentication failure recorded; repair credentials and retry speech."
        if not viable and pin == "custom":
            hint += " Complete the custom provider URL, API key and required model."
        report[leg] = {"provider": pin, "configured": bool(viable), "validation": "untested",
                       "candidates": candidates, "hint": hint}
    report["configured"] = report["stt"]["configured"] and report["tts"]["configured"]
    return report


def next_actions(cfg: HarkConfig, *, speech, ambient, setup=None, sessions=()) -> list[dict[str, str]]:
    """Dependency-ordered, directly actionable setup repair steps."""
    actions = []
    def add(action, command, doc):
        actions.append({"action": action, "command": command, "doc": doc})
    if cfg.warnings:
        add("Review configuration warnings", "hark config show", "docs/SPEC.md")
    if cfg.ambient.enabled and ambient.get("package_ok") is False:
        extra = "wake-sherpa" if "sherpa" in str(ambient.get("engine")) else "wake"
        add("Install wake backend dependencies", f"uv sync --extra {extra}", "docs/CUSTOM_WAKE.md")
    if cfg.ambient.enabled and not ambient.get("model_ok"):
        command = ("./scripts/download-sherpa-kws-model.sh" if "sherpa" in str(ambient.get("engine"))
                   else "./scripts/setup-ambient.sh")
        add("Install the configured wake model", command, "docs/CUSTOM_WAKE.md")
    for leg in ("stt", "tts"):
        if not speech[leg]["configured"]:
            add(speech[leg]["hint"], "hark providers", "docs/PROVIDERS.md")
        elif any(row.get("last_failure") for row in speech[leg]["candidates"]):
            add(f"Inspect the most recent {leg.upper()} provider failure", "hark logs -n 50", "docs/PROVIDERS.md")
    if not setup or setup.get("needs_run"):
        add("Save setup preferences", "hark setup", "skills/hark/SETUP.md")
    if any(not row.get("ok") for row in sessions):
        add("Start or reconnect the configured Herdr session", "herdr start", "docs/HERDR.md")
    add("Check diagnostics before starting workers; cloud audio access remains untested", "hark doctor; hark start", "README.md")
    return actions
