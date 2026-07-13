"""hark CLI entrypoint."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from hark import __version__
from hark.config import config_to_dict, eprint, load_config, write_default_config
from hark.doctor import run_doctor
from hark.exitcodes import ERROR, HERDR, OK, USAGE
from hark.herdr.client import HerdrClient, HerdrError
from hark.paths import default_config_path, state_dir
from hark.targets import parse_target
from hark.watch import run_watch


def _add_json_flag(p: argparse.ArgumentParser) -> None:
    p.add_argument("--json", action="store_true", help="machine-readable output")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hark",
        description="Hark — voice bridge for Herdr coding agents",
    )
    p.add_argument("--version", action="version", version=f"hark {__version__}")
    p.add_argument(
        "--config",
        dest="config_path",
        default=None,
        help="path to config.toml (or set HARK_CONFIG)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    # doctor
    d = sub.add_parser("doctor", help="check Herdr, auth, paths")
    _add_json_flag(d)

    # config
    c = sub.add_parser("config", help="config path | init | show")
    c_sub = c.add_subparsers(dest="config_cmd", required=True)
    c_sub.add_parser("path", help="print config path")
    ci = c_sub.add_parser("init", help="write default config.toml")
    ci.add_argument("--force", action="store_true")
    cs = c_sub.add_parser("show", help="show effective config")
    _add_json_flag(cs)

    # status
    st = sub.add_parser("status", help="list agent statuses")
    st.add_argument("--session", action="append", dest="sessions", default=None)
    st.add_argument("--status", dest="filter_status", default=None)
    st.add_argument("--read-excerpt", action="store_true")
    _add_json_flag(st)

    # watch
    w = sub.add_parser("watch", help="emit HEP events (poll merge)")
    w.add_argument("--session", action="append", dest="sessions", default=None)
    w.add_argument(
        "--statuses",
        default=None,
        help="comma list, default blocked,done",
    )
    w.add_argument("--for-monitor", action="store_true")
    w.add_argument(
        "--transport",
        choices=("auto", "socket", "poll"),
        default=None,
    )
    w.add_argument("--once", action="store_true", help="one poll cycle then exit")
    w.add_argument(
        "--read-questions",
        action="store_true",
        help="on blocked, try to read pane excerpt (slower)",
    )

    # context
    ctx = sub.add_parser("context", help="read pane context for a target")
    ctx.add_argument("target", help="session/pane or pane with --session")
    ctx.add_argument("--session", default=None)
    ctx.add_argument("--lines", type=int, default=60)
    _add_json_flag(ctx)

    # reply / keys / answer (delivery)
    rp = sub.add_parser("reply", help="freeform send text (debug; prefer answer)")
    rp.add_argument("target")
    rp.add_argument("text")
    rp.add_argument("--session", default=None)

    ky = sub.add_parser("keys", help="send keys to a pane")
    ky.add_argument("target")
    ky.add_argument("keys", nargs="+")
    ky.add_argument("--session", default=None)

    an = sub.add_parser("answer", help="bound delivery by event_id (partial v1)")
    an.add_argument("event_id")
    an.add_argument("--text", default=None)
    an.add_argument("--keys", nargs="+", default=None)
    an.add_argument(
        "--expect-fingerprint",
        default=None,
        help="optional fingerprint check (full bound store later)",
    )

    # stubs for Mode C / later
    for name, help_ in (
        ("queue", "pending interactions (stub)"),
        ("tts", "text-to-speech (not yet)"),
        ("listen", "speech-to-text (not yet)"),
        ("ask", "speak + listen (not yet)"),
        ("skip", "skip event (stub)"),
        ("mute", "mute (stub)"),
        ("unmute", "unmute (stub)"),
        ("devices", "list audio devices (stub)"),
        ("providers", "list speech providers"),
    ):
        sp = sub.add_parser(name, help=help_)
        if name == "providers":
            sp.add_argument("test_name", nargs="?", default=None)
            _add_json_flag(sp)
        if name in ("tts", "listen", "ask"):
            sp.add_argument("text", nargs="*", default=[])
            _add_json_flag(sp)
        if name in ("listen", "ask"):
            sp.add_argument(
                "--end-mode",
                choices=("silence", "radio"),
                default=None,
                help=(
                    "silence=Smart Turn/end-silence; radio=keep listening until "
                    "end phrase (e.g. 'okay send it'). Default: config [listen].end_mode"
                ),
            )
        if name == "skip":
            sp.add_argument("event_id")
        if name == "queue":
            _add_json_flag(sp)

    return p


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return int(code) if isinstance(code, int) else USAGE

    cfg_path = getattr(args, "config_path", None)
    cfg = load_config(cfg_path)

    try:
        return dispatch(args, cfg)
    except HerdrError as exc:
        eprint(f"hark: herdr error: {exc}")
        return HERDR
    except ValueError as exc:
        eprint(f"hark: {exc}")
        return USAGE
    except FileExistsError as exc:
        eprint(f"hark: {exc}")
        return ERROR
    except NotImplementedError as exc:
        eprint(f"hark: not implemented yet: {exc}")
        return ERROR


def dispatch(args: argparse.Namespace, cfg) -> int:
    cmd = args.cmd

    if cmd == "doctor":
        return run_doctor(cfg, as_json=bool(args.json))

    if cmd == "config":
        return cmd_config(args, cfg)

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

    if cmd == "context":
        return cmd_context(args, cfg)

    if cmd == "reply":
        return cmd_reply(args, cfg)

    if cmd == "keys":
        return cmd_keys(args, cfg)

    if cmd == "answer":
        return cmd_answer(args, cfg)

    if cmd == "providers":
        return cmd_providers(args)

    if cmd == "queue":
        empty: list = []
        if args.json:
            print(json.dumps({"queue": empty}))
        else:
            print("(empty — queue tracking not active in Mode A poll-only)")
        return OK

    if cmd in ("mute", "unmute", "skip"):
        eprint(f"hark {cmd}: recorded no-op (library mute/skip store later)")
        return OK

    if cmd == "devices":
        eprint("hark devices: audio device enumeration not implemented yet")
        return ERROR

    if cmd in ("tts", "listen", "ask"):
        end_mode = getattr(args, "end_mode", None) or cfg.listen.end_mode
        eprint(
            f"hark {cmd}: speech I/O not implemented yet "
            f"(Phase 1.6+). Effective listen end_mode={end_mode!r} "
            f"(config [listen] / HARK_LISTEN_END_MODE / --end-mode). "
            "Auth: see hark doctor / hark providers."
        )
        return ERROR

    eprint(f"hark: unknown command {cmd}")
    return USAGE


def cmd_config(args: argparse.Namespace, cfg) -> int:
    if args.config_cmd == "path":
        print(default_config_path())
        return OK
    if args.config_cmd == "init":
        path = write_default_config(force=bool(args.force))
        print(f"wrote {path}")
        state_dir().mkdir(parents=True, exist_ok=True)
        return OK
    if args.config_cmd == "show":
        data = config_to_dict(cfg)
        if args.json:
            print(json.dumps(data, indent=2))
        else:
            print(json.dumps(data, indent=2))
        return OK
    return USAGE


def cmd_status(args: argparse.Namespace, cfg) -> int:
    sessions = cfg.sessions
    if args.sessions:
        want = set(args.sessions)
        sessions = [s for s in cfg.sessions if s.id in want]
        if not sessions:
            from hark.config import SessionConfig

            sessions = [SessionConfig(id=sid) for sid in args.sessions]

    rows = []
    any_ok = False
    errors = []
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
        if not rows and errors:
            for e in errors:
                eprint(f"{e['session_id']}: {e['error']}")
            return HERDR
        for r in rows:
            print(
                f"{r['target']:16}  {r['status']:8}  "
                f"{r['agent'] or '-':8}  rev={r['revision']}"
            )
        for e in errors:
            eprint(f"warn: {e['session_id']}: {e['error']}")

    if not any_ok:
        return HERDR
    return OK


def cmd_context(args: argparse.Namespace, cfg) -> int:
    target = parse_target(args.target, default_session=args.session or "local")
    session = cfg.session_by_id(target.session_id)
    if session is None:
        from hark.config import SessionConfig

        session = SessionConfig(id=target.session_id)
    client = HerdrClient(session)
    text = client.read_pane(target.pane_id, lines=args.lines)
    if args.json:
        print(
            json.dumps(
                {
                    "target": str(target),
                    "lines": args.lines,
                    "text": text,
                },
                indent=2,
            )
        )
    else:
        print(text)
    return OK


def cmd_reply(args: argparse.Namespace, cfg) -> int:
    target = parse_target(args.target, default_session=args.session or "local")
    session = cfg.session_by_id(target.session_id)
    if session is None:
        from hark.config import SessionConfig

        session = SessionConfig(id=target.session_id)
    HerdrClient(session).send_text(target.pane_id, args.text)
    print(json.dumps({"ok": True, "target": str(target), "mode": "reply"}))
    return OK


def cmd_keys(args: argparse.Namespace, cfg) -> int:
    target = parse_target(args.target, default_session=args.session or "local")
    session = cfg.session_by_id(target.session_id)
    if session is None:
        from hark.config import SessionConfig

        session = SessionConfig(id=target.session_id)
    HerdrClient(session).send_keys(target.pane_id, list(args.keys))
    print(
        json.dumps(
            {"ok": True, "target": str(target), "keys": list(args.keys)}
        )
    )
    return OK


def cmd_answer(args: argparse.Namespace, cfg) -> int:
    """Bound answer: full event store comes later; reject obvious misuse now."""
    from hark.exitcodes import ABORT

    if not args.text and not args.keys:
        eprint("hark answer: require --text or --keys")
        return USAGE
    if args.text and args.keys:
        eprint("hark answer: use either --text or --keys, not both")
        return USAGE

    # Without a delivery store we cannot revalidate fingerprints from event_id.
    # Accept only with explicit fingerprint for now, or warn and refuse.
    eprint(
        "hark answer: bound delivery store not implemented yet; "
        "use `hark reply` / `hark keys` for freeform, or wait for event store."
    )
    if args.expect_fingerprint:
        eprint(
            f"(would check fingerprint {args.expect_fingerprint!r} for event {args.event_id})"
        )
    return ABORT


def cmd_providers(args: argparse.Namespace) -> int:
    from hark.providers.auth import all_provider_status

    rows = [
        {
            "name": a.name,
            "available": a.available,
            "source": a.source,
            "detail": a.detail,
        }
        for a in all_provider_status()
    ]
    if args.test_name:
        name = args.test_name.lower()
        rows = [r for r in rows if r["name"] == name]
        if not rows:
            eprint(f"unknown provider: {args.test_name}")
            return USAGE
        # Live provider test later
        eprint(f"provider live test for {name}: not implemented yet")
    if args.json:
        print(json.dumps({"providers": rows}, indent=2))
    else:
        for r in rows:
            mark = "ok" if r["available"] else "no"
            print(f"{r['name']:10}  {mark:3}  {r['detail']}")
    return OK


if __name__ == "__main__":
    raise SystemExit(main())
