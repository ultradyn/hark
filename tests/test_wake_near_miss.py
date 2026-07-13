"""Plausible wake near-miss detection and Mode A grouping schedule (B019)."""

from __future__ import annotations

from hark.wake import (
    DEFAULT_ACTIVATION_PHRASES,
    NearMiss,
    NearMissAccumulator,
    TextProbeBackend,
    make_wake_near_miss_event,
    match_activation,
    near_miss_group_size,
    plausible_near_miss,
)


def test_near_miss_group_size_schedule():
    assert near_miss_group_size(0) == 1
    assert near_miss_group_size(1) == 2
    assert near_miss_group_size(2) == 2
    assert near_miss_group_size(3) == 3
    assert near_miss_group_size(10) == 3


def test_accumulator_emits_1_2_2_then_threes():
    acc = NearMissAccumulator()
    sizes: list[int] = []
    for i in range(11):
        miss = NearMiss(
            text=f"hey hoc{i}",
            best_phrase="hey hark",
            score=0.6,
            reason="test",
        )
        group = acc.add(miss)
        if group is not None:
            sizes.append(len(group))
    assert sizes == [1, 2, 2, 3, 3]
    assert acc.total == 11
    assert acc.group_index == 5
    assert acc.pending == []


def test_accumulator_pending_until_group_full():
    acc = NearMissAccumulator()
    # first group size 1 → immediate emit
    g0 = acc.add(NearMiss("hey hoc", "hey hark", 0.6, "t"))
    assert g0 is not None and len(g0) == 1
    # second group size 2
    assert acc.add(NearMiss("hey ho", "hey hark", 0.5, "t")) is None
    g1 = acc.add(NearMiss("a hawk", "hey hark", 0.55, "t"))
    assert g1 is not None and len(g1) == 2
    assert [m.text for m in g1] == ["hey ho", "a hawk"]


def test_accumulator_reset_pending():
    acc = NearMissAccumulator()
    acc.add(NearMiss("hey hoc", "hey hark", 0.6, "t"))  # emits group 0
    assert acc.add(NearMiss("hey ho", "hey hark", 0.5, "t")) is None
    assert len(acc.pending) == 1
    acc.reset_pending()
    assert acc.pending == []
    # total/group_index preserved
    assert acc.total == 2
    assert acc.group_index == 1


def test_plausible_accepts_fixture_style_misses():
    accept = [
        "hey hoc",
        "hey ho",
        "a hawk",
        "hello",
        "hey ha",
        "harold",
        "hi harkk",
        "hey",
        "hey har",
        "a hook",
    ]
    for text in accept:
        miss = plausible_near_miss(text)
        assert miss is not None, f"expected near-miss for {text!r}"
        assert miss.text
        assert miss.best_phrase
        assert 0.0 < miss.score <= 1.0
        # Must not also be a successful activation
        assert match_activation(text, anywhere=True) is None


def test_plausible_rejects_noise_and_long_speech():
    reject = [
        "",
        "   ",
        "the weather is nice today",
        "please hark back to the earlier design",
        "what time is it",
        "hey everyone how are you doing",
        "hey everyone",
        "okay thanks",
        "hi there",
        "hey you",
        "ok sure",
        "a bird",
    ]
    for text in reject:
        assert plausible_near_miss(text) is None, f"should reject {text!r}"


def test_plausible_rejects_successful_activations():
    for text in ("hey hark", "hey hook", "hey harold", "hello herald", "ok hark status"):
        assert match_activation(text, anywhere=True) is not None
        assert plausible_near_miss(text) is None


def test_plausible_respects_custom_phrases():
    phrases = ["start prompt", "begin dictation"]
    # Unrelated to custom phrases
    assert plausible_near_miss("hey hoc", phrases) is not None  # still wake-family
    # Close to custom phrase
    miss = plausible_near_miss("start promt", phrases)
    assert miss is not None
    assert "start" in miss.best_phrase or miss.score > 0.5


def test_make_wake_near_miss_event_shape():
    attempts = [
        NearMiss("hey hoc", "hey hark", 0.62, "prefix_product_near"),
        NearMiss("a hawk", "hey hark", 0.55, "family_token"),
    ]
    acc = NearMissAccumulator()
    # simulate after emitting group index 1 (second group)
    acc.total = 3
    acc.group_index = 2
    ev = make_wake_near_miss_event(
        attempts,
        total_near_misses=acc.total,
        group_index=acc.group_index,
        phrases=DEFAULT_ACTIVATION_PHRASES,
    )
    assert ev["schema"] == "hark.event.v1"
    assert ev["kind"] == "ambient.wake_near_miss"
    assert ev["count"] == 2
    assert ev["total_near_misses"] == 3
    assert ev["group_index"] == 1
    assert len(ev["attempts"]) == 2
    assert ev["attempts"][0]["text"] == "hey hoc"
    assert "extra_trigger_phrases" in ev["instructions"]
    assert "restart" in ev["instructions"].lower() or "SIGHUP" in ev["instructions"]
    assert "activation_phrases" in ev["instructions"]
    assert isinstance(ev["event_id"], str) and ev["event_id"]
    assert ev["observed_at"].endswith("Z")


def test_text_probe_sets_last_text_on_miss():
    be = TextProbeBackend()
    assert be.score_snippet(b"TXT:hey hoc") is None
    assert be.last_text == "hey hoc"
    miss = plausible_near_miss(be.last_text)
    assert miss is not None
