"""Antigravity (agy) agentapi helpers for Mode A wake / deliver.

Google Antigravity CLI (``agy``) has no native long-lived Monitor tool.
Inbound wake uses::

    agy agentapi send-message [--title=…] <conversation_id> <content>

with ``ANTIGRAVITY_LS_ADDRESS`` set to the local language-service HTTP address
and ``conversation_id`` as the recipient (often self).

This is the c2c-inspired path (AgyAdapter / Mode_agy_inject / agy-env.json):
a small sidecar tails ``hark monitor --for-monitor`` and injects each HEP line
into the agy conversation so Mode A can stay event-driven without polling.

Status: **experimental foundation** — pure env/payload helpers + thin
subprocess inject. Full managed lifecycle (hooks, auto-register on SessionStart)
is left to follow-ups; see ``docs/plans/B049-agy-agentapi.md`` and ``docs/AGY.md``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, TextIO

from hark.paths import state_dir

ENV_LS_ADDRESS = "ANTIGRAVITY_LS_ADDRESS"
ENV_CONVERSATION_ID = "ANTIGRAVITY_CONVERSATION_ID"
DEFAULT_TITLE = "hark mode-a"
DEFAULT_AGY_BIN = "agy"
AGY_ENV_FILENAME = "agy-env.json"

WAKE_PREAMBLE = (
    "[hark] Mode A wake — treat as a Monitor event. "
    "Use the hark skill (context / ask / answer / tts). "
    "Do not invent answers. Then idle until the next wake."
)


@dataclass(frozen=True)
class AgyEnv:
    """Registered Antigravity inject target."""

    ls_address: str
    conversation_id: str

    def normalized(self) -> AgyEnv:
        ls = (self.ls_address or "").strip()
        conv = (self.conversation_id or "").strip()
        if not ls:
            raise ValueError("ls_address is empty")
        if not conv:
            raise ValueError("conversation_id is empty")
        return AgyEnv(ls_address=ls, conversation_id=conv)


@dataclass(frozen=True)
class SendResult:
    ok: bool
    argv: list[str]
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    dry_run: bool = False
    error: str | None = None


def agy_env_path(*, base: Path | None = None) -> Path:
    """Default path for persisted agy inject metadata."""
    root = base if base is not None else state_dir()
    return root / AGY_ENV_FILENAME


def read_agy_env(path: Path | None = None) -> AgyEnv | None:
    """Load ``agy-env.json`` if present and valid."""
    p = path if path is not None else agy_env_path()
    if not p.is_file():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    ls = raw.get("ls_address")
    conv = raw.get("conversation_id")
    if not isinstance(ls, str) or not isinstance(conv, str):
        return None
    try:
        return AgyEnv(ls_address=ls, conversation_id=conv).normalized()
    except ValueError:
        return None


def write_agy_env(env: AgyEnv, path: Path | None = None) -> Path:
    """Persist inject target; creates parent dirs. Returns the path written."""
    p = path if path is not None else agy_env_path()
    normalized = env.normalized()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ls_address": normalized.ls_address,
        "conversation_id": normalized.conversation_id,
    }
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(p)
    return p


def resolve_agy_env_from_environ(
    environ: Mapping[str, str] | None = None,
) -> AgyEnv | None:
    """Build AgyEnv from process env (``ANTIGRAVITY_*``), if both are set."""
    env = environ if environ is not None else os.environ
    ls = (env.get(ENV_LS_ADDRESS) or "").strip()
    conv = (env.get(ENV_CONVERSATION_ID) or "").strip()
    if not ls or not conv:
        return None
    try:
        return AgyEnv(ls_address=ls, conversation_id=conv).normalized()
    except ValueError:
        return None


def resolve_agy_env(
    *,
    path: Path | None = None,
    environ: Mapping[str, str] | None = None,
    prefer: str = "file",
) -> AgyEnv | None:
    """Resolve inject target from file and/or environ.

    prefer:
      - ``file`` (default): file then environ
      - ``environ``: environ then file
    """
    file_env = read_agy_env(path)
    proc_env = resolve_agy_env_from_environ(environ)
    if prefer == "environ":
        return proc_env or file_env
    return file_env or proc_env


def format_wake_payload(
    line: str | Mapping[str, Any],
    *,
    preamble: str = WAKE_PREAMBLE,
) -> str:
    """Wrap a HEP monitor line (or dict) as agentapi message body.

    Blank input raises ValueError. Dicts are JSON-serialized compactly.
    """
    if isinstance(line, Mapping):
        body = json.dumps(line, separators=(",", ":"), ensure_ascii=False)
    else:
        body = str(line).strip()
    if not body:
        raise ValueError("wake payload is empty")
    head = (preamble or "").strip()
    if not head:
        return body
    return f"{head}\n\n{body}"


def parse_monitor_line(line: str) -> dict[str, Any] | None:
    """Parse one NDJSON HEP line; return dict or None if blank/invalid."""
    s = (line or "").strip()
    if not s:
        return None
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def build_send_message_argv(
    conversation_id: str,
    content: str,
    *,
    title: str = DEFAULT_TITLE,
    agy_bin: str = DEFAULT_AGY_BIN,
) -> list[str]:
    """Build argv for ``agy agentapi send-message`` (no shell)."""
    conv = (conversation_id or "").strip()
    if not conv:
        raise ValueError("conversation_id is empty")
    if content is None or str(content) == "":
        raise ValueError("content is empty")
    bin_name = (agy_bin or DEFAULT_AGY_BIN).strip() or DEFAULT_AGY_BIN
    t = (title or DEFAULT_TITLE).strip() or DEFAULT_TITLE
    return [
        bin_name,
        "agentapi",
        "send-message",
        f"--title={t}",
        conv,
        str(content),
    ]


def which_agy(agy_bin: str = DEFAULT_AGY_BIN) -> str | None:
    """Resolve agy binary path, or None if missing."""
    name = (agy_bin or DEFAULT_AGY_BIN).strip() or DEFAULT_AGY_BIN
    if os.path.isabs(name) and os.path.isfile(name) and os.access(name, os.X_OK):
        return name
    return shutil.which(name)


def send_message(
    content: str,
    *,
    env: AgyEnv | None = None,
    path: Path | None = None,
    title: str = DEFAULT_TITLE,
    agy_bin: str = DEFAULT_AGY_BIN,
    timeout: float = 60.0,
    dry_run: bool = False,
    environ: Mapping[str, str] | None = None,
) -> SendResult:
    """Inject *content* into the registered conversation via agentapi.

    Sets ``ANTIGRAVITY_LS_ADDRESS`` for the child process (replacing any prior
    value). Does not drain or mutate inboxes — pure wake inject.
    """
    target = env or resolve_agy_env(path=path, environ=environ)
    if target is None:
        return SendResult(
            ok=False,
            argv=[],
            error=(
                "no agy env: run `hark agentapi register` with "
                f"{ENV_LS_ADDRESS} and {ENV_CONVERSATION_ID} set, "
                f"or write {agy_env_path()}"
            ),
        )
    try:
        target = target.normalized()
        argv = build_send_message_argv(
            target.conversation_id,
            content,
            title=title,
            agy_bin=agy_bin,
        )
    except ValueError as exc:
        return SendResult(ok=False, argv=[], error=str(exc))

    if dry_run:
        return SendResult(ok=True, argv=argv, dry_run=True, returncode=0)

    resolved = which_agy(argv[0])
    if resolved is None:
        return SendResult(
            ok=False,
            argv=argv,
            error=f"agy binary not found: {argv[0]!r} (install Antigravity CLI)",
        )
    run_argv = [resolved, *argv[1:]]

    child_env = dict(os.environ if environ is None else environ)
    # Replace any prior LS address so inject targets the registered session.
    child_env[ENV_LS_ADDRESS] = target.ls_address
    child_env[ENV_CONVERSATION_ID] = target.conversation_id

    try:
        proc = subprocess.run(
            run_argv,
            env=child_env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return SendResult(
            ok=False,
            argv=run_argv,
            error=f"agentapi timed out after {timeout}s",
            stdout=(exc.stdout or "") if isinstance(exc.stdout, str) else "",
            stderr=(exc.stderr or "") if isinstance(exc.stderr, str) else "",
        )
    except OSError as exc:
        return SendResult(ok=False, argv=run_argv, error=str(exc))

    return SendResult(
        ok=proc.returncode == 0,
        argv=run_argv,
        returncode=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        error=None if proc.returncode == 0 else f"exit {proc.returncode}",
    )


def deliver_line(
    line: str,
    *,
    env: AgyEnv | None = None,
    path: Path | None = None,
    title: str = DEFAULT_TITLE,
    agy_bin: str = DEFAULT_AGY_BIN,
    timeout: float = 60.0,
    dry_run: bool = False,
    wrap: bool = True,
    environ: Mapping[str, str] | None = None,
) -> SendResult | None:
    """Deliver one monitor stdout line. Blank lines return None (skip)."""
    s = (line or "").strip()
    if not s:
        return None
    body = format_wake_payload(s) if wrap else s
    return send_message(
        body,
        env=env,
        path=path,
        title=title,
        agy_bin=agy_bin,
        timeout=timeout,
        dry_run=dry_run,
        environ=environ,
    )


def deliver_lines(
    lines: Iterable[str],
    *,
    env: AgyEnv | None = None,
    path: Path | None = None,
    title: str = DEFAULT_TITLE,
    agy_bin: str = DEFAULT_AGY_BIN,
    timeout: float = 60.0,
    dry_run: bool = False,
    wrap: bool = True,
    stop_on_error: bool = False,
    environ: Mapping[str, str] | None = None,
) -> list[SendResult]:
    """Deliver many lines; skips blanks. Optionally stop on first failure."""
    out: list[SendResult] = []
    for line in lines:
        result = deliver_line(
            line,
            env=env,
            path=path,
            title=title,
            agy_bin=agy_bin,
            timeout=timeout,
            dry_run=dry_run,
            wrap=wrap,
            environ=environ,
        )
        if result is None:
            continue
        out.append(result)
        if stop_on_error and not result.ok:
            break
    return out


def env_to_public_dict(env: AgyEnv) -> dict[str, str]:
    """JSON-friendly dict (no secrets beyond session identity)."""
    return asdict(env.normalized())


def status_dict(
    *,
    path: Path | None = None,
    environ: Mapping[str, str] | None = None,
    agy_bin: str = DEFAULT_AGY_BIN,
) -> dict[str, Any]:
    """Snapshot for ``hark agentapi status``."""
    p = path if path is not None else agy_env_path()
    file_env = read_agy_env(p)
    proc_env = resolve_agy_env_from_environ(environ)
    resolved = resolve_agy_env(path=p, environ=environ)
    return {
        "env_path": str(p),
        "file": env_to_public_dict(file_env) if file_env else None,
        "process": env_to_public_dict(proc_env) if proc_env else None,
        "resolved": env_to_public_dict(resolved) if resolved else None,
        "agy_bin": agy_bin,
        "agy_found": which_agy(agy_bin) is not None,
        "agy_path": which_agy(agy_bin),
    }


def follow_monitor_and_deliver(
    *,
    env: AgyEnv | None = None,
    path: Path | None = None,
    title: str = DEFAULT_TITLE,
    agy_bin: str = DEFAULT_AGY_BIN,
    timeout: float = 60.0,
    dry_run: bool = False,
    wrap: bool = True,
    replay: int = 0,
    hark_bin: list[str] | None = None,
    log: TextIO | None = None,
    stop_on_error: bool = False,
) -> int:
    """Spawn ``hark monitor --for-monitor`` and inject each line (sidecar).

    Returns process exit code of the monitor (or 1 on hard inject failure when
    stop_on_error). Designed for ``hark agentapi deliver --follow-monitor``.
    """
    err = log if log is not None else sys.stderr
    target = env or resolve_agy_env(path=path)
    if target is None and not dry_run:
        print(
            "hark agentapi: no agy env registered "
            f"(need {ENV_LS_ADDRESS} + {ENV_CONVERSATION_ID} or {agy_env_path()})",
            file=err,
        )
        return 1

    if hark_bin is None:
        # Prefer same interpreter package entry when available.
        hark_bin = [sys.executable, "-m", "hark"]

    argv = [
        *hark_bin,
        "monitor",
        "--for-monitor",
        "--replay",
        str(int(replay)),
    ]
    print(f"hark agentapi deliver: following {' '.join(argv)}", file=err)
    if dry_run:
        print("hark agentapi deliver: dry-run (no inject)", file=err)

    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        print(f"hark agentapi deliver: failed to start monitor: {exc}", file=err)
        return 1

    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            # Monitor may interleave non-JSON diagnostics; only inject JSON-ish lines.
            stripped = line.strip()
            if not stripped:
                continue
            if not stripped.startswith("{"):
                print(stripped, file=err)
                continue
            result = deliver_line(
                stripped,
                env=target,
                path=path,
                title=title,
                agy_bin=agy_bin,
                timeout=timeout,
                dry_run=dry_run,
                wrap=wrap,
            )
            if result is None:
                continue
            if result.ok:
                kind = "?"
                parsed = parse_monitor_line(stripped)
                if parsed and parsed.get("kind"):
                    kind = str(parsed["kind"])
                tag = "dry-run" if result.dry_run else "ok"
                print(f"hark agentapi deliver: {tag} kind={kind}", file=err)
            else:
                print(
                    f"hark agentapi deliver: inject failed: {result.error}",
                    file=err,
                )
                if stop_on_error:
                    proc.terminate()
                    return 1
    except KeyboardInterrupt:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        return 130
    return int(proc.wait() or 0)
