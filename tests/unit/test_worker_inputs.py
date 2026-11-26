"""Invalid agent arguments and a fault with no target stay within the tool contract."""

import asyncio
import json
from typing import Any
from unittest.mock import create_autospec

import mcp_types as types
import pytest
from pydantic import ValidationError

from fivefold.agents.tools import Timeouts, gateway_tools
from fivefold.domain.api import FactRead, SubmitPurchaseOrder
from fivefold.domain.scenario import load_scenario_file
from fivefold.faults.plan import Fired, parse_plan
from fivefold.gateway.tools import PurchaseOrderArgs
from fivefold.harness.demo import DEV_001
from fivefold.harness.fault_effects import SimulatorFaults
from fivefold.harness.log import MemoryHarnessLog
from fivefold.harness.scheduler import Scheduler
from fivefold.sim import Control


@pytest.mark.parametrize("model", [PurchaseOrderArgs, SubmitPurchaseOrder])
def test_order_amount_must_fit_the_existing_json_encoder(
    model: type[PurchaseOrderArgs | SubmitPurchaseOrder], monkeypatch: pytest.MonkeyPatch
) -> None:
    from fivefold.domain import json_storage

    terms: dict[str, object] = {
        "intent_id": "REQ-1",
        "supplier_id": "S-1",
        "sku": "W-100",
        "currency": "USD",
        "cost_center": "CC-OPS",
        "quantity": 7,
        "unit_price_minor": 6,
    }
    if model is SubmitPurchaseOrder:
        terms.update(approval_id="APR-0001", basis={})

    def encoder(value: int) -> str:
        if value == 42:
            raise ValueError("integer encoder refused the derived amount")
        return str(value)

    monkeypatch.setattr(json_storage, "str", encoder, raising=False)
    with pytest.raises(ValidationError, match="too large for the run's JSON records"):
        model.model_validate(terms)
    model.model_validate({**terms, "quantity": 8})


@pytest.mark.parametrize(("wait", "budget"), [("bad", 10), (-1, 10), (7, 17), (80, 70)])
def test_invalid_waits_do_not_change_arguments_or_transport_budget(
    wait: object,
    budget: int,
) -> None:
    seen = []

    class Caller:
        async def call_tool(
            self, name: str, arguments: dict[str, Any], timeout_ms: int
        ) -> types.CallToolResult:
            seen.append((name, arguments, timeout_ms))
            answer = types.TextContent(type="text", text='{"reason":"invalid_args"}')
            return types.CallToolResult(content=[answer], is_error=True)

    listed = types.ListToolsResult(
        tools=[
            types.Tool.model_validate(
                {"name": "get_action_status", "inputSchema": {"type": "object"}}
            )
        ]
    )
    (tool,) = gateway_tools(Caller(), listed, Timeouts(10, 20, 60))
    args = {"action_id": "A-0001", "wait_ms": wait}
    assert json.loads(asyncio.run(tool.ainvoke(args))) == {"reason": "invalid_args"}
    assert seen == [("get_action_status", args, budget)]


def test_f7_uses_the_no_intent_ceiling_when_the_write_names_an_unknown_intent() -> None:
    control = create_autospec(Control, instance=True)
    control.current_fact.return_value = FactRead(
        key="quote/S-1/W-100", version=1, data={"unit_price_minor": 540}
    )
    effects = SimulatorFaults(Scheduler(), control, MemoryHarnessLog(), load_scenario_file(DEV_001))
    (planned,) = parse_plan(
        [
            {
                "fault": "F7",
                "variant": "price_up",
                "at": {"point": "net.agent_sim.request"},
                "params": {"percent": 5},
            }
        ]
    )
    effects.apply(Fired(planned, 1), {"intent_id": "unknown", "supplier_id": "S-1", "sku": "W-100"})
    control.change_fact.assert_called_once_with(
        "quote/S-1/W-100", {"unit_price_minor": 567}, actor="fault-plan", vt=0
    )


@pytest.mark.parametrize("variant", ["late_relevant", "late_harmless"])
def test_f8_leaves_a_missing_memory_fact_alone(variant: str) -> None:
    control = create_autospec(Control, instance=True)
    control.current_fact.return_value = None
    effects = SimulatorFaults(Scheduler(), control, MemoryHarnessLog(), load_scenario_file(DEV_001))
    (planned,) = parse_plan(
        [
            {
                "fault": "F8",
                "variant": variant,
                "at": {"point": "net.agent_sim.request"},
            }
        ]
    )
    effects.apply(Fired(planned, 1), {"supplier_id": "S-2", "sku": "W-100"})
    control.correct_memory.assert_not_called()
