"""Fault plans: validation against the catalog, and per-entry occurrence counting (0006)."""

from __future__ import annotations

import re
from typing import Any

import pytest
from pydantic import ValidationError

from fivefold.domain.contracts import Contract
from fivefold.domain.scenario import Params
from fivefold.faults.catalog import BUILT, POINTS, FaultId
from fivefold.faults.plan import (
    FaultInjector,
    check_configuration,
    check_run,
    parse_plan,
    seed_parameters,
)
from fivefold.harness.rng import Generators

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


# F4: duplicate delivery ------------------------------------------------------------------


@pytest.mark.parametrize(
    "point", ["net.agent_gw.request", "net.gw_sim.request", "net.agent_sim.request"]
)
def test_f4_is_built_at_each_hops_request_point(point: str) -> None:
    (planned,) = parse_plan([{"fault": "F4", "at": {"point": point}}])
    assert point in planned.kind.points


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"fault": "F4", "at": {"point": "net.gw_sim.answer"}}, "F4 does not fire at"),
        ({"fault": "F4", "variant": "gw_sim", "at": {"point": "net.gw_sim.request"}}, "no variant"),
        (
            {"fault": "F4", "at": {"point": "net.gw_sim.request"}, "params": {"copy_delay_ms": -1}},
            "copy_delay_ms must be at least 0",
        ),
    ],
)
def test_f4_where_or_how_the_catalog_does_not_allow_is_refused(
    entry: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        parse_plan([entry])


def test_f4s_copy_delay_is_drawn_from_the_runs_fault_generator_when_the_plan_omits_it() -> None:
    """0006: `copy_delay_ms` defaults to a seeded draw from 5 to 50 (0009, never global)."""
    plan = parse_plan(
        [
            {"fault": "F4", "at": {"point": "net.gw_sim.request"}},
            {"fault": "F4", "at": {"point": "net.agent_gw.request"}},
            {"fault": "F4", "at": {"point": "net.gw_sim.request"}, "params": {"copy_delay_ms": 0}},
            {"fault": "F2", "at": {"point": "net.gw_sim.answer"}},
        ]
    )
    with pytest.raises(ValueError, match="drawn when the run starts"):
        plan[0].param("copy_delay_ms")
    seeded = seed_parameters(plan, Generators(7).get("faults"))
    drawn = [planned.param("copy_delay_ms") for planned in seeded[:3]]
    assert all(5 <= delay <= 50 for delay in drawn[:2]) and drawn[2] == 0
    assert seeded[3] == plan[3], "a fault without seeded parameters is left as it is"
    again = seed_parameters(plan, Generators(7).get("faults"))
    assert [planned.params for planned in again] == [planned.params for planned in seeded]
    others = {
        seed_parameters(plan, Generators(seed).get("faults"))[0].param("copy_delay_ms")
        for seed in range(30)
    }
    assert len(others) > 1, "the delay follows the seed"


@pytest.mark.parametrize(
    ("point", "gateway"),
    [
        ("net.agent_gw.request", False),
        ("net.gw_sim.request", False),
        ("net.agent_sim.request", True),
    ],
)
def test_f4_copies_only_on_the_configurations_hops(point: str, gateway: bool) -> None:
    plan = parse_plan([{"fault": "F4", "at": {"point": point}}])
    with pytest.raises(ValueError, match=re.escape(f"F4 at {point}")):
        check_configuration(plan, gateway=gateway)


# F5: late commit -------------------------------------------------------------------------


def params(**changes: int) -> Params:
    """dev-001's timing by default: T 2,000, D 10,000, L 0."""
    return Params(**changes)


@pytest.mark.parametrize("variant", [None, "in_contract", "beyond_deadline"])
def test_f5_is_built_where_writes_reach_the_simulator(variant: str | None) -> None:
    point = "sim.write.received"
    (planned,) = parse_plan([{"fault": "F5", "variant": variant, "at": {"point": point}}])
    assert point in planned.kind.points


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"fault": "F5", "at": {"point": "net.gw_sim.request"}}, "F5 does not fire at"),
        ({"fault": "F5", "variant": "late", "at": {"point": "sim.write.received"}}, "no variant"),
        (
            {"fault": "F5", "at": {"point": "sim.write.received"}, "params": {"delay_ms": -5}},
            "delay_ms must be at least 0",
        ),
    ],
)
def test_f5_where_or_how_the_catalog_does_not_allow_is_refused(
    entry: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        parse_plan([entry])


def test_f5_holds_a_write_for_5000_ms_unless_the_plan_says_otherwise() -> None:
    default, chosen = parse_plan(
        [
            {"fault": "F5", "at": {"point": "sim.write.received"}},
            {"fault": "F5", "at": {"point": "sim.write.received"}, "params": {"delay_ms": 7_000}},
        ]
    )
    assert (default.param("delay_ms"), chosen.param("delay_ms")) == (5_000, 7_000)


@pytest.mark.parametrize(
    ("variant", "delay_ms", "accepted"),
    [
        (None, 5_000, True),
        ("in_contract", 2_001, True),
        ("in_contract", 9_999, True),
        ("in_contract", 2_000, False),
        ("in_contract", 10_000, False),
        ("in_contract", 15_000, False),
        ("beyond_deadline", 10_001, True),
        ("beyond_deadline", 20_000, True),
        ("beyond_deadline", 10_000, False),
        ("beyond_deadline", 5_000, False),
    ],
)
def test_f5s_delay_must_fit_its_variant_against_t_and_d(
    variant: str | None, delay_ms: int, accepted: bool
) -> None:
    """0006: in contract, T < delay < D; beyond the deadline, delay > D. T 2,000, D 10,000."""
    plan = parse_plan(
        [
            {
                "fault": "F5",
                "variant": variant,
                "at": {"point": "sim.write.received"},
                "params": {"delay_ms": delay_ms},
            }
        ]
    )
    if accepted:
        check_run(plan, params=params(), contract=Contract.C2)
    else:
        with pytest.raises(ValueError, match="F5"):
            check_run(plan, params=params(), contract=Contract.C2)


@pytest.mark.parametrize("deadline_ms", [10_000, 4_000])
def test_f5_beyond_the_deadline_must_set_its_delay(deadline_ms: int) -> None:
    """0006 gives only the in-contract default (5,000). Beyond the deadline the plan must
    set `delay_ms`, even where that default would exceed a short D."""
    plan = parse_plan(
        [{"fault": "F5", "variant": "beyond_deadline", "at": {"point": "sim.write.received"}}]
    )
    with pytest.raises(ValueError, match=r"F5 beyond_deadline needs delay_ms set"):
        check_run(plan, params=params(commit_deadline_ms=deadline_ms), contract=Contract.C1)


def test_f5_fires_with_and_without_a_gateway() -> None:
    plan = parse_plan([{"fault": "F5", "at": {"point": "sim.write.received"}}])
    check_configuration(plan, gateway=True)
    check_configuration(plan, gateway=False)


# F10: status answer delayed or stale -----------------------------------------------------

F10_AT_ITS_POINT = [
    {"fault": "F10", "variant": "delayed", "at": {"point": "sim.status.answer"}},
    {"fault": "F10", "variant": "stale_within_lag", "at": {"point": "sim.status.answer"}},
    {"fault": "F10", "variant": "stale_beyond_lag", "at": {"point": "sim.write.received"}},
]
"""Each F10 variant at its own point: delayed and stale within L where a status answer is
produced, stale beyond L where a write reaches the destination (0013)."""


@pytest.mark.parametrize("entry", F10_AT_ITS_POINT, ids=lambda entry: str(entry["variant"]))
def test_each_f10_variant_is_built_at_its_own_point(entry: dict[str, Any]) -> None:
    (planned,) = parse_plan([entry])
    assert planned.at.point in planned.kind.points
    assert BUILT[FaultId.F10].points == {"sim.status.answer", "sim.write.received"}


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"fault": "F10", "at": {"point": "sim.status.answer"}}, "F10 needs a variant"),
        (
            {"fault": "F10", "variant": "stale", "at": {"point": "sim.status.answer"}},
            "no variant 'stale'",
        ),
        (
            {"fault": "F10", "variant": "delayed", "at": {"point": "net.gw_sim.answer"}},
            "F10 does not fire at",
        ),
        (
            {"fault": "F10", "variant": "delayed", "at": {"point": "sim.write.received"}},
            re.escape("F10 delayed fires at ['sim.status.answer'], not sim.write.received"),
        ),
        (
            {"fault": "F10", "variant": "stale_within_lag", "at": {"point": "sim.write.received"}},
            re.escape("F10 stale_within_lag fires at ['sim.status.answer']"),
        ),
        (
            {"fault": "F10", "variant": "stale_beyond_lag", "at": {"point": "sim.status.answer"}},
            re.escape(
                "F10 stale_beyond_lag fires at ['sim.write.received'], not sim.status.answer"
            ),
        ),
        (
            {
                "fault": "F10",
                "variant": "delayed",
                "at": {"point": "sim.status.answer"},
                "params": {"count": 0},
            },
            "count must be at least 1",
        ),
    ],
)
def test_f10_where_or_how_the_catalog_does_not_allow_is_refused(
    entry: dict[str, object], message: str
) -> None:
    """Before record 0013, stale beyond L fired at a status answer, so it froze a state
    that already listed the order. It now fires only where a write arrives."""
    with pytest.raises(ValidationError, match=message):
        parse_plan([entry])


def test_f10_shapes_20_answers_unless_the_plan_says_otherwise() -> None:
    default, chosen, stale = parse_plan(
        [
            {"fault": "F10", "variant": "delayed", "at": {"point": "sim.status.answer"}},
            {
                "fault": "F10",
                "variant": "delayed",
                "at": {"point": "sim.status.answer"},
                "params": {"count": 3},
            },
            {"fault": "F10", "variant": "stale_beyond_lag", "at": {"point": "sim.write.received"}},
        ]
    )
    assert (default.param("count"), chosen.param("count"), stale.param("count")) == (20, 3, 20)


@pytest.mark.parametrize("entry", F10_AT_ITS_POINT, ids=lambda entry: str(entry["variant"]))
@pytest.mark.parametrize("contract", [Contract.C0, Contract.C2, Contract.C3])
def test_f10_runs_only_where_the_destination_answers_status(
    contract: Contract, entry: dict[str, Any]
) -> None:
    """Every variant acts on status answers, wherever it fires."""
    plan = parse_plan([entry])
    check_run(plan, params=params(status_lag_ms=3_000), contract=Contract.C1)
    with pytest.raises(ValueError, match=f"F10 needs a status lookup, which {contract}"):
        check_run(plan, params=params(status_lag_ms=3_000), contract=contract)


def test_f10_stale_within_the_lag_needs_a_lag() -> None:
    """0006, notes: the default L is 0, so these cells set `status_lag_ms` to 3,000."""
    plan = parse_plan(
        [{"fault": "F10", "variant": "stale_within_lag", "at": {"point": "sim.status.answer"}}]
    )
    with pytest.raises(ValueError, match="needs status_lag_ms above 0"):
        check_run(plan, params=params(), contract=Contract.C1)
    check_run(plan, params=params(status_lag_ms=3_000), contract=Contract.C1)


@pytest.mark.parametrize("variant", ["delayed", "stale_beyond_lag"])
def test_f10_beyond_the_lag_or_delayed_needs_no_lag(variant: str) -> None:
    (entry,) = [entry for entry in F10_AT_ITS_POINT if entry["variant"] == variant]
    check_run(parse_plan([entry]), params=params(), contract=Contract.C1)


@pytest.mark.parametrize("entry", F10_AT_ITS_POINT, ids=lambda entry: str(entry["variant"]))
def test_f10_fires_with_and_without_a_gateway(entry: dict[str, Any]) -> None:
    plan = parse_plan([entry])
    check_configuration(plan, gateway=True)
    check_configuration(plan, gateway=False)
