"""Herdr client: CLI subprocess + health checks."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
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


class HerdrClient:
    MIN_VERSION = (0, 7, 1)

    def __init__(
        self,
        session: SessionConfig,
        herdr_bin: str | None = None,
    ) -> None:
        self.session = session
        self.herdr_bin = herdr_bin or session.herdr_bin or shutil.which("herdr") or "herdr"
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
        if not isinstance(item, dict):
            continue
        pane_id = str(item.get("pane_id") or item.get("id") or "")
        if not pane_id:
            continue
        status = str(
            item.get("agent_status")
            or item.get("status")
            or item.get("state")
            or "unknown"
        )
        out.append(
            AgentInfo(
                session_id=session_id,
                pane_id=pane_id,
                agent=(str(item["agent"]) if item.get("agent") else None),
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
        )
    return out

