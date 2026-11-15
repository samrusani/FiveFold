"""The fault plan of each expected-outcome cell (0006): where each fault fires, by
configuration, and that the catalog accepts every plan."""

from __future__ import annotations

import pytest

from fivefold.domain.contracts import Contract
from fivefold.domain.scenario import load_scenario_file
from fivefold.faults.catalog import BUILT, FaultId
from fivefold.faults.plan import check_configuration, check_run, parse_plan
from fivefold.harness.cells import Cell, cell_plan, cell_scenario, load_expected
from fivefold.harness.demo import DEV_001


def cell(fault: str, variant: str | None, config: str, contract: Contract = Contract.C2) -> Cell:
    return Cell(
        fault=fault,
        variant=variant,
        contract=contract,
        config=config,
        verdict="pass",
        failing=(),
    )


@pytest.mark.parametrize("expected", load_expected().cells, ids=lambda expected: expected.name)
def test_every_committed_cells_plan_is_one_the_catalog_and_the_run_accept(expected: Cell) -> None:
    planned = parse_plan(cell_plan(expected))
    assert expected.fault in {str(entry.fault) for entry in planned}
    check_configuration(planned, gateway=expected.config != "B0")
    scenario = cell_scenario(expected, load_scenario_file(DEV_001))
    check_run(planned, params=scenario.params, contract=expected.contract)


@pytest.mark.parametrize(
    ("fault", "variant", "config", "point", "match", "planned"),
    [
        ("F6", "early", "G2", "gw.dispatch.before_check", {}, "early"),
        ("F6", "early", "B0", "net.agent_sim.answer", {"api": "request_approval"}, "early"),
        ("F6", "late", "G2", "net.gw_sim.request", {"api": "submit_purchase_order"}, "late"),
        ("F6", "late", "B0", "net.agent_sim.request", {"api": "submit_purchase_order"}, "late"),
        ("F7", "early_price_up", "G2", "gw.dispatch.before_check", {}, "price_up"),
        (
            "F7",
            "early_harmless",
            "B0",
            "net.agent_sim.answer",
            {"api": "request_approval"},
            "harmless",
        ),
        (
            "F7",
            "late_price_up",
            "B0",
            "net.agent_sim.request",
            {"api": "submit_purchase_order"},
            "price_up",
        ),
        ("F12", "gw_sim", "G2", "net.gw_sim.request", {"api": "submit_purchase_order"}, None),
        ("F12", "agent_gw", "G2", "net.agent_gw.request", {"api": "submit_purchase_order"}, None),
        ("F12", "agent_sim", "B0", "net.agent_sim.request", {"api": "submit_purchase_order"}, None),
        ("F6", "early", "G1", "gw.dispatch.before_check", {}, "early"),
        ("F6", "late", "G1", "net.gw_sim.request", {"api": "submit_purchase_order"}, "late"),
        ("F7", "early_harmless", "G1", "gw.dispatch.before_check", {}, "harmless"),
        ("F12", "gw_sim", "G1", "net.gw_sim.request", {"api": "submit_purchase_order"}, None),
        ("F12", "agent_gw", "G1", "net.agent_gw.request", {"api": "submit_purchase_order"}, None),
        ("F1", "a", "G1", "gw.dispatch.after_check", {"api": "submit_purchase_order"}, "a"),
        ("F1", "b", "G1", "gw.dispatch.after_record", {"api": "submit_purchase_order"}, "b"),
        ("F3", None, "G1", "gw.dispatch.after_answer", {"api": "submit_purchase_order"}, None),
    ],
)
def test_a_cell_fires_its_fault_where_record_0006_says(
    fault: str,
    variant: str,
    config: str,
    point: str,
    match: dict[str, str],
    planned: str | None,
) -> None:
    (entry,) = cell_plan(cell(fault, variant, config))
    assert entry["at"] == {"point": point, "match": match, "occurrence": 1}
    assert entry.get("variant") == planned


@pytest.mark.parametrize(
    ("fault", "variant", "config"),
    [
        ("F6", "midway", "G2"),
        ("F7", "early_price_down", "G2"),
        ("F7", "price_up", "B0"),
        ("F12", "agent_sim", "G2"),
        ("F12", "gw_sim", "B0"),
        ("F12", "agent_sim", "G1"),
        ("F1", "c", "G2"),
        ("F9", "other_args", "B0"),
        ("F8", None, "G2"),
    ],
)
def test_a_cell_with_no_plan_is_refused(fault: str, variant: str, config: str) -> None:
    with pytest.raises(ValueError):
        cell_plan(cell(fault, variant, config))


# F4 ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("variant", "config"), [("gw_sim", "G2"), ("agent_gw", "G2"), ("agent_sim", "B0")]
)
def test_f4_cells_copy_the_order_on_the_named_hop(variant: str, config: str) -> None:
    (entry,) = cell_plan(cell("F4", variant, config))
    assert entry == {
        "fault": "F4",
        "at": {
            "point": f"net.{variant}.request",
            "match": {"api": "submit_purchase_order"},
            "occurrence": 1,
        },
    }


@pytest.mark.parametrize(
    ("variant", "config"), [("agent_sim", "G2"), ("gw_sim", "B0"), ("agent_gw", "B0")]
)
def test_an_f4_cell_on_a_hop_its_configuration_lacks_is_refused(variant: str, config: str) -> None:
    with pytest.raises(ValueError, match="F4 cannot copy the order"):
        cell_plan(cell("F4", variant, config))


# F5 ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("config", ["G2", "B0"])
def test_f5_cells_hold_the_order_where_it_reaches_the_simulator(config: str) -> None:
    """0006 gives no default beyond the deadline; the cells use 20,000 ms, twice D."""
    (in_contract,) = cell_plan(cell("F5", "in_contract", config))
    (beyond,) = cell_plan(cell("F5", "beyond_deadline", config))
    at = {"point": "sim.write.received", "match": {"api": "submit_purchase_order"}, "occurrence": 1}
    assert in_contract == {"fault": "F5", "variant": "in_contract", "at": at, "params": {}}
    assert beyond == {
        "fault": "F5",
        "variant": "beyond_deadline",
        "at": at,
        "params": {"delay_ms": 20_000},
    }


def test_an_f5_cell_names_one_of_its_variants() -> None:
    with pytest.raises(ValueError, match="F5 has no variant 'late'"):
        cell_plan(cell("F5", "late", "G2"))


# F10 ---------------------------------------------------------------------------------------

STATUS_ANSWER = {
    "point": "sim.status.answer",
    "match": {"api": "get_order_status"},
    "occurrence": 1,
}
ORDER_ARRIVES = {
    "point": "sim.write.received",
    "match": {"api": "submit_purchase_order"},
    "occurrence": 1,
}


@pytest.mark.parametrize(
    ("variant", "at", "params"),
    [
        ("delayed", STATUS_ANSWER, {"count": 5}),
        ("stale_within_lag", STATUS_ANSWER, None),
        ("stale_beyond_lag", ORDER_ARRIVES, None),
    ],
)
def test_f10_cells_pair_it_with_f2_so_g2_probes(
    variant: str, at: dict[str, object], params: dict[str, int] | None
) -> None:
    """0006, notes: F10 needs a status probe, so its cells pair it with F2. Record 0013: the
    delayed cells set `count` to 5, and stale beyond L fires as the order reaches the
    simulator, so the state it freezes does not list the order."""
    lost, status = cell_plan(cell("F10", variant, "G2", Contract.C1))
    assert lost == cell_plan(cell("F2", None, "G2", Contract.C1))[0]
    expected: dict[str, object] = {"fault": "F10", "variant": variant, "at": at}
    if params is not None:
        expected["params"] = params
    assert status == expected


def test_the_delayed_cells_count_stays_below_max_probes_and_the_default_stays_20() -> None:
    """0013: with `count` at C1's `max_probes`, every probe goes unanswered and G2
    escalates. The delayed cells set 5; stale beyond L keeps the catalog's 20, so its
    answers stay stale until an empty one is past the settle time."""
    scenario = load_scenario_file(DEV_001)
    delayed = parse_plan(cell_plan(cell("F10", "delayed", "G2", Contract.C1)))[1]
    stale = parse_plan(cell_plan(cell("F10", "stale_beyond_lag", "G2", Contract.C1)))[1]
    assert delayed.param("count") == 5 < scenario.params.max_probes == 20
    assert stale.param("count") == BUILT[FaultId.F10].params["count"] == 20


@pytest.mark.parametrize(
    ("variant", "config", "message"),
    [
        ("stale", "G2", "no variant"),
        ("delayed", "B0", "never calls the status tool"),
        ("stale_beyond_lag", "B0", "never calls the status tool"),
        ("stale_within_lag", "G1", "never asks for the destination's status"),
        ("stale_beyond_lag", "G1", "never asks for the destination's status"),
    ],
)
def test_an_f10_cell_needs_a_variant_and_a_status_probe(
    variant: str, config: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        cell_plan(cell("F10", variant, config, Contract.C1))


def test_only_the_stale_within_lag_cells_change_the_scenario() -> None:
    """0006, notes: they set `status_lag_ms` to 3,000, because dev-001's L is 0."""
    scenario = load_scenario_file(DEV_001)
    within = cell_scenario(cell("F10", "stale_within_lag", "G2", Contract.C1), scenario)
    assert within.params.status_lag_ms == 3_000
    assert within.model_dump(exclude={"params"}) == scenario.model_dump(exclude={"params"})
    others = (
        cell("F10", "delayed", "G2", Contract.C1),
        cell("F10", "stale_beyond_lag", "G2", Contract.C1),
        cell("F2", None, "G2"),
    )
    for other in others:
        assert cell_scenario(other, scenario) == scenario
