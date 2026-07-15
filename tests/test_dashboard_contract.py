"""B060: hark.dashboard.v1 contract — schemas validate fixtures.

Rust-port parity gate: a Rust `hark serve` must produce responses that pass
these same schema/fixture checks (see docs/DASHBOARD.md).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from hark.state_feed import CursorPosition, format_cursor

jsonschema = pytest.importorskip("jsonschema")

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "schemas" / "dashboard-v1"
FIX = ROOT / "fixtures" / "dashboard"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_jsonl(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            rows.append(json.loads(line))
    return rows


def _validator(schema_file: str) -> "jsonschema.Draft202012Validator":
    schema = _load(SCHEMAS / schema_file)
    registry = None
    try:
        from referencing import Registry, Resource

        resources = []
        for p in SCHEMAS.glob("*.schema.json"):
            doc = _load(p)
            resources.append((p.name, Resource.from_contents(doc)))
            if "$id" in doc:
                resources.append((doc["$id"], Resource.from_contents(doc)))
        registry = Registry().with_resources(resources)
    except ImportError:
        pass
    if registry is not None:
        return jsonschema.Draft202012Validator(schema, registry=registry)
    return jsonschema.Draft202012Validator(schema)


# ---------------------------------------------------------------------------
# stream envelope
# ---------------------------------------------------------------------------

STREAM_ROWS = _load_jsonl(FIX / "stream.jsonl")


@pytest.mark.parametrize(
    "row", STREAM_ROWS, ids=[f"{r['source']}:{r['type']}:{i}" for i, r in enumerate(STREAM_ROWS)]
)
def test_stream_fixture_validates(row: dict) -> None:
    _validator("stream.schema.json").validate(row)


def test_stream_covers_all_sources() -> None:
    sources = {r["source"] for r in STREAM_ROWS}
    assert {"watch", "ambient", "system", "usage", "delivery", "serve"} <= sources


def test_stream_has_hello_first() -> None:
    assert STREAM_ROWS[0]["type"] == "hello"
    assert STREAM_ROWS[0]["payload"]["kind"] == "serve.hello"


def test_cursor_format() -> None:
    pat = re.compile(
        r"^[a-z][a-z0-9_-]*:[0-9]{1,19}"
        r"(?:@(?:[A-Za-z0-9._-]+|[a-f0-9]{32}~[a-f0-9]{32}))?"
        r"(?:,[a-z][a-z0-9_-]*:[0-9]{1,19}"
        r"(?:@(?:[A-Za-z0-9._-]+|[a-f0-9]{32}~[a-f0-9]{32}))?)*$"
    )
    for row in STREAM_ROWS:
        assert pat.fullmatch(row["cursor"]), row["cursor"]

    incarnation_cursor = format_cursor(
        {
            "watch": CursorPosition(184, "a" * 32, "b" * 32),
            "serve": 9,
        }
    )
    assert pat.fullmatch(incarnation_cursor)
    row = {**STREAM_ROWS[0], "cursor": incarnation_cursor}
    _validator("stream.schema.json").validate(row)

    assert not pat.fullmatch("watch:１２")
    assert not pat.fullmatch(f"watch:{'9' * 20}")


# ---------------------------------------------------------------------------
# snapshots
# ---------------------------------------------------------------------------

SNAPSHOTS = [
    ("health.json", "health.schema.json"),
    ("config.json", "config.schema.json"),
    ("herdr-sessions.json", "herdr-sessions.schema.json"),
    ("context.json", "context.schema.json"),
    ("deliveries.json", "deliveries.schema.json"),
    ("usage.json", "usage.schema.json"),
    ("events-page.json", "events-page.schema.json"),
]


@pytest.mark.parametrize("fixture,schema", SNAPSHOTS, ids=[s[0] for s in SNAPSHOTS])
def test_snapshot_fixture_validates(fixture: str, schema: str) -> None:
    _validator(schema).validate(_load(FIX / fixture))


# ---------------------------------------------------------------------------
# actions ($defs cases)
# ---------------------------------------------------------------------------

ACTION_ROWS = _load_jsonl(FIX / "actions.jsonl")


@pytest.mark.parametrize("row", ACTION_ROWS, ids=[r["id"] for r in ACTION_ROWS])
def test_action_fixture_validates(row: dict) -> None:
    schema = _load(SCHEMAS / "actions.schema.json")
    case = {"$ref": f"#/$defs/{row['def']}", "$defs": schema["$defs"]}
    jsonschema.Draft202012Validator(case).validate(row["data"])


def test_answer_request_rejects_both_and_neither() -> None:
    schema = _load(SCHEMAS / "actions.schema.json")
    case = {"$ref": "#/$defs/answerRequest", "$defs": schema["$defs"]}
    v = jsonschema.Draft202012Validator(case)
    with pytest.raises(jsonschema.ValidationError):
        v.validate({"event_id": "e1", "text": "yes", "keys": ["1"]})
    with pytest.raises(jsonschema.ValidationError):
        v.validate({"event_id": "e1"})


# ---------------------------------------------------------------------------
# redaction gate (normative: docs/DASHBOARD.md)
# ---------------------------------------------------------------------------

SECRET_VALUE = re.compile(
    r"(sk-[A-Za-z0-9]{16,}|xai-[A-Za-z0-9]{16,}|AIza[A-Za-z0-9_-]{20,}"
    r"|eyJ[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,})"
)
SECRET_KEY = re.compile(r"(api_?key|secret|password|bearer|access_token|refresh_token)$", re.I)


def _walk(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk(v, f"{path}[{i}]")
    else:
        yield path, obj


@pytest.mark.parametrize(
    "fixture",
    sorted(p.name for p in FIX.iterdir()),
)
def test_no_secret_material_in_fixtures(fixture: str) -> None:
    path = FIX / fixture
    if path.suffix == ".jsonl":
        docs = _load_jsonl(path)
    else:
        docs = [_load(path)]
    for doc in docs:
        for keypath, value in _walk(doc):
            # auth request is the one allowed client->server token carrier
            if keypath.endswith(".token") and "auth" in json.dumps(doc)[:80]:
                continue
            if isinstance(value, str):
                assert not SECRET_VALUE.search(value), f"{fixture}:{keypath}"
            leaf = keypath.rsplit(".", 1)[-1]
            if SECRET_KEY.search(leaf):
                assert value in (None, "", True, False), (
                    f"{fixture}:{keypath} secret-named key must not carry a value"
                )


def test_config_fixture_has_no_dashboard_token() -> None:
    cfg = _load(FIX / "config.json")["config"]
    assert "token" not in cfg.get("dashboard", {}), "expose token_configured, never token"
    assert cfg["dashboard"]["token_configured"] is True
