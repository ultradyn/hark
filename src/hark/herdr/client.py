"""Herdr client: CLI subprocess + health checks."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hark.config import SessionConfig, resolve_session_socket


class HerdrError(Exception):
    """Herdr unreachable, version too old, or protocol error."""


@dataclass
class AgentInfo:
    session_id: str
    pane_id: str
    agent: str | None
    status: str
    revision: int = 0
    workspace_id: str | None = None
    tab_id: str | None = None
    terminal_id: str | None = None
    cwd: str | None = None
    focused: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def target(self) -> str:
        return f"{self.session_id}/{self.pane_id}"


@dataclass
class HerdrSessionHealth:
    session_id: str
    ok: bool
    version: str | None = None
    socket: str | None = None
    agent_count: int = 0
    error: str | None = None
    protocol: int | None = None


@dataclass
class NamedSessionInfo:
    """One entry from ``herdr session list --json``."""

    name: str
    running: bool
    default: bool = False
    session_dir: str | None = None
    socket_path: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class HerdrClient:
    MIN_VERSION = (0, 7, 1)

    def __init__(
        self,
        session: SessionConfig,
        herdr_bin: str | None = None,
    ) -> None:
        self.session = session
        # ``herdr_bin`` on an SSH session names the remote binary. Tunnel-backed
        # operations still need a local client against HERDR_SOCKET_PATH.
        configured_bin = None if session.ssh else session.herdr_bin
        self.herdr_bin = herdr_bin or configured_bin or shutil.which("herdr") or "herdr"
        self.socket_path = resolve_session_socket(session)

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["HERDR_SOCKET_PATH"] = str(self.socket_path)
        if self.session.id and self.session.id != "local":
            env.setdefault("HERDR_SESSION", self.session.id)
        return env

    def run_raw(
        self,
        args: list[str],
        *,
        timeout: float = 15.0,
    ) -> subprocess.CompletedProcess[str]:
        cmd = [self.herdr_bin, *args]
        try:
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=self._env(),
                check=False,
            )
        except FileNotFoundError as exc:
            raise HerdrError(f"herdr binary not found: {self.herdr_bin}") from exc
        except subprocess.TimeoutExpired as exc:
            raise HerdrError(f"herdr timed out: {' '.join(cmd)}") from exc

    def run_json(self, args: list[str], *, timeout: float = 15.0) -> dict[str, Any]:
        proc = self.run_raw(args, timeout=timeout)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
            raise HerdrError(f"herdr {' '.join(args)} failed: {err[:400]}")
        text = (proc.stdout or "").strip()
        if not text:
            raise HerdrError(f"herdr {' '.join(args)} returned empty stdout")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise HerdrError(f"herdr {' '.join(args)} not JSON: {text[:200]!r}") from exc
        if not isinstance(data, dict):
            raise HerdrError(f"expected JSON object from herdr, got {type(data).__name__}")
        if "error" in data and data["error"]:
            raise HerdrError(f"herdr error: {data['error']}")
        return data

    def version_string(self) -> str:
        proc = self.run_raw(["--version"], timeout=5)
        out = (proc.stdout or proc.stderr or "").strip()
        for part in out.replace(",", " ").split():
            if part[0].isdigit() and "." in part:
                return part
        return out or "unknown"

    @staticmethod
    def parse_version(ver: str) -> tuple[int, ...]:
        nums: list[int] = []
        for piece in ver.split("."):
            digits = "".join(c for c in piece if c.isdigit())
            if digits:
                nums.append(int(digits))
            if len(nums) >= 3:
                break
        return tuple(nums) if nums else (0,)

    def check_version(self) -> str:
        ver = self.version_string()
        parsed = self.parse_version(ver)
        if parsed < self.MIN_VERSION:
            raise HerdrError(
                f"Herdr {ver} < required {'.'.join(map(str, self.MIN_VERSION))}"
            )
        return ver

    def socket_exists(self) -> bool:
        return Path(self.socket_path).exists()

    def list_agents(self) -> list[AgentInfo]:
        data = self.run_json(["agent", "list"])
        return parse_agent_list(data, session_id=self.session.id)

    def get_agent(self, pane_id: str) -> AgentInfo | None:
        for a in self.list_agents():
            if a.pane_id == pane_id:
                return a
        return None

    def list_sessions(self) -> list[NamedSessionInfo]:
        """Global ``herdr session list --json`` (not scoped to one socket)."""
        # session list is process-global; avoid forcing HERDR_SOCKET_PATH alone
        proc = self.run_raw(["session", "list", "--json"], timeout=10)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
            raise HerdrError(f"herdr session list failed: {err[:400]}")
        text = (proc.stdout or "").strip()
        if not text:
            raise HerdrError("herdr session list returned empty stdout")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise HerdrError(f"herdr session list not JSON: {text[:200]!r}") from exc
        return parse_session_list(data)

    def ensure_session(
        self,
        name: str,
        *,
        start: bool = True,
        wait_s: float = 8.0,
        poll_s: float = 0.25,
    ) -> NamedSessionInfo:
        """Return named Herdr session; optionally start headless server if not running.

        Idempotent when the session already exists and is running. Starting uses
        ``herdr --session <name> server`` (detached). ``name`` ``default`` maps to
        bare ``herdr server`` when needed.
        """
        name = (name or "").strip()
        if not name:
            raise HerdrError("session name is empty")

        def _find() -> NamedSessionInfo | None:
            for s in self.list_sessions():
                if s.name == name:
                    return s
            return None

        found = _find()
        if found is not None and found.running:
            return found
        if not start:
            if found is not None:
                return found
            raise HerdrError(f"herdr session {name!r} not found")

        # Start headless server for this named session
        if name == "default":
            cmd = [self.herdr_bin, "server"]
        else:
            cmd = [self.herdr_bin, "--session", name, "server"]
        try:
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                env=os.environ.copy(),
            )
        except FileNotFoundError as exc:
            raise HerdrError(f"herdr binary not found: {self.herdr_bin}") from exc

        deadline = time.monotonic() + max(0.5, wait_s)
        last: NamedSessionInfo | None = found
        while time.monotonic() < deadline:
            time.sleep(poll_s)
            last = _find()
            if last is not None and last.running:
                return last
        raise HerdrError(
            f"herdr session {name!r} did not become running within {wait_s}s"
            + (f" (last={last})" if last else "")
        )

    def start_agent(
        self,
        name: str,
        argv: list[str],
        *,
        cwd: str | None = None,
        workspace_id: str | None = None,
        tab_id: str | None = None,
        split: str | None = None,
        focus: bool = False,
        env_pairs: list[str] | None = None,
        timeout: float = 30.0,
    ) -> AgentInfo:
        """Run ``herdr agent start <name> … -- <argv>`` and return the new agent.

        ``argv`` must be non-empty (resolved coding CLI + args).
        """
        if not argv:
            raise HerdrError("start_agent requires non-empty argv")
        label = (name or "").strip() or "agent"
        args: list[str] = ["agent", "start", label]
        if cwd:
            args.extend(["--cwd", cwd])
        if workspace_id:
            args.extend(["--workspace", workspace_id])
        if tab_id:
            args.extend(["--tab", tab_id])
        if split:
            if split not in ("right", "down"):
                raise HerdrError(f"invalid split {split!r} (use right|down)")
            args.extend(["--split", split])
        if env_pairs:
            for pair in env_pairs:
                args.extend(["--env", pair])
        args.append("--focus" if focus else "--no-focus")
        args.append("--")
        args.extend(str(a) for a in argv)

        data = self.run_json(args, timeout=timeout)
        return parse_agent_start(data, session_id=self.session.id)

    def health(self) -> HerdrSessionHealth:
        sock = str(self.socket_path)
        if not self.socket_exists() and not shutil.which(self.herdr_bin):
            return HerdrSessionHealth(
                session_id=self.session.id,
                ok=False,
                socket=sock,
                error="herdr binary missing and socket absent",
            )
        try:
            ver = self.check_version()
            agents = self.list_agents()
            return HerdrSessionHealth(
                session_id=self.session.id,
                ok=True,
                version=ver,
                socket=sock,
                agent_count=len(agents),
                protocol=14 if self.parse_version(ver) >= (0, 7, 1) else None,
            )
        except HerdrError as exc:
            return HerdrSessionHealth(
                session_id=self.session.id,
                ok=False,
                socket=sock,
                error=str(exc),
            )

    def send_text(self, pane_id: str, text: str, *, submit: bool = True) -> None:
        """Inject text into a pane. By default also press Enter to submit.

        Dogfood: freeform reply without Enter left the operator prompt unsent.
        Pass ``submit=False`` only when you intentionally want buffer-only paste.
        """
        sent = False
        try:
            self.run_json(["agent", "send", pane_id, text])
            sent = True
        except HerdrError:
            pass
        if not sent:
            try:
                self.run_json(["pane", "send-text", pane_id, text])
                sent = True
            except HerdrError as exc:
                raise HerdrError(f"could not send text to {pane_id}: {exc}") from exc
        if submit:
            # agent send / send-text often only type; Enter submits to the agent
            self.send_keys(pane_id, ["enter"])

    def send_keys(self, pane_id: str, keys: list[str]) -> None:
        if not keys:
            raise HerdrError("no keys to send")
        proc = self.run_raw(["pane", "send-keys", pane_id, *keys])
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            raise HerdrError(f"pane send-keys failed: {err[:400]}")

    def read_pane(self, pane_id: str, lines: int = 60) -> str:
        data = self.run_json(
            [
                "agent",
                "read",
                pane_id,
                "--source",
                "recent-unwrapped",
                "--lines",
                str(lines),
                "--format",
                "text",
            ]
        )
        result = data.get("result") or data
        if isinstance(result, dict):
            read = result.get("read")
            if isinstance(read, dict) and isinstance(read.get("text"), str):
                return read["text"]
            for key in ("text", "content", "output", "visible", "recent"):
                if isinstance(result.get(key), str):
                    return result[key]
        # fallback sources
        for source in ("recent", "visible"):
            try:
                data = self.run_json(
                    [
                        "agent",
                        "read",
                        pane_id,
                        "--source",
                        source,
                        "--lines",
                        str(lines),
                        "--format",
                        "text",
                    ]
                )
                result = data.get("result") or data
                if isinstance(result, dict):
                    read = result.get("read")
                    if isinstance(read, dict) and isinstance(read.get("text"), str):
                        return read["text"]
            except HerdrError:
                continue
        raise HerdrError(f"could not read pane {pane_id}")

def parse_agent_list(
    data: dict[str, Any],
    *,
    session_id: str = "default",
) -> list[AgentInfo]:
    """Parse Herdr ``agent list`` JSON (live CLI or fixtures/herdr/*.json).

    Accepts either the full CLI envelope ``{id, result: {agents, type}}`` or a
    bare ``{agents: [...]}`` object. Used by :meth:`HerdrClient.list_agents` and
    contract tests against redacted real captures.
    """
    if not isinstance(data, dict):
        raise HerdrError(
            f"expected JSON object for agent list, got {type(data).__name__}"
        )
    result = data.get("result") or data
    agents_raw = result.get("agents") if isinstance(result, dict) else None
    if agents_raw is None and isinstance(data.get("agents"), list):
        agents_raw = data["agents"]
    if not isinstance(agents_raw, list):
        raise HerdrError("agent list missing agents[]")

    out: list[AgentInfo] = []
    for item in agents_raw:
        info = _agent_info_from_dict(item, session_id=session_id)
        if info is not None:
            out.append(info)
    return out


def parse_session_list(data: dict[str, Any] | list[Any]) -> list[NamedSessionInfo]:
    """Parse ``herdr session list --json``."""
    if isinstance(data, list):
        raw_list = data
    elif isinstance(data, dict):
        raw_list = data.get("sessions")
        if raw_list is None and isinstance(data.get("result"), dict):
            raw_list = data["result"].get("sessions")
        if raw_list is None:
            raise HerdrError("session list missing sessions[]")
    else:
        raise HerdrError(
            f"expected JSON object/list for session list, got {type(data).__name__}"
        )
    if not isinstance(raw_list, list):
        raise HerdrError("session list missing sessions[]")
    out: list[NamedSessionInfo] = []
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("id") or "").strip()
        if not name:
            continue
        out.append(
            NamedSessionInfo(
                name=name,
                running=bool(item.get("running", False)),
                default=bool(item.get("default", False)),
                session_dir=(
                    str(item["session_dir"]) if item.get("session_dir") else None
                ),
                socket_path=(
                    str(item["socket_path"])
                    if item.get("socket_path")
                    else (str(item["socket"]) if item.get("socket") else None)
                ),
                raw=item,
            )
        )
    return out


def parse_agent_start(
    data: dict[str, Any],
    *,
    session_id: str = "default",
) -> AgentInfo:
    """Parse ``herdr agent start`` JSON into :class:`AgentInfo`."""
    if not isinstance(data, dict):
        raise HerdrError(
            f"expected JSON object for agent start, got {type(data).__name__}"
        )
    result = data.get("result") if isinstance(data.get("result"), dict) else data
    agent_raw = result.get("agent") if isinstance(result, dict) else None
    if not isinstance(agent_raw, dict):
        # some versions may return the agent at top level
        if isinstance(result, dict) and result.get("pane_id"):
            agent_raw = result
        else:
            raise HerdrError("agent start response missing result.agent")
    info = _agent_info_from_dict(agent_raw, session_id=session_id)
    if info is None:
        raise HerdrError("agent start response missing pane_id")
    return info


def _agent_info_from_dict(
    item: Any,
    *,
    session_id: str,
) -> AgentInfo | None:
    if not isinstance(item, dict):
        return None
    pane_id = str(item.get("pane_id") or item.get("id") or "")
    if not pane_id:
        return None
    status = str(
        item.get("agent_status")
        or item.get("status")
        or item.get("state")
        or "unknown"
    )
    agent_label = item.get("agent") or item.get("name")
    return AgentInfo(
        session_id=session_id,
        pane_id=pane_id,
        agent=(str(agent_label) if agent_label else None),
        status=status,
        revision=int(item.get("revision") or item.get("pane_revision") or 0),
        workspace_id=(
            str(item["workspace_id"]) if item.get("workspace_id") else None
        ),
        tab_id=str(item["tab_id"]) if item.get("tab_id") else None,
        terminal_id=(
            str(item["terminal_id"]) if item.get("terminal_id") else None
        ),
        cwd=str(item["cwd"]) if item.get("cwd") else None,
        focused=bool(item.get("focused", False)),
        raw=item,
    )

