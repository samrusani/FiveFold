"""Fault plans: validation against the catalog, and per-entry occurrence counting (0006)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from fivefold.faults.catalog import BUILT, POINTS, FaultId
from fivefold.faults.plan import FaultInjector, parse_plan

DEMO_1 = [
    {
        "fault": "F2",
        "at": {
            "point": "net.gw_sim.answer",
            "match": {"api": "submit_purchase_order", "answer": "committed"},
            "occurrence": 1,
        },
    },
    {
        "fault": "F11",
        "at": {"point": "agent.after_tool_call", "match": {"tool": "submit_purchase_order"}},
        "params": {"restart_delay_ms": 10_000},
    },
]
"""Demo 1's faults, as 0006 writes them."""


def test_demo_1s_plan_is_valid() -> None:
    f2, f11 = parse_plan(DEMO_1)
    assert (f2.fault, f2.at.occurrence) == (FaultId.F2, 1)
    assert f11.param("restart_delay_ms") == 10_000


def test_a_parameter_falls_back_to_the_catalog_default() -> None:
    (f11,) = parse_plan([{"fault": "F11", "at": {"point": "agent.after_tool_result"}}])
    assert f11.param("restart_delay_ms") == 1_000


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"fault": "F2", "at": {"point": "net.nowhere.answer"}}, "not a fault point"),
        ({"fault": "F2", "at": {"point": "agent.step"}}, "does not fire at"),
        ({"fault": "F3", "at": {"point": "gw.dispatch.after_answer"}}, "cannot be injected"),
        ({"fault": "F13", "at": {"point": "agent.step"}}, "fault"),
        (
            {"fault": "F11", "at": {"point": "agent.after_tool_call"}, "params": {"x": 1}},
            "no parameters",
        ),
        ({"fault": "F2", "at": {"point": "net.gw_sim.answer", "occurrence": 0}}, "occurrence"),
        ({"fault": "F2", "at": {"point": "net.gw_sim.answer"}, "extra": 1}, "extra"),
    ],
)
def test_a_plan_the_catalog_does_not_allow_is_refused(
    entry: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        parse_plan([entry])


def test_each_entry_counts_its_own_matching_events() -> None:
    plan = parse_plan(
        [
            {
                "fault": "F2",
                "at": {
                    "point": "net.gw_sim.answer",
                    "match": {"answer": "committed"},
                    "occurrence": 2,
                },
            },
            {"fault": "F2", "at": {"point": "net.gw_sim.answer", "occurrence": 3}},
        ]
    )
    injector = FaultInjector(plan)
    fired = [
        [f.planned.at.occurrence for f in injector.at("net.gw_sim.answer", {"answer": answer})]
        for answer in ("rejected", "committed", "committed", "committed")
    ]
    # The first entry counts committed answers only; the second counts every answer.
    assert fired == [[], [], [2, 3], []]


def test_matches_compare_as_text_and_other_points_never_count() -> None:
    plan = parse_plan(
        [
            {
                "fault": "F11",
                "at": {
                    "point": "agent.after_tool_call",
                    "match": {"tool": "submit_purchase_order"},
                },
            }
        ]
    )
    injector = FaultInjector(plan)
    assert injector.at("agent.after_tool_result", {"tool": "submit_purchase_order"}) == []
    assert injector.at("agent.after_tool_call", {"tool": "get_quote"}) == []
    (fired,) = injector.at("agent.after_tool_call", {"tool": "submit_purchase_order"})
    assert (fired.fault, fired.occurrence) == (FaultId.F11, 1)
    assert injector.at("agent.after_tool_call", {"tool": "submit_purchase_order"}) == []
    with pytest.raises(ValueError, match="not a fault point"):
        injector.at("nowhere", {})


def test_the_catalog_names_only_points_0006_lists() -> None:
    for kind in BUILT.values():
        assert kind.points <= POINTS
    assert len(POINTS) == 4 + 2 * 3 + 2 + 4


@pytest.mark.parametrize(
    ("fault", "point"),
    [
        ("F6", "gw.dispatch.before_check"),
        ("F6", "net.agent_sim.answer"),
        ("F7", "net.gw_sim.request"),
        ("F12", "net.agent_gw.request"),
    ],
)
def test_the_m2_faults_are_built_at_their_points(fault: str, point: str) -> None:
    (planned,) = parse_plan([{"fault": fault, "at": {"point": point}}])
    assert point in planned.kind.points


def test_f12_never_fires_on_an_answer() -> None:
    with pytest.raises(ValidationError, match="does not fire at"):
        parse_plan([{"fault": "F12", "at": {"point": "net.gw_sim.answer"}}])


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        (
            {"fault": "F7", "variant": "harmlesss", "at": {"point": "net.gw_sim.request"}},
            "no variant 'harmlesss'",
        ),
        (
            {"fault": "F7", "at": {"point": "net.gw_sim.request"}, "params": {"percent": -5}},
            "percent must be at least 1",
        ),
        (
            {"fault": "F6", "variant": "early", "at": {"point": "net.gw_sim.request"}},
            "F6 early fires at",
        ),
        (
            {
                "fault": "F11",
                "variant": "after_tool_call",
                "at": {"point": "agent.after_tool_result"},
            },
            "F11 after_tool_call fires at",
        ),
        ({"fault": "F2", "variant": "any", "at": {"point": "net.gw_sim.answer"}}, "no variant"),
    ],
)
def test_a_variant_or_parameter_the_fault_does_not_have_is_refused(
    entry: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        parse_plan([entry])


def test_each_variant_is_accepted_at_its_own_points() -> None:
    parse_plan(
        [
            {"fault": "F6", "variant": "late", "at": {"point": "net.agent_sim.request"}},
            {"fault": "F7", "variant": "harmless", "at": {"point": "gw.dispatch.before_check"}},
            {
                "fault": "F11",
                "variant": "after_tool_result",
                "at": {"point": "agent.after_tool_result"},
            },
        ]
    )
