"""Fault plans: validation against the catalog, and per-entry occurrence counting (0006)."""

from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from fivefold.faults.catalog import BUILT, POINTS, FaultId
from fivefold.faults.plan import FaultInjector, check_configuration, parse_plan

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
        ({"fault": "F8", "at": {"point": "agent.step"}}, "cannot be injected"),
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
        ("F1", "gw.dispatch.after_check"),
        ("F1", "gw.dispatch.after_record"),
        ("F1", "agent.before_tool_call"),
        ("F9", "agent.step"),
        ("F3", "gw.dispatch.after_answer"),
        ("F3", "agent.after_tool_result"),
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
        (
            {"fault": "F1", "variant": "a", "at": {"point": "gw.dispatch.after_record"}},
            "F1 a fires at",
        ),
        (
            {"fault": "F1", "variant": "b", "at": {"point": "gw.dispatch.after_check"}},
            "F1 b fires at",
        ),
        (
            {"fault": "F1", "at": {"point": "gw.dispatch.after_answer"}},
            "F1 does not fire at",
        ),
        ({"fault": "F9", "at": {"point": "agent.after_tool_call"}}, "F9 does not fire at"),
        ({"fault": "F9", "variant": "other_args", "at": {"point": "agent.step"}}, "no variant"),
        (
            {
                "fault": "F1",
                "at": {"point": "gw.dispatch.after_check"},
                "params": {"restart_delay_ms": -1},
            },
            "restart_delay_ms must be at least 0",
        ),
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
            {"fault": "F1", "variant": "a", "at": {"point": "gw.dispatch.after_check"}},
            {"fault": "F1", "variant": "b", "at": {"point": "gw.dispatch.after_record"}},
            {"fault": "F1", "variant": "b", "at": {"point": "agent.before_tool_call"}},
            {"fault": "F9", "variant": "same_args", "at": {"point": "agent.step"}},
            {"fault": "F9", "variant": "different_args", "at": {"point": "agent.step"}},
        ]
    )


def test_f1_restarts_after_a_second_by_default() -> None:
    (planned,) = parse_plan([{"fault": "F1", "at": {"point": "gw.dispatch.after_check"}}])
    assert planned.param("restart_delay_ms") == 1_000


@pytest.mark.parametrize(
    ("fault", "point", "gateway"),
    [
        ("F1", "agent.before_tool_call", True),
        ("F3", "agent.after_tool_result", True),
        ("F3", "gw.dispatch.after_answer", False),
        ("F1", "gw.dispatch.after_check", False),
        ("F2", "net.agent_sim.answer", True),
        ("F2", "net.gw_sim.answer", False),
        ("F12", "net.agent_gw.request", False),
    ],
)
def test_a_point_the_configuration_does_not_have_is_refused(
    fault: str, point: str, gateway: bool
) -> None:
    """F1 at `agent.before_tool_call` is B0's equivalent; in G2 it would kill the agent
    instead of crashing the gateway (0006)."""
    plan = parse_plan([{"fault": fault, "at": {"point": point}}])
    with pytest.raises(ValueError, match=re.escape(f"{fault} at {point}")):
        check_configuration(plan, gateway=gateway)


def test_each_fault_is_accepted_where_its_configuration_has_the_point() -> None:
    gateway = parse_plan(
        [
            {"fault": "F1", "at": {"point": "gw.dispatch.after_record"}},
            {"fault": "F2", "at": {"point": "net.gw_sim.answer"}},
            {"fault": "F11", "at": {"point": "agent.after_tool_call"}},
            {"fault": "F12", "at": {"point": "net.agent_gw.request"}},
        ]
    )
    baseline = parse_plan(
        [
            {"fault": "F1", "at": {"point": "agent.before_tool_call"}},
            {"fault": "F2", "at": {"point": "net.agent_sim.answer"}},
            {"fault": "F11", "at": {"point": "agent.after_tool_result"}},
        ]
    )
    check_configuration(gateway, gateway=True)
    check_configuration(baseline, gateway=False)
