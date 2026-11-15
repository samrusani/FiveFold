"""The viewer's timeline order (0012, decision 1), with no database.

Events go by virtual time; at one virtual time, harness events come before gateway events,
and each source keeps its own `seq` order. Every event that shares its virtual time with
another is marked, because the rule does not say which of them happened first.
"""

from __future__ import annotations

from collections import Counter

from hypothesis import given
from hypothesis import strategies as st

from fivefold.viewer.timeline import SOURCE_ORDER, Event, Kind, Source, merge


def event(source: Source, seq: int, vt: int) -> Event:
    kind: Kind = "message" if source == "harness" else "action_event"
    return Event(source, seq, vt, kind, {"seq": seq})


def test_harness_events_come_first_at_an_equal_virtual_time() -> None:
    timeline = merge(
        [
            event("gateway", 1, 4658),
            event("harness", 51, 4658),
            event("gateway", 2, 4658),
            event("harness", 49, 4640),
            event("gateway", 3, 4700),
        ]
    )
    assert [(e["source"], e["seq"], e["vt"], e["shares_vt"]) for e in timeline] == [
        ("harness", 49, 4640, False),
        ("harness", 51, 4658, True),
        ("gateway", 1, 4658, True),
        ("gateway", 2, 4658, True),
        ("gateway", 3, 4700, False),
    ]


def test_events_of_one_source_at_one_virtual_time_keep_their_seq_order_and_are_marked() -> None:
    timeline = merge([event("harness", 9, 100), event("harness", 7, 100)])
    assert [(e["seq"], e["shares_vt"]) for e in timeline] == [(7, True), (9, True)]


def test_each_event_carries_its_record_under_its_kind() -> None:
    (only,) = merge([Event("harness", 18, 5292, "fault", {"fault": "F11"})])
    assert only == {
        "source": "harness",
        "seq": 18,
        "vt": 5292,
        "kind": "fault",
        "shares_vt": False,
        "fault": {"fault": "F11"},
    }


def test_an_empty_run_has_an_empty_timeline() -> None:
    assert merge([]) == []


sources = st.sampled_from(["harness", "gateway"])


@given(
    st.lists(
        st.tuples(sources, st.integers(min_value=1, max_value=10_000), st.integers(0, 50)),
        unique_by=lambda item: (item[0], item[1]),
        max_size=40,
    )
)
def test_the_order_and_the_marks_follow_the_rule_for_any_events(
    items: list[tuple[Source, int, int]],
) -> None:
    timeline = merge([event(source, seq, vt) for source, seq, vt in items])
    assert len(timeline) == len(items)
    keys = [(e["vt"], SOURCE_ORDER[e["source"]], e["seq"]) for e in timeline]  # type: ignore[index]
    assert keys == sorted(keys)
    per_vt = Counter(vt for _, _, vt in items)
    assert all(e["shares_vt"] == (per_vt[e["vt"]] > 1) for e in timeline)  # type: ignore[index]
