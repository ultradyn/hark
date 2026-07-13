"""XDG paths for config and state."""

from __future__ import annotations

import os
from pathlib import Path


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / "hark"
    return Path.home() / ".config" / "hark"


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    if base:
        return Path(base) / "hark"
    return Path.home() / ".local" / "state" / "hark"


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    if base:
        return Path(base) / "hark"
    return Path.home() / ".cache" / "hark"


def default_config_path() -> Path:
    override = os.environ.get("HARK_CONFIG")
    if override:
        return Path(override)
    return config_dir() / "config.toml"


def grok_auth_path() -> Path:
    return Path.home() / ".grok" / "auth.json"


def codex_auth_path() -> Path:
    """Codex CLI credentials (`auth.json`). Honors CODEX_HOME when set."""
    override = os.environ.get("CODEX_HOME")
    if override:
        return Path(override) / "auth.json"
    return Path.home() / ".codex" / "auth.json"


def opencode_auth_path() -> Path:
    """OpenCode provider credentials (XDG data: `…/opencode/auth.json`)."""
    base = os.environ.get("XDG_DATA_HOME")
    if base:
        return Path(base) / "opencode" / "auth.json"
    return Path.home() / ".local" / "share" / "opencode" / "auth.json"


def pi_agent_auth_path() -> Path:
    """Pi coding-agent credentials (`~/.pi/agent/auth.json`)."""
    return Path.home() / ".pi" / "agent" / "auth.json"


def mmx_config_path() -> Path:
    """MiniMax CLI (`mmx`) config/auth store (`~/.mmx/config.json`).

    Honors MMX_CONFIG_DIR when set (same as mmx-cli).
    """
    override = os.environ.get("MMX_CONFIG_DIR")
    if override:
        return Path(override) / "config.json"
    return Path.home() / ".mmx" / "config.json"


def legacy_minimax_path() -> Path:
    """Legacy MiniMax credential file/dir (`~/.minimax`), if present."""
    return Path.home() / ".minimax"


def default_herdr_socket() -> Path:
    override = os.environ.get("HERDR_SOCKET_PATH")
    if override:
        return Path(override)
    return Path.home() / ".config" / "herdr" / "herdr.sock"
