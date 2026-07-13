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

    w = sub.add_parser("watch", help="emit HEP events")
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

    ctx = sub.add_parser("context", help="read pane context")
    ctx.add_argument("target")
    ctx.add_argument("--session")
    ctx.add_argument("--lines", type=int, default=60)
    ctx.add_argument("--json", action="store_true")

    rp = sub.add_parser("reply", help="freeform send text")
    rp.add_argument("target")
    rp.add_argument("text")
    rp.add_argument("--session")

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

    li = sub.add_parser("listen", help="speech-to-text")
    li.add_argument("--provider")
    li.add_argument("--end-mode", choices=("silence", "radio"))
    li.add_argument("--json", action="store_true")

    ask = sub.add_parser("ask", help="speak prompt + listen")
    ask.add_argument("text", nargs="+")
    ask.add_argument("--confirm", choices=("auto", "always", "never"))
    ask.add_argument("--end-mode", choices=("silence", "radio"))
    ask.add_argument("--provider")
    ask.add_argument("--json", action="store_true")

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

    q = sub.add_parser("queue", help="pending bound events")
    q.add_argument("--json", action="store_true")

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
    sub.add_parser("unmute", help="unmute system default mic (pactl)")

    stt_stats = sub.add_parser("stats", help="TTS/STT usage stats")
    stt_stats.add_argument("--json", action="store_true")
    stt_stats.add_argument("--reset", action="store_true", help="delete usage log")

    return p


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return int(code) if isinstance(code, int) else USAGE

    cfg = load_config(getattr(args, "config_path", None))
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
    if cmd == "ask":
        return cmd_ask(args, cfg)
    if cmd == "ambient":
        return cmd_ambient(args, cfg)
    if cmd == "queue":
        return cmd_queue(args)
    if cmd == "providers":
        return cmd_providers(args)
    if cmd == "devices":
        return cmd_devices(args)
    if cmd == "mute":
        return cmd_mic_mute(True)
    if cmd == "unmute":
        return cmd_mic_mute(False)
    if cmd == "stats":
        return cmd_stats(args)
    return USAGE


def cmd_mic_mute(mute: bool) -> int:
    from hark.audio.mic_mute import default_source, set_source_mute, source_is_muted

    src = default_source()
    if not src:
        eprint("hark: no default Pulse/PipeWire source")
        return AUDIO
    if not set_source_mute(src, mute):
        eprint(f"hark: failed to {'mute' if mute else 'unmute'} {src}")
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
    _client_for(cfg, target.session_id).send_text(target.pane_id, args.text)
    print(json.dumps({"ok": True, "target": str(target), "mode": "reply"}))
    return OK


def cmd_keys(args: argparse.Namespace, cfg) -> int:
    target = parse_target(args.target, default_session=args.session or "local")
    _client_for(cfg, target.session_id).send_keys(target.pane_id, list(args.keys))
    print(json.dumps({"ok": True, "target": str(target), "keys": list(args.keys)}))
    return OK


def cmd_answer(args: argparse.Namespace, cfg) -> int:
    if not args.text and not args.keys:
        eprint("hark answer: require --text or --keys")
        return USAGE
    if args.text and args.keys:
        eprint("hark answer: use either --text or --keys")
        return USAGE

    store = DeliveryStore()
    bound = store.get(args.event_id)
    if bound is None:
        eprint(f"hark answer: unknown event_id {args.event_id} (not in store)")
        eprint("hint: events are registered by `hark watch`; or use reply/keys")
        return ABORT
    if store.already_delivered(args.event_id):
        eprint("hark answer: already delivered (idempotent refuse)")
        return ABORT

    fingerprint = (
        bound.question_fingerprint.strip()
        if isinstance(bound.question_fingerprint, str)
        else ""
    )
    if not fingerprint:
        store.mark(args.event_id, "rejected", reason="missing_question_fingerprint")
        eprint("hark answer: bound event has no question fingerprint")
        return ABORT

    has_revision = isinstance(bound.pane_revision, int) and bound.pane_revision > 0

    client = _client_for(cfg, bound.session_id)
    live = client.get_agent(bound.pane_id)
    if live is None:
        store.mark(args.event_id, "rejected", reason="pane_gone")
        eprint("hark answer: pane no longer present")
        return ABORT
    if live.status != "blocked":
        store.mark(args.event_id, "rejected", reason="not_blocked")
        eprint(f"hark answer: agent is no longer blocked (live status: {live.status})")
        return ABORT
    if has_revision and live.revision != bound.pane_revision:
        store.mark(args.event_id, "rejected", reason="stale_revision")
        eprint(
            f"hark answer: stale revision "
            f"(expected {bound.pane_revision}, live {live.revision})"
        )
        return ABORT

    if fingerprint:
        try:
            text = client.read_pane(bound.pane_id, lines=40)
            from hark.events import extract_question_excerpt

            excerpt = extract_question_excerpt(text)
            live_fp = question_fingerprint(excerpt)
            if live_fp != fingerprint:
                store.mark(args.event_id, "rejected", reason="fingerprint_mismatch")
                eprint("hark answer: question fingerprint mismatch (stale)")
                return ABORT
        except HerdrError:
            store.mark(args.event_id, "rejected", reason="fingerprint_unavailable")
            eprint("hark answer: unable to verify question fingerprint")
            return ABORT

    if args.keys:
        client.send_keys(bound.pane_id, list(args.keys))
        store.mark(args.event_id, "delivered", keys=list(args.keys))
    else:
        client.send_text(bound.pane_id, args.text)
        store.mark(args.event_id, "delivered", text=args.text)

    print(
        json.dumps(
            {
                "ok": True,
                "event_id": args.event_id,
                "target": f"{bound.session_id}/{bound.pane_id}",
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
    from hark.speech import run_tts

    text = " ".join(args.text)
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


def cmd_listen(args: argparse.Namespace, cfg) -> int:
    from hark.speech import run_listen

    result = run_listen(cfg, provider=args.provider, end_mode=args.end_mode)
    if result.cancelled:
        eprint(f"cancelled via {result.end_phrase!r}")
        print(json.dumps({"ok": False, "cancelled": True, "text": result.text}))
        return ABORT
    payload = {
        "ok": True,
        "text": result.text,
        "provider": result.provider,
        "duration_ms": result.duration_ms,
        "end_mode": result.end_mode,
        "end_phrase": result.end_phrase,
    }
    print(json.dumps(payload, indent=2 if args.json else None))
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
    # continuous Mode A ambient
    return run_ambient_loop(cfg, announce=announce)


def cmd_queue(args: argparse.Namespace) -> int:
    store = DeliveryStore()
    pending = []
    if store.path.is_file():
        seen = {}
        for line in store.path.read_text(encoding="utf-8").splitlines():
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            eid = data.get("event_id")
            if eid:
                seen[eid] = data
        for eid, data in seen.items():
            if not store.already_delivered(eid) and data.get("status") == "pending":
                pending.append(data)
    if args.json:
        print(json.dumps({"queue": pending}, indent=2))
    else:
        if not pending:
            print("(empty)")
        for p in pending:
            print(f"{p.get('event_id')}  {p.get('session_id')}/{p.get('pane_id')}")
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
    if args.test_name:
        name = args.test_name.lower()
        rows = [r for r in rows if r["name"] == name]
        if not rows:
            eprint(f"unknown provider: {args.test_name}")
            eprint("hint: try `hark providers voices`")
            return USAGE
    if args.json:
        print(json.dumps({"providers": rows}, indent=2))
    else:
        for r in rows:
            mark = "ok" if r["available"] else "no"
            print(f"{r['name']:10}  {mark:3}  {r['detail']}")
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
