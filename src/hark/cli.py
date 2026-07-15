"""hark CLI entrypoint."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from hark import __version__
from hark.config import (
    SessionConfig,
    config_to_dict,
    eprint,
    load_config,
    write_default_config,
)
from hark.delivery import DeliveryStore
from hark.doctor import run_doctor
from hark.exitcodes import ABORT, AUDIO, ERROR, HERDR, OK, PROVIDER, TIMEOUT, USAGE
from hark.fingerprint import question_fingerprint
from hark.herdr.client import HerdrClient, HerdrError
from hark.paths import default_config_path, state_dir
from hark.providers.base import ProviderError
from hark.targets import parse_target
from hark.watch import run_watch


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hark",
        description="Hark — voice bridge for Herdr coding agents",
    )
    p.add_argument("--version", action="version", version=f"hark {__version__}")
    p.add_argument("--config", dest="config_path", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("doctor", help="check Herdr, auth, paths")
    d.add_argument("--json", action="store_true")

    su = sub.add_parser(
        "setup",
        help="guided first-run setup (persona, wake engine, sessions)",
    )
    su.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="non-interactive (use flags / defaults)",
    )
    su.add_argument(
        "--force",
        action="store_true",
        help="re-run even if setup-complete.json exists",
    )
    su.add_argument(
        "--persona",
        choices=("feminine", "masculine", "custom"),
        default=None,
        help="feminine=Iris+eve, masculine=Mercury+leo",
    )
    su.add_argument(
        "--wake-engine",
        dest="wake_engine",
        choices=("vosk", "sherpa_kws", "defer"),
        default=None,
        help="ambient wake backend (default vosk until dogfood)",
    )
    su.add_argument("--voice", default=None, help="TTS voice id (e.g. eve, leo)")
    su.add_argument(
        "--names",
        default=None,
        help="comma-separated wake names (overrides persona list)",
    )
    su.add_argument(
        "--sessions",
        default=None,
        help="comma sessions: local or id=ssh:host (e.g. local,work=ssh:box)",
    )
    su.add_argument(
        "--skip-doctor",
        action="store_true",
        help="skip doctor health print",
    )
    su.add_argument(
        "--skip-download",
        action="store_true",
        help="do not download Sherpa model when engine=sherpa_kws",
    )

    c = sub.add_parser("config", help="config path | init | show")
    cs = c.add_subparsers(dest="config_cmd", required=True)
    cs.add_parser("path")
    ci = cs.add_parser("init")
    ci.add_argument("--force", action="store_true")
    cshow = cs.add_parser("show")
    cshow.add_argument("--json", action="store_true")

    st = sub.add_parser("status", help="list agent statuses")
    st.add_argument("--session", action="append", dest="sessions")
    st.add_argument("--status", dest="filter_status")
    st.add_argument("--json", action="store_true")

    w = sub.add_parser("watch", help="emit HEP events from Herdr only")
    w.add_argument("--session", action="append", dest="sessions")
    w.add_argument("--statuses", default=None)
    w.add_argument("--for-monitor", action="store_true")
    w.add_argument("--transport", choices=("auto", "socket", "poll"))
    w.add_argument("--once", action="store_true")
    w.add_argument(
        "--read-questions",
        action="store_true",
        default=True,
        help="read question excerpts before emitting blocked events (default)",
    )

    def _add_dashboard_serve_parser(name: str, *, help: str) -> None:
        p = sub.add_parser(name, help=help)
        p.add_argument(
            "--host", default=None, help="bind host (default: [dashboard].host)"
        )
        p.add_argument(
            "--port",
            type=int,
            default=None,
            help="bind port (default: [dashboard].port)",
        )
        p.add_argument(
            "--print-token",
            action="store_true",
            help="generate a token for [dashboard].token and exit",
        )

    # Preferred names for the live web UI (B060–B067). Keep `serve` as alias.
    _add_dashboard_serve_parser(
        "webui",
        help="start live web dashboard UI (REST + SSE; alias: dashboard, serve)",
    )
    _add_dashboard_serve_parser(
        "dashboard",
        help="start live web dashboard UI (alias for webui / serve)",
    )
    _add_dashboard_serve_parser(
        "serve",
        help="alias for webui — live web dashboard (REST + SSE, hark.dashboard.v1)",
    )

    mon = sub.add_parser(
        "monitor",
        help=(
            "unified handsfree feed (watch + ambient JSONL): agent.blocked, "
            "ambient.prompt, ambient.wake_near_miss, … — single Monitor command"
        ),
    )
    mon.add_argument(
        "--for-monitor",
        action="store_true",
        default=True,
        help="compact HEP lines for harness Monitors (default on)",
    )
    mon.add_argument(
        "--full",
        action="store_true",
        help="emit full event objects (disable --for-monitor compaction)",
    )
    mon.add_argument(
        "--replay",
        type=int,
        default=5,
        metavar="N",
        help="replay last N matching events before follow (default 5; 0=none)",
    )
    mon.add_argument(
        "--kinds",
        default=None,
        help="comma-separated kind filter (default: handsfree wake set)",
    )
    mon.add_argument(
        "--allow-multiple",
        action="store_true",
        help=(
            "skip singleflight lock (debug only — a second consumer "
            "duplicates HEP wakes to the orchestrator)"
        ),
    )

    ctx = sub.add_parser("context", help="read pane context")
    ctx.add_argument("target")
    ctx.add_argument("--session")
    ctx.add_argument("--lines", type=int, default=60)
    ctx.add_argument("--json", action="store_true")

    rp = sub.add_parser("reply", help="freeform send text (+ Enter to submit)")
    rp.add_argument("target")
    rp.add_argument("text")
    rp.add_argument("--session")
    rp.add_argument(
        "--no-submit",
        action="store_true",
        help="type only; do not press Enter (default submits)",
    )

    ky = sub.add_parser("keys", help="send keys")
    ky.add_argument("target")
    ky.add_argument("keys", nargs="+")
    ky.add_argument("--session")

    an = sub.add_parser("answer", help="bound delivery by event_id")
    an.add_argument("event_id")
    an.add_argument("--text")
    an.add_argument("--keys", nargs="+")

    sk = sub.add_parser("skip", help="skip bound event")
    sk.add_argument("event_id")

    tts = sub.add_parser("tts", help="text-to-speech")
    tts.add_argument("text", nargs="+")
    tts.add_argument("--provider")
    tts.add_argument("--voice")
    tts.add_argument("--no-play", action="store_true")
    tts.add_argument("--out", type=Path)
    tts.add_argument("--json", action="store_true")
    tts.add_argument(
        "--listen",
        action="store_true",
        help="after TTS plays, start recording (cue + STT) for a user reply",
    )
    tts.add_argument(
        "--listen-for-user-response",
        action="store_true",
        dest="listen",
        help="alias of --listen",
    )
    tts.add_argument(
        "--end-mode",
        choices=("silence", "radio"),
        default=None,
        help="with --listen: how the reply capture ends (default: config)",
    )
    tts.add_argument(
        "--event-id",
        default=None,
        help="with --listen: tag the captured reply with the blocked event it answers "
        "(echoed as for_event so a reply is never associated with the wrong pane)",
    )

    li = sub.add_parser("listen", help="speech-to-text")
    li.add_argument("--provider")
    li.add_argument("--end-mode", choices=("silence", "radio"))
    li.add_argument("--json", action="store_true")
    li.add_argument(
        "--event-id",
        default=None,
        help="tag the captured reply with the blocked event it answers (echoed as for_event)",
    )

    le = sub.add_parser(
        "listen-end",
        help="finish or cancel an active listen (agent control from partials)",
    )
    le.add_argument("--stream-id", default=None, help="target stream_id from partial")
    le.add_argument(
        "--cancel",
        action="store_true",
        help="cancel instead of finalize as complete prompt",
    )
    le.add_argument("--reason", default=None, help="optional note (logged)")
    le.add_argument("--json", action="store_true")

    ask = sub.add_parser("ask", help="speak prompt + listen")
    ask.add_argument("text", nargs="+")
    ask.add_argument("--confirm", choices=("auto", "always", "never"))
    ask.add_argument("--end-mode", choices=("silence", "radio"))
    ask.add_argument("--provider")
    ask.add_argument("--json", action="store_true")
    ask.add_argument(
        "--event-id",
        default=None,
        help="tag the captured reply with the blocked event it answers (echoed as for_event)",
    )

    amb = sub.add_parser(
        "ambient",
        help="wake-phrase ambient (default: continuous loop)",
    )
    amb.add_argument(
        "--once",
        action="store_true",
        help="single wake cycle then exit (default is --loop)",
    )
    amb.add_argument(
        "--loop",
        action="store_true",
        default=True,
        help="continuous ambient (default)",
    )
    amb.add_argument("--timeout", type=float, default=None)
    amb.add_argument("--no-announce", action="store_true")
    amb.add_argument("--json", action="store_true")

    we = sub.add_parser(
        "wake-enroll",
        help="beep-paced wake enrollment samples (I006)",
    )
    we.add_argument(
        "--phrase",
        default=None,
        help='activation phrase to practice (default: first configured, e.g. "hey iris")',
    )
    we.add_argument(
        "--count",
        type=int,
        default=7,
        help="number of good samples to keep (default 7, range 5–10)",
    )
    we.add_argument(
        "--min",
        dest="min_count",
        type=int,
        default=5,
        help="minimum accepted samples for success exit (default 5)",
    )
    we.add_argument(
        "--no-learn",
        action="store_true",
        help="do not seed wake_learned aliases from scores",
    )
    we.add_argument(
        "--no-score",
        action="store_true",
        help="skip wake-backend scoring of samples",
    )
    we.add_argument(
        "--dry-run",
        action="store_true",
        help="play beeps / print loop without mic or files",
    )
    we.add_argument(
        "--no-beep",
        action="store_true",
        help="suppress enrollment beeps (print-only pacing)",
    )

    q = sub.add_parser("queue", help="pending bound events")
    q.add_argument("--json", action="store_true")
    q.add_argument(
        "--announce",
        action="store_true",
        help="speak the waiting-agent count by TTS when more than one is waiting",
    )
    q.add_argument(
        "--all",
        action="store_true",
        help="include stale/superseded/aged-out pending events (default: fresh only)",
    )
    q.add_argument(
        "--prune",
        action="store_true",
        help="expire stale/superseded/aged-out pending events (B101)",
    )
    q.add_argument(
        "--live",
        action="store_true",
        help="with --prune: expire events whose pane is gone or not blocked (also default soft-filter)",
    )
    q.add_argument(
        "--offline",
        action="store_true",
        help="skip Herdr live checks; age/supersede filter only",
    )
    q.add_argument(
        "--max-age",
        type=float,
        default=None,
        metavar="SEC",
        help="max age for fresh queue items in seconds (default 4h / HARK_QUEUE_MAX_AGE_S)",
    )

    prov = sub.add_parser("providers", help="list speech providers or voices")
    prov.add_argument(
        "test_name",
        nargs="?",
        help="provider name, or 'voices' to list TTS voices",
    )
    prov.add_argument("--json", action="store_true")

    dev = sub.add_parser("devices", help="list audio devices")
    dev.add_argument("--json", action="store_true")

    sub.add_parser("mute", help="mute system default mic (pactl)")
    sub.add_parser("unmute", help="unmute system default mic (pactl + ALSA Wave)")
    ms = sub.add_parser(
        "mute-sync",
        help="sync hardware unmute → OS (one-shot or watch loop)",
    )
    ms.add_argument(
        "--watch",
        action="store_true",
        help="run background poller (also started with ambient by default)",
    )
    ms.add_argument(
        "--once",
        action="store_true",
        help="force ensure_unmuted now and exit (default if no --watch)",
    )

    stt_stats = sub.add_parser("stats", help="TTS/STT usage stats")
    stt_stats.add_argument("--json", action="store_true")
    stt_stats.add_argument("--reset", action="store_true", help="delete usage log")

    logs = sub.add_parser("logs", help="unified system log (ambient+tts+stt+…); raw follow with -f")
    logs.add_argument("-n", "--lines", type=int, default=40)
    logs.add_argument("--json", action="store_true")
    logs.add_argument(
        "--follow",
        "-f",
        action="store_true",
        help="follow system.jsonl (like tail -f); for colorful human view use watch-logs",
    )
    logs.add_argument(
        "--path",
        action="store_true",
        help="print log path only",
    )

    wl = sub.add_parser(
        "watch-logs",
        help="live colorful human-readable logs (system.jsonl; optional ambient/watch)",
    )
    wl.add_argument(
        "-n",
        "--lines",
        type=int,
        default=40,
        help="show last N lines before follow (default 40; 0=none)",
    )
    wl.add_argument(
        "-f",
        "--follow",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="follow new lines (default: on; use --no-follow for snapshot)",
    )
    wl.add_argument(
        "--no-color",
        action="store_true",
        help="disable ANSI colors (also when stdout is not a TTY, or NO_COLOR)",
    )
    wl.add_argument(
        "--all",
        action="store_true",
        dest="all_logs",
        help="also follow ambient.jsonl and watch.jsonl under the state dir",
    )
    wl.add_argument(
        "--path",
        action="store_true",
        help="print log path(s) only",
    )

    # Handsfree always-on workers (ambient + watch); preferred over run-mode-a.sh
    from hark.workers import add_lifecycle_parsers

    add_lifecycle_parsers(sub)

    dae = sub.add_parser(
        "daemon",
        help="experimental harkd scaffold (not required for handsfree v1; see docs/HARKD.md)",
    )
    dae_sub = dae.add_subparsers(dest="daemon_cmd", required=True)
    dae_start = dae_sub.add_parser(
        "start", help="foreground supervisor (single-instance pidfile)"
    )
    dae_start.add_argument(
        "--workers",
        action="store_true",
        help="also supervise ambient + watch (same pieces as run-mode-a.sh)",
    )
    dae_start.add_argument(
        "--no-watch", action="store_true", help="with --workers: skip watch"
    )
    dae_start.add_argument(
        "--no-ambient", action="store_true", help="with --workers: skip ambient"
    )
    dae_start.add_argument("--session", default="default")
    dae_status = dae_sub.add_parser("status", help="harkd / workers / locks")
    dae_status.add_argument("--json", action="store_true")
    dae_stop = dae_sub.add_parser("stop", help="SIGTERM via harkd.pid")
    dae_stop.add_argument("--force", action="store_true")
    dae_stop.add_argument("--timeout", type=float, default=15.0)
    dae_stop.add_argument("--json", action="store_true")

    # Antigravity (agy) agentapi — handsfree wake without native Monitor (B049)
    api = sub.add_parser(
        "agentapi",
        help=(
            "experimental Antigravity (agy) agentapi wake/deliver "
            "(see docs/AGY.md)"
        ),
    )
    api_sub = api.add_subparsers(dest="agentapi_cmd", required=True)
    api_reg = api_sub.add_parser(
        "register",
        help=(
            "persist ANTIGRAVITY_LS_ADDRESS + conversation id "
            f"to ~/.local/state/hark/agy-env.json"
        ),
    )
    api_reg.add_argument(
        "--ls-address",
        default=None,
        help="override LS HTTP address (default: env ANTIGRAVITY_LS_ADDRESS)",
    )
    api_reg.add_argument(
        "--conversation",
        default=None,
        dest="conversation_id",
        help="override conversation id (default: env ANTIGRAVITY_CONVERSATION_ID)",
    )
    api_reg.add_argument(
        "--path",
        type=Path,
        default=None,
        help="override agy-env.json path",
    )
    api_reg.add_argument("--json", action="store_true")
    api_st = api_sub.add_parser("status", help="show registered / process agy env")
    api_st.add_argument("--path", type=Path, default=None)
    api_st.add_argument("--json", action="store_true")
    api_st.add_argument(
        "--agy-bin",
        default="agy",
        help="agy binary name/path (default: agy)",
    )
    api_send = api_sub.add_parser(
        "send",
        help="inject one message into the registered conversation (wake)",
    )
    api_send.add_argument("content", nargs="+", help="message body")
    api_send.add_argument("--title", default=None)
    api_send.add_argument("--path", type=Path, default=None)
    api_send.add_argument("--agy-bin", default="agy")
    api_send.add_argument("--timeout", type=float, default=60.0)
    api_send.add_argument(
        "--dry-run",
        action="store_true",
        help="print argv only; do not call agy",
    )
    api_send.add_argument("--json", action="store_true")
    api_send.add_argument(
        "--raw",
        action="store_true",
        help="do not wrap with the Hark wake preamble",
    )
    api_del = api_sub.add_parser(
        "deliver",
        help=(
            "handsfree sidecar: read monitor lines and inject via agentapi "
            "(stdin or --follow-monitor)"
        ),
    )
    api_del.add_argument(
        "--follow-monitor",
        action="store_true",
        help="spawn `hark monitor --for-monitor` and inject each HEP line",
    )
    api_del.add_argument(
        "--stdin",
        action="store_true",
        help="read NDJSON lines from stdin (default if not --follow-monitor)",
    )
    api_del.add_argument("--title", default=None)
    api_del.add_argument("--path", type=Path, default=None)
    api_del.add_argument("--agy-bin", default="agy")
    api_del.add_argument("--timeout", type=float, default=60.0)
    api_del.add_argument("--dry-run", action="store_true")
    api_del.add_argument(
        "--raw",
        action="store_true",
        help="do not wrap lines with the Hark wake preamble",
    )
    api_del.add_argument(
        "--replay",
        type=int,
        default=0,
        help="with --follow-monitor: pass --replay N to hark monitor",
    )
    api_del.add_argument(
        "--stop-on-error",
        action="store_true",
        help="exit on first failed inject",
    )
    api_del.add_argument("--json", action="store_true")

    # I005 / B057 — voice-spawn surfaces (handsfree tools)
    sess = sub.add_parser(
        "session",
        help="list or ensure Herdr named sessions (I005)",
    )
    sess_sub = sess.add_subparsers(dest="session_cmd", required=True)
    sess_list = sess_sub.add_parser("list", help="list Herdr sessions")
    sess_list.add_argument("--json", action="store_true")
    sess_ens = sess_sub.add_parser(
        "ensure",
        help="ensure a named Herdr session is running (start headless if needed)",
    )
    sess_ens.add_argument("name", help="Herdr session name (e.g. default, swarm)")
    sess_ens.add_argument(
        "--no-start",
        action="store_true",
        help="only look up; do not start a missing/stopped session",
    )
    sess_ens.add_argument("--json", action="store_true")

    ags = sub.add_parser(
        "agent-start",
        help=(
            "start a coding agent in Herdr (resolves CLI aliases; optional kickoff prompt)"
        ),
    )
    ags.add_argument(
        "agent",
        help=(
            "catalog agent (claude/cc, codex/cx, grok/gk, cursor-agent/cr, …) "
            "or binary name; with --adhoc treat as free-form command"
        ),
    )
    ags.add_argument(
        "--session",
        default=None,
        help="Hark/Herdr session id (default: first configured session)",
    )
    ags.add_argument(
        "--herdr-session",
        default=None,
        dest="herdr_session",
        help="named Herdr session to ensure/start into (optional)",
    )
    ags.add_argument("--cwd", default=None, help="working directory for the agent pane")
    ags.add_argument(
        "--name",
        default=None,
        dest="pane_name",
        help="Herdr agent/pane label (default: agent key)",
    )
    ags.add_argument("--workspace", default=None, help="Herdr workspace id")
    ags.add_argument("--tab", default=None, help="Herdr tab id")
    ags.add_argument("--split", choices=("right", "down"), default=None)
    ags.add_argument(
        "--focus",
        action="store_true",
        help="focus the new pane (default: --no-focus)",
    )
    ags.add_argument(
        "--prompt",
        default=None,
        help="kickoff text to send after start (submits with Enter)",
    )
    ags.add_argument(
        "--adhoc",
        action="store_true",
        help="treat AGENT as an ad-hoc command (no catalog alias table)",
    )
    ags.add_argument(
        "extra",
        nargs="*",
        help="extra args after the resolved CLI (or full argv tail)",
    )
    ags.add_argument("--json", action="store_true")

    return p


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # B109: piped stdout is fully buffered by default — stream line-oriented
    # HEP / status so Monitor, grep, and tail -f see progress. Also warn when
    # interactive commands are piped (agents often append `| tail` which waits
    # for EOF and looks hung).
    try:
        from hark.stdio import configure_stdio, maybe_warn_non_tty_stdout

        configure_stdio()
        maybe_warn_non_tty_stdout(argv)
    except Exception:
        pass
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return int(code) if isinstance(code, int) else USAGE

    cfg = load_config(getattr(args, "config_path", None))
    for warning in cfg.warnings:
        eprint(f"hark config warning: {warning}")
    try:
        from hark.audio.cues import configure_cues_from_config

        configure_cues_from_config(cfg)
    except Exception:
        pass
    try:
        return dispatch(args, cfg)
    except HerdrError as exc:
        eprint(f"hark: herdr error: {exc}")
        return HERDR
    except ProviderError as exc:
        eprint(f"hark: provider: {exc}")
        return getattr(exc, "code", PROVIDER) or PROVIDER
    except TimeoutError as exc:
        eprint(f"hark: timeout: {exc}")
        return TIMEOUT
    except ValueError as exc:
        eprint(f"hark: {exc}")
        return USAGE
    except FileExistsError as exc:
        eprint(f"hark: {exc}")
        return ERROR
    except RuntimeError as exc:
        msg = str(exc)
        eprint(f"hark: {msg}")
        if "mic" in msg.lower() or "sounddevice" in msg.lower() or "audio" in msg.lower():
            return AUDIO
        return ERROR


def dispatch(args: argparse.Namespace, cfg) -> int:
    cmd = args.cmd

    if cmd == "doctor":
        return run_doctor(cfg, as_json=bool(args.json))

    if cmd == "setup":
        from hark.setup_flow import cmd_setup

        return cmd_setup(args)

    if cmd == "config":
        if args.config_cmd == "path":
            print(default_config_path())
            return OK
        if args.config_cmd == "init":
            path = write_default_config(force=bool(args.force))
            print(f"wrote {path}")
            state_dir().mkdir(parents=True, exist_ok=True)
            return OK
        if args.config_cmd == "show":
            print(json.dumps(config_to_dict(cfg), indent=2))
            return OK
        return USAGE

    if cmd == "status":
        return cmd_status(args, cfg)
    if cmd == "watch":
        statuses = None
        if args.statuses:
            statuses = [s.strip() for s in args.statuses.split(",") if s.strip()]
        return run_watch(
            cfg,
            session_ids=args.sessions,
            statuses=statuses,
            for_monitor=bool(args.for_monitor),
            transport=args.transport,
            once=bool(args.once),
            read_questions=bool(args.read_questions),
        )
    if cmd == "monitor":
        from hark.monitor_feed import MODE_A_WAKE_KINDS, run_monitor

        kinds = MODE_A_WAKE_KINDS
        if getattr(args, "kinds", None):
            kinds = frozenset(
                s.strip() for s in str(args.kinds).split(",") if s.strip()
            )
        for_monitor = bool(args.for_monitor) and not bool(getattr(args, "full", False))
        return run_monitor(
            for_monitor=for_monitor,
            kinds=kinds,
            replay=int(getattr(args, "replay", 0) or 0),
            allow_multiple=bool(getattr(args, "allow_multiple", False)),
        )
    if cmd in ("webui", "dashboard", "serve"):
        if getattr(args, "print_token", False):
            import secrets as _secrets

            token = _secrets.token_urlsafe(32)
            print(token)
            eprint('add to ~/.config/hark/config.toml:\n[dashboard]\ntoken = "%s"' % token)
            return OK
        from hark.dashboard.server import run_serve

        return run_serve(cfg, host=args.host, port=args.port)
    if cmd == "context":
        return cmd_context(args, cfg)
    if cmd == "reply":
        return cmd_reply(args, cfg)
    if cmd == "keys":
        return cmd_keys(args, cfg)
    if cmd == "answer":
        return cmd_answer(args, cfg)
    if cmd == "skip":
        return cmd_skip(args)
    if cmd == "tts":
        return cmd_tts(args, cfg)
    if cmd == "listen":
        return cmd_listen(args, cfg)
    if cmd == "listen-end":
        return cmd_listen_end(args)
    if cmd == "ask":
        return cmd_ask(args, cfg)
    if cmd == "ambient":
        return cmd_ambient(args, cfg)
    if cmd == "wake-enroll":
        from hark.wake_enroll import cmd_wake_enroll

        return cmd_wake_enroll(args, cfg)
    if cmd == "queue":
        return cmd_queue(args, cfg)
    if cmd == "providers":
        return cmd_providers(args)
    if cmd == "devices":
        return cmd_devices(args)
    if cmd == "mute":
        return cmd_mic_mute(True)
    if cmd == "unmute":
        return cmd_mic_mute(False)
    if cmd == "mute-sync":
        return cmd_mute_sync(args)
    if cmd == "stats":
        return cmd_stats(args)
    if cmd == "logs":
        return cmd_logs(args)
    if cmd == "watch-logs":
        return cmd_watch_logs(args)
    if cmd == "start":
        from hark.workers import cmd_start

        return cmd_start(args)
    if cmd == "stop":
        from hark.workers import cmd_stop

        return cmd_stop(args)
    if cmd == "restart":
        from hark.workers import cmd_restart

        return cmd_restart(args)
    if cmd == "daemon":
        from hark.daemon import dispatch_daemon

        return dispatch_daemon(args)
    if cmd == "agentapi":
        return cmd_agentapi(args)
    if cmd == "session":
        return cmd_session(args, cfg)
    if cmd == "agent-start":
        return cmd_agent_start(args, cfg)
    return USAGE


def cmd_session(args: argparse.Namespace, cfg) -> int:
    """``hark session list|ensure`` (I005 / B057)."""
    client = _client_for(cfg, (cfg.sessions[0].id if cfg.sessions else "local"))
    sub = getattr(args, "session_cmd", None)
    if sub == "list":
        rows = client.list_sessions()
        payload = [
            {
                "name": s.name,
                "running": s.running,
                "default": s.default,
                "session_dir": s.session_dir,
                "socket_path": s.socket_path,
            }
            for s in rows
        ]
        if args.json:
            print(json.dumps({"sessions": payload}, indent=2))
        else:
            if not payload:
                print("(no herdr sessions)")
            for s in payload:
                mark = "running" if s["running"] else "stopped"
                dflt = " (default)" if s["default"] else ""
                print(f"{s['name']}{dflt}: {mark}  sock={s['socket_path'] or '?'}")
        return OK
    if sub == "ensure":
        info = client.ensure_session(
            str(args.name),
            start=not bool(getattr(args, "no_start", False)),
        )
        payload = {
            "name": info.name,
            "running": info.running,
            "default": info.default,
            "session_dir": info.session_dir,
            "socket_path": info.socket_path,
        }
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(
                f"session {info.name}: "
                f"{'running' if info.running else 'stopped'} "
                f"sock={info.socket_path or '?'}"
            )
        return OK
    return USAGE


def cmd_agent_start(args: argparse.Namespace, cfg) -> int:
    """``hark agent-start`` — resolve CLI, start in Herdr, optional kickoff (B057)."""
    from hark.agents.resolve import ResolveError, resolve_adhoc_argv, resolve_agent_argv

    session_id = (
        args.session
        or (cfg.sessions[0].id if cfg.sessions else None)
        or "local"
    )
    client = _client_for(cfg, session_id)

    herdr_sess = getattr(args, "herdr_session", None)
    if herdr_sess:
        client.ensure_session(str(herdr_sess))
        # Re-bind client to the named herdr session socket when possible
        client = HerdrClient(SessionConfig(id=str(herdr_sess)))
        session_id = str(herdr_sess)

    overrides = getattr(cfg, "agents", None)
    override_map = None
    prefer = True
    if overrides is not None:
        prefer = bool(getattr(overrides, "prefer_aliases", True))
        override_map = dict(getattr(overrides, "cli", {}) or {})

    extra = list(getattr(args, "extra", None) or [])
    try:
        if bool(getattr(args, "adhoc", False)):
            resolved = resolve_adhoc_argv(str(args.agent), extra_args=extra)
        else:
            try:
                resolved = resolve_agent_argv(
                    str(args.agent),
                    extra_args=extra,
                    overrides=override_map,
                    prefer_aliases=prefer,
                )
            except ResolveError:
                # Fall back to ad-hoc when token is an unknown PATH binary
                resolved = resolve_adhoc_argv(str(args.agent), extra_args=extra)
    except ResolveError as exc:
        eprint(f"hark agent-start: {exc}")
        return USAGE

    pane_name = getattr(args, "pane_name", None) or resolved.agent_key
    agent = client.start_agent(
        pane_name,
        resolved.argv,
        cwd=getattr(args, "cwd", None),
        workspace_id=getattr(args, "workspace", None),
        tab_id=getattr(args, "tab", None),
        split=getattr(args, "split", None),
        focus=bool(getattr(args, "focus", False)),
    )

    prompt = getattr(args, "prompt", None)
    kicked = False
    if prompt:
        client.send_text(agent.pane_id, str(prompt), submit=True)
        kicked = True

    payload = {
        "session_id": agent.session_id,
        "pane_id": agent.pane_id,
        "target": agent.target,
        "agent": agent.agent,
        "status": agent.status,
        "cwd": agent.cwd,
        "argv": resolved.argv,
        "source": resolved.source,
        "agent_key": resolved.agent_key,
        "kickoff": kicked,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(
            f"started {resolved.agent_key} → {agent.target} "
            f"(source={resolved.source}, argv0={resolved.command})"
        )
        if kicked:
            print(f"  kickoff prompt sent to {agent.pane_id}")
    return OK


def cmd_agentapi(args: argparse.Namespace) -> int:
    """Antigravity agentapi register / status / send / deliver (B049)."""
    from hark.agentapi import (
        DEFAULT_TITLE,
        AgyEnv,
        deliver_line,
        follow_monitor_and_deliver,
        format_wake_payload,
        resolve_agy_env_from_environ,
        send_message,
        status_dict,
        write_agy_env,
    )

    sub = getattr(args, "agentapi_cmd", None)
    if sub == "register":
        ls = getattr(args, "ls_address", None)
        conv = getattr(args, "conversation_id", None)
        if not ls or not conv:
            from_env = resolve_agy_env_from_environ()
            if from_env is None and (not ls or not conv):
                eprint(
                    "hark agentapi register: need --ls-address and --conversation, "
                    "or both ANTIGRAVITY_LS_ADDRESS and ANTIGRAVITY_CONVERSATION_ID"
                )
                return USAGE
            if from_env is not None:
                ls = ls or from_env.ls_address
                conv = conv or from_env.conversation_id
        try:
            env = AgyEnv(ls_address=str(ls), conversation_id=str(conv)).normalized()
        except ValueError as exc:
            eprint(f"hark agentapi register: {exc}")
            return USAGE
        path = write_agy_env(env, path=getattr(args, "path", None))
        payload = {
            "path": str(path),
            "ls_address": env.ls_address,
            "conversation_id": env.conversation_id,
        }
        if args.json:
            print(json.dumps(payload))
        else:
            print(f"wrote {path}")
            print(f"  ls_address={env.ls_address}")
            print(f"  conversation_id={env.conversation_id}")
        return OK

    if sub == "status":
        info = status_dict(
            path=getattr(args, "path", None),
            agy_bin=str(getattr(args, "agy_bin", "agy") or "agy"),
        )
        if args.json:
            print(json.dumps(info, indent=2))
            return OK
        print(f"env_path: {info['env_path']}")
        print(f"agy: {info.get('agy_path') or '(not found)'}")
        for label in ("file", "process", "resolved"):
            val = info.get(label)
            if val:
                print(f"{label}: ls={val['ls_address']} conv={val['conversation_id']}")
            else:
                print(f"{label}: (none)")
        return OK if info.get("resolved") else ERROR

    if sub == "send":
        content = " ".join(args.content)
        wrap = not bool(getattr(args, "raw", False))
        body = format_wake_payload(content) if wrap else content
        title = getattr(args, "title", None) or DEFAULT_TITLE
        result = send_message(
            body,
            path=getattr(args, "path", None),
            title=title,
            agy_bin=str(getattr(args, "agy_bin", "agy") or "agy"),
            timeout=float(getattr(args, "timeout", 60.0) or 60.0),
            dry_run=bool(getattr(args, "dry_run", False)),
        )
        out = {
            "ok": result.ok,
            "dry_run": result.dry_run,
            "argv": result.argv,
            "returncode": result.returncode,
            "error": result.error,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
        if args.json:
            print(json.dumps(out, indent=2))
        else:
            if result.dry_run:
                print("dry-run argv:", " ".join(result.argv))
            elif result.ok:
                print("sent ok")
            else:
                eprint(f"hark agentapi send: {result.error}")
                if result.stderr:
                    eprint(result.stderr.rstrip())
        return OK if result.ok else ERROR

    if sub == "deliver":
        follow = bool(getattr(args, "follow_monitor", False))
        title = getattr(args, "title", None) or DEFAULT_TITLE
        wrap = not bool(getattr(args, "raw", False))
        dry = bool(getattr(args, "dry_run", False))
        path = getattr(args, "path", None)
        agy_bin = str(getattr(args, "agy_bin", "agy") or "agy")
        timeout = float(getattr(args, "timeout", 60.0) or 60.0)
        stop = bool(getattr(args, "stop_on_error", False))

        if follow:
            return follow_monitor_and_deliver(
                path=path,
                title=title,
                agy_bin=agy_bin,
                timeout=timeout,
                dry_run=dry,
                wrap=wrap,
                replay=int(getattr(args, "replay", 0) or 0),
                stop_on_error=stop,
            )

        # stdin (default)
        results = []
        for line in sys.stdin:
            r = deliver_line(
                line,
                path=path,
                title=title,
                agy_bin=agy_bin,
                timeout=timeout,
                dry_run=dry,
                wrap=wrap,
            )
            if r is None:
                continue
            results.append(r)
            if not r.ok:
                eprint(f"hark agentapi deliver: {r.error}")
                if stop:
                    return ERROR
            elif dry:
                print("dry-run argv:", " ".join(r.argv), file=sys.stderr)
            else:
                print("sent ok", file=sys.stderr)
        if args.json:
            print(
                json.dumps(
                    [
                        {
                            "ok": r.ok,
                            "dry_run": r.dry_run,
                            "error": r.error,
                            "argv": r.argv,
                        }
                        for r in results
                    ],
                    indent=2,
                )
            )
        if not results:
            return OK
        return OK if all(r.ok for r in results) else ERROR

    return USAGE


def cmd_mic_mute(mute: bool) -> int:
    from hark.audio.mic_mute import (
        default_source,
        ensure_unmuted,
        set_source_mute,
        source_is_muted,
    )

    src = default_source()
    if not src:
        eprint("hark: no default Pulse/PipeWire source")
        return AUDIO
    if not mute:
        # Full cascade: Pulse + ALSA Wave + release TTS hold
        result = ensure_unmuted(source=src)
        print(json.dumps({"ok": True, "muted": False, "source": src, **result}))
        return OK
    if not set_source_mute(src, mute):
        eprint(f"hark: failed to mute {src}")
        return AUDIO
    state = source_is_muted(src)
    print(
        json.dumps(
            {
                "ok": True,
                "source": src,
                "muted": state if state is not None else mute,
            }
        )
    )
    return OK


def cmd_mute_sync(args: argparse.Namespace) -> int:
    """Force OS unmute cascade, or run hardware→OS mute sync watcher."""
    from hark.audio.mic_mute import (
        alsa_mic_capture_on,
        default_source,
        ensure_unmuted,
        find_wave_alsa_card,
        source_is_muted,
        start_mute_sync_watcher,
    )

    if args.watch:
        ok = start_mute_sync_watcher(enabled=True)
        src = default_source()
        print(
            json.dumps(
                {
                    "ok": ok,
                    "watching": True,
                    "source": src,
                    "wave_card": find_wave_alsa_card(),
                    "pulse_muted": source_is_muted(src) if src else None,
                    "alsa_capture_on": alsa_mic_capture_on(),
                }
            )
        )
        # Keep process alive for standalone watcher
        try:
            import time as _time

            while True:
                _time.sleep(3600)
        except KeyboardInterrupt:
            return OK
        return OK

    result = ensure_unmuted()
    src = default_source()
    print(
        json.dumps(
            {
                "ok": True,
                "source": src,
                "pulse_muted": source_is_muted(src) if src else None,
                "alsa_capture_on": alsa_mic_capture_on(),
                "wave_card": find_wave_alsa_card(),
                **result,
            }
        )
    )
    return OK


def cmd_stats(args: argparse.Namespace) -> int:
    from hark.usage import UsageStore

    store = UsageStore()
    if args.reset:
        if store.path.is_file():
            store.path.unlink()
        print(json.dumps({"ok": True, "reset": True, "path": str(store.path)}))
        return OK
    summary = store.summary()
    if args.json:
        print(json.dumps(summary, indent=2))
        return OK
    print(f"usage log: {summary['path']}  ({summary['total_events']} events)")
    for kind in ("tts", "stt"):
        a = summary[kind]
        print(f"\n{kind.upper()}")
        print(f"  instances:     {a['instances']}  (ok={a['ok']} err={a['errors']})")
        print(f"  total chars:   {a['total_chars']}  (avg {a['avg_chars']})")
        print(f"  total words:   {a['total_words']}  (avg {a['avg_words']})")
        print(
            f"  total audio:   {a['total_audio_s']}s  "
            f"({a['total_audio_ms']} ms, avg {a['avg_audio_ms']} ms)"
        )
        print(f"  total latency: {a['total_latency_ms']} ms  (avg {a['avg_latency_ms']})")
        if a["by_provider"]:
            print(f"  by provider:   {a['by_provider']}")
    return OK


def cmd_logs(args: argparse.Namespace) -> int:
    from hark.syslog import system_log_path, tail_lines

    path = system_log_path()
    if args.path:
        print(path)
        return OK
    if args.follow:
        import time

        print(f"# {path}", file=sys.stderr)
        # print last N then follow
        for rec in tail_lines(args.lines, path):
            _print_log_rec(rec, as_json=args.json)
        pos = path.stat().st_size if path.is_file() else 0
        try:
            while True:
                if path.is_file():
                    size = path.stat().st_size
                    if size > pos:
                        with path.open("r", encoding="utf-8") as fh:
                            fh.seek(pos)
                            for line in fh:
                                line = line.strip()
                                if not line:
                                    continue
                                try:
                                    rec = json.loads(line)
                                except json.JSONDecodeError:
                                    continue
                                _print_log_rec(rec, as_json=args.json)
                            pos = fh.tell()
                    elif size < pos:
                        pos = 0  # rotated
                time.sleep(0.25)
        except KeyboardInterrupt:
            return OK
    rows = tail_lines(args.lines, path)
    if not rows:
        eprint(f"hark logs: empty or missing ({path})")
        return OK
    for rec in rows:
        _print_log_rec(rec, as_json=args.json)
    return OK


def _print_log_rec(rec: dict, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(rec, separators=(",", ":")))
        return
    ts = rec.get("ts")
    try:
        from datetime import datetime, timezone

        tss = datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%H:%M:%S")
    except Exception:
        tss = str(ts)
    comp = rec.get("component", "?")
    ev = rec.get("event", "?")
    msg = rec.get("message") or ""
    data = rec.get("data") or {}
    extras = ""
    if data:
        # compact key highlights
        bits = []
        for k in ("provider", "voice", "rms", "last_text", "phrase", "audio_ms", "chars"):
            if k in data and data[k] not in (None, ""):
                bits.append(f"{k}={data[k]}")
        if bits:
            extras = "  " + " ".join(bits)
    print(f"{tss}  {comp:8}  {ev:20}  {msg}{extras}")


def cmd_watch_logs(args: argparse.Namespace) -> int:
    """Live colorful human-readable log viewer (B041)."""
    from hark.logview import (
        follow_pretty,
        header_line,
        resolve_sources,
        tail_pretty,
        use_color,
    )

    sources = resolve_sources(include_all=bool(getattr(args, "all_logs", False)))
    if args.path:
        for label, path in sources:
            print(f"{label}\t{path}" if len(sources) > 1 else str(path))
        return OK

    color = use_color(stream=sys.stdout, no_color=bool(args.no_color))
    print(header_line(sources, color=color), file=sys.stderr)

    lines = tail_pretty(sources, args.lines, color=color)
    for line in lines:
        print(line, flush=True)

    if not args.follow:
        if not lines:
            eprint("hark watch-logs: empty or missing log(s)")
        return OK

    return follow_pretty(sources, color=color)


def _client_for(cfg, session_id: str) -> HerdrClient:
    session = cfg.session_by_id(session_id) or SessionConfig(id=session_id)
    return HerdrClient(session)


def cmd_status(args: argparse.Namespace, cfg) -> int:
    sessions = cfg.sessions
    if args.sessions:
        want = set(args.sessions)
        sessions = [s for s in cfg.sessions if s.id in want] or [
            SessionConfig(id=sid) for sid in args.sessions
        ]
    rows = []
    errors = []
    any_ok = False
    for session in sessions:
        client = HerdrClient(session)
        try:
            agents = client.list_agents()
            any_ok = True
        except HerdrError as exc:
            errors.append({"session_id": session.id, "error": str(exc)})
            continue
        for a in agents:
            if args.filter_status and a.status != args.filter_status:
                continue
            rows.append(
                {
                    "session_id": a.session_id,
                    "pane_id": a.pane_id,
                    "target": a.target,
                    "agent": a.agent,
                    "status": a.status,
                    "revision": a.revision,
                    "cwd": a.cwd,
                    "focused": a.focused,
                }
            )
    if args.json:
        print(json.dumps({"agents": rows, "errors": errors}, indent=2))
    else:
        for r in rows:
            print(
                f"{r['target']:16}  {r['status']:8}  "
                f"{r['agent'] or '-':8}  rev={r['revision']}"
            )
        for e in errors:
            eprint(f"warn: {e['session_id']}: {e['error']}")
    return OK if any_ok else HERDR


def cmd_context(args: argparse.Namespace, cfg) -> int:
    target = parse_target(args.target, default_session=args.session or "local")
    text = _client_for(cfg, target.session_id).read_pane(
        target.pane_id, lines=args.lines
    )
    if args.json:
        print(json.dumps({"target": str(target), "text": text}, indent=2))
    else:
        print(text)
    return OK


def cmd_reply(args: argparse.Namespace, cfg) -> int:
    target = parse_target(args.target, default_session=args.session or "local")
    submit = not bool(getattr(args, "no_submit", False))
    _client_for(cfg, target.session_id).send_text(
        target.pane_id, args.text, submit=submit
    )
    print(
        json.dumps(
            {
                "ok": True,
                "target": str(target),
                "mode": "reply",
                "submit": submit,
            }
        )
    )
    return OK


def cmd_keys(args: argparse.Namespace, cfg) -> int:
    target = parse_target(args.target, default_session=args.session or "local")
    _client_for(cfg, target.session_id).send_keys(target.pane_id, list(args.keys))
    print(json.dumps({"ok": True, "target": str(target), "keys": list(args.keys)}))
    return OK


_ANSWER_REJECT_HINTS = {
    "bad_request": "require exactly one of --text / --keys",
    "unknown_event": "unknown event_id (not in store); events are registered by `hark watch`; or use reply/keys",
    "already_delivered": "already delivered (idempotent refuse)",
    "missing_question_fingerprint": "bound event has no question fingerprint",
    "pane_gone": "pane no longer present",
    "not_blocked": "agent is no longer blocked",
    "stale_revision": "stale pane revision",
    "fingerprint_mismatch": "question fingerprint mismatch (stale)",
    "fingerprint_unavailable": "unable to verify question fingerprint",
}


def cmd_answer(args: argparse.Namespace, cfg) -> int:
    if not args.text and not args.keys:
        eprint("hark answer: require --text or --keys")
        return USAGE
    if args.text and args.keys:
        eprint("hark answer: use either --text or --keys")
        return USAGE

    from hark.answering import answer_bound_event

    result = answer_bound_event(
        args.event_id,
        text=args.text,
        keys=list(args.keys) if args.keys else None,
        store=DeliveryStore(),
        client_for=lambda session_id: _client_for(cfg, session_id),
    )
    if result.status == "rejected":
        hint = _ANSWER_REJECT_HINTS.get(
            result.reason or "", result.reason or "rejected"
        )
        eprint(f"hark answer: {hint}")
        return ABORT
    print(
        json.dumps(
            {
                "ok": result.ok,
                "event_id": result.event_id,
                "target": result.target,
                **({"status": result.status} if result.status != "delivered" else {}),
            }
        )
    )
    return OK


def cmd_skip(args: argparse.Namespace) -> int:
    store = DeliveryStore()
    store.mark(args.event_id, "skipped")
    print(json.dumps({"ok": True, "event_id": args.event_id, "status": "skipped"}))
    return OK


def cmd_tts(args: argparse.Namespace, cfg) -> int:
    from hark.speech import run_tts, speak_and_listen

    text = " ".join(args.text)
    want_listen = bool(getattr(args, "listen", False))
    if want_listen and args.no_play:
        eprint("hark tts: --listen requires playback (drop --no-play)")
        return USAGE

    if not want_listen:
        result = run_tts(
            cfg,
            text,
            provider=args.provider,
            voice=args.voice,
            play=not args.no_play,
            out=args.out,
        )
        if args.json or args.no_play or args.out:
            print(json.dumps(result))
        else:
            print(json.dumps({"ok": True, "provider": result["provider"]}))
        return OK

    # Auto-listen: half-duplex default, or overlap_prearm (see speak_and_listen)
    try:
        result, listened = speak_and_listen(
            cfg,
            text,
            provider=args.provider,
            voice=args.voice,
            end_mode=args.end_mode,
            out=args.out,
        )
    except Exception as exc:
        payload = {
            "ok": False,
            "tts": getattr(exc, "tts_info", None),
            "error": str(exc),
            "listen": None,
        }
        print(json.dumps(payload, indent=2 if args.json else None))
        return (
            TIMEOUT
            if "timeout" in str(exc).lower() or "empty" in str(exc).lower()
            else AUDIO
        )

    if listened.cancelled:
        eprint(f"cancelled via {listened.end_phrase!r}")
        print(
            json.dumps(
                {
                    "ok": False,
                    "cancelled": True,
                    "text": listened.text,
                    "tts": result,
                    "stream_id": listened.stream_id,
                },
                indent=2 if args.json else None,
            )
        )
        return ABORT

    out = {
        "ok": True,
        "tts": result,
        "text": listened.text,
        "meta_command": listened.meta_command,
        "for_event": getattr(args, "event_id", None),
        "listen": {
            "provider": listened.provider,
            "duration_ms": listened.duration_ms,
            "end_mode": listened.end_mode,
            "end_phrase": listened.end_phrase,
            "stream_id": listened.stream_id,
        },
    }
    print(json.dumps(out, indent=2 if args.json else None))
    return OK


def cmd_listen(args: argparse.Namespace, cfg) -> int:
    from hark.meta_commands import classify_meta_command
    from hark.speech import run_listen

    # Beep when listen is armed (same knob as ask/tts --listen), not only when speech opens.
    result = run_listen(
        cfg,
        provider=args.provider,
        end_mode=args.end_mode,
        arm_cue=bool(
            getattr(getattr(cfg, "audio", None), "answer_arm_cue", True)
        ),
    )
    if result.cancelled:
        eprint(f"cancelled via {result.end_phrase!r}")
        print(json.dumps({"ok": False, "cancelled": True, "text": result.text}))
        return ABORT
    if result.meta_command is None:
        result.meta_command = classify_meta_command(result.text)
    payload = {
        "ok": True,
        "text": result.text,
        "meta_command": result.meta_command,
        "for_event": getattr(args, "event_id", None),
        "provider": result.provider,
        "duration_ms": result.duration_ms,
        "end_mode": result.end_mode,
        "end_phrase": result.end_phrase,
        "stream_id": result.stream_id,
    }
    print(json.dumps(payload, indent=2 if args.json else None))
    return OK


def cmd_listen_end(args: argparse.Namespace) -> int:
    """Agent control: finish or cancel the active listen (from radio partials)."""
    from hark.listen_control import read_active, request_listen_action

    action = "cancel" if args.cancel else "finish"
    result = request_listen_action(
        action,
        stream_id=args.stream_id,
        reason=args.reason,
    )
    active = read_active()
    out = {**result, "active": active}
    print(json.dumps(out, indent=2 if args.json else None))
    if not result.get("ok"):
        eprint(f"hark listen-end: {result.get('error')}")
        return USAGE
    return OK


def cmd_ask(args: argparse.Namespace, cfg) -> int:
    from hark.speech import run_ask

    prompt = " ".join(args.text)
    result = run_ask(
        cfg,
        prompt,
        confirm=args.confirm,
        end_mode=args.end_mode,
        provider=args.provider,
    )
    result["for_event"] = getattr(args, "event_id", None)
    print(json.dumps(result, indent=2 if args.json else None))
    return int(result.get("exit", OK if result.get("ok") else ERROR))


def cmd_ambient(args: argparse.Namespace, cfg) -> int:
    from hark.ambient import ambient_event_line, run_ambient, run_ambient_loop

    cfg.ambient.enabled = True
    announce = not args.no_announce
    if args.once:
        result = run_ambient(
            cfg,
            once=True,
            timeout_s=args.timeout,
            announce=announce,
        )
        payload = ambient_event_line(result)
        payload = {k: v for k, v in payload.items() if v is not None}
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(json.dumps(payload, separators=(",", ":")))
        return OK if result.activated else TIMEOUT
    # continuous handsfree ambient
    return run_ambient_loop(cfg, announce=announce)


def _queue_live_answerable(cfg, ev: dict) -> tuple[bool, str]:
    """Return whether a bound event still looks answerable on Herdr (B101).

    Fail-soft on Herdr errors: keep the event rather than silently dropping it.
    """
    session_id = str(ev.get("session_id") or "")
    pane_id = str(ev.get("pane_id") or "")
    try:
        live = _client_for(cfg, session_id).get_agent(pane_id)
    except Exception as exc:  # noqa: BLE001 — fail-soft; leave pending
        return True, f"herdr_error:{exc}"
    if live is None:
        return False, "pane_gone"
    # Answerable when blocked (needs input). Idle/working panes are stale queue noise.
    if live.status != "blocked":
        return False, "not_blocked"
    rev = ev.get("pane_revision")
    if (
        isinstance(rev, int)
        and rev > 0
        and getattr(live, "revision", None) is not None
        and live.revision != rev
    ):
        return False, "stale_revision"
    return True, "ok"


def cmd_queue(args: argparse.Namespace, cfg=None) -> int:
    from hark.delivery import queue_max_age_s, summarize_pending

    store = DeliveryStore()
    max_age = getattr(args, "max_age", None)
    include_all = bool(getattr(args, "all", False))
    do_prune = bool(getattr(args, "prune", False))
    # Live check defaults on whenever we have config (trustworthy announce/list).
    # `--offline` skips Herdr. `--live` is accepted as an explicit alias for the
    # default live soft-filter / prune-with-live behavior.
    offline = bool(getattr(args, "offline", False))
    use_live = (not offline) and cfg is not None

    pruned: list[dict] = []
    if do_prune:
        # Age/supersede always expire. Live-unanswerable expire when Herdr is
        # reachable (default) or when --live was passed; --offline skips live.
        live_cb = (
            (lambda ev: _queue_live_answerable(cfg, ev))
            if use_live and cfg is not None
            else None
        )
        pruned = store.prune(max_age_s=max_age, is_answerable=live_cb)

    classified = store.classify_pending(max_age_s=max_age)
    fresh = list(classified["fresh"])
    stale = list(classified["stale"])

    # Soft live filter for display/announce (no write unless --prune above).
    if use_live and not include_all and cfg is not None:
        still_fresh: list[dict] = []
        for item in fresh:
            ok, reason = _queue_live_answerable(cfg, item)
            if ok:
                still_fresh.append(item)
            else:
                tagged = dict(item)
                tagged["_stale_reason"] = reason
                stale.append(tagged)
        fresh = still_fresh

    fresh_public = [
        {k: v for k, v in item.items() if not str(k).startswith("_")}
        for item in fresh
    ]
    if include_all:
        pending = store.pending_events(include_stale=True)
        summary = summarize_pending(pending, stale=None)
    else:
        pending = fresh_public
        summary = summarize_pending(fresh_public, stale=stale)
    count = summary["count"]

    announced = False
    # Announce only the fresh/answerable count (never inflate with stale).
    if getattr(args, "announce", False) and count > 1 and cfg is not None:
        from hark.speech import run_tts

        run_tts(cfg, summary["announcement"], play=True)
        announced = True

    if args.json:
        print(
            json.dumps(
                {
                    "queue": pending,
                    "count": count,
                    "targets": summary["targets"],
                    "stale_count": summary.get("stale_count", 0),
                    "stale_targets": summary.get("stale_targets", []),
                    "pruned": [
                        {
                            "event_id": p.get("event_id"),
                            "session_id": p.get("session_id"),
                            "pane_id": p.get("pane_id"),
                            "reason": p.get("_stale_reason"),
                        }
                        for p in pruned
                    ],
                    "max_age_s": queue_max_age_s(max_age),
                    "live": use_live and not include_all,
                    "announcement": summary["announcement"],
                    "announced": announced,
                },
                indent=2,
            )
        )
    else:
        if not pending:
            print("(empty)")
        for p in pending:
            print(f"{p.get('event_id')}  {p.get('session_id')}/{p.get('pane_id')}")
        if pruned:
            print(f"pruned {len(pruned)} stale event(s)")
        elif summary.get("stale_count", 0) and not include_all:
            print(
                f"({summary['stale_count']} stale pending ignored; "
                f"`hark queue --prune` to expire)"
            )
        print(summary["announcement"])
    return OK


def cmd_providers(args: argparse.Namespace) -> int:
    from hark.providers.auth import all_provider_status

    if args.test_name and args.test_name.lower() in ("voices", "voice", "tts-voices"):
        from hark.providers.xai import list_xai_voices

        voices = list_xai_voices()
        if args.json:
            print(json.dumps({"voices": voices}, indent=2))
        else:
            print(f"{'voice_id':12}  {'name':12}  gender")
            for v in voices:
                print(
                    f"{str(v.get('voice_id', '')):12}  "
                    f"{str(v.get('name', '')):12}  "
                    f"{v.get('gender') or '-'}"
                )
            print(
                "\nSet default: [tts] voice = \"ara\" in ~/.config/hark/config.toml"
                "\nOr one-shot:  hark tts --voice ara \"hello\""
            )
        return OK

    rows = [
        {
            "name": a.name,
            "available": a.available,
            "source": a.source,
            "detail": a.detail,
        }
        for a in all_provider_status()
    ]
    # Optional local full-STT readiness (B072)
    try:
        from hark.providers.local_stt import local_stt_statuses

        for s in local_stt_statuses():
            rows.append(
                {
                    "name": s.name,
                    "available": s.available,
                    "source": "local" if s.available else None,
                    "detail": s.detail,
                    "rtf_note": s.rtf_note,
                }
            )
    except Exception:
        pass
    if args.test_name:
        name = args.test_name.lower().replace("-", "_")
        rows = [
            r
            for r in rows
            if r["name"] == name or r["name"].replace("-", "_") == name
        ]
        if not rows:
            eprint(f"unknown provider: {args.test_name}")
            eprint("hint: try `hark providers voices`")
            return USAGE
    if args.json:
        print(json.dumps({"providers": rows}, indent=2))
    else:
        for r in rows:
            mark = "ok" if r["available"] else "no"
            print(f"{r['name']:16}  {mark:3}  {r['detail']}")
            if r.get("rtf_note"):
                print(f"{'':16}      RTF: {r['rtf_note']}")
    return OK


def cmd_devices(args: argparse.Namespace) -> int:
    try:
        from hark.audio.capture import list_input_devices

        devices = list_input_devices()
    except Exception as exc:
        eprint(f"hark devices: {exc}")
        return AUDIO
    if args.json:
        print(json.dumps({"devices": devices}, indent=2))
    else:
        for d in devices:
            print(f"{d['id']:3}  {d['name']}")
    return OK


if __name__ == "__main__":
    raise SystemExit(main())
