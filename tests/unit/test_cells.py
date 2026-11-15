"""The fault plan of each expected-outcome cell (0006): where each fault fires, by
configuration, and that the catalog accepts every plan."""

from __future__ import annotations

import pytest

from fivefold.domain.contracts import Contract
from fivefold.faults.plan import check_configuration, parse_plan
from fivefold.harness.cells import Cell, cell_plan, load_expected


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
