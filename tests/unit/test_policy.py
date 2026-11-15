"""The scripted policy of record 0008, row by row, and the scripted model's replay."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from fivefold.agents.policy import Mode, ToolCall, scripted_policy
from fivefold.agents.scripted import ScriptError, call_id, next_call
from fivefold.domain.scenario import Scenario, scenario_from_data

Result = dict[str, Any]
QUOTES = {"S-1": 540, "S-2": 575}


@pytest.fixture
def scenario(dev_data: dict[str, object]) -> Scenario:
    return scenario_from_data(dev_data)


class World:
    """Answers the policy's calls: reads from fixed facts, everything else from queues."""

    def __init__(self, mode: Mode) -> None:
        self.mode = mode
        self.reads = 0
        self.queues: dict[str, list[Result]] = defaultdict(list)
        self.left = 80_000
        self.calls: list[ToolCall] = []

    def then(self, tool: str, *results: Result) -> World:
        self.queues[tool].extend(results)
        return self

    def answer(self, call: ToolCall) -> Result:
        self.calls.append(call)
        if call.name in ("get_quote", "get_budget"):
            self.reads += 1
            if call.name == "get_quote":
                supplier = str(call.args["supplier_id"])
                key = f"quote/{supplier}/W-100"
                data: dict[str, object] = {
                    "unit_price_minor": QUOTES[supplier],
                    "currency": "USD",
                    "min_quantity": 10,
                    "lead_time_days": 5,
                    "status": "active",
                }
            else:
                key = "budget/CC-OPS"
                data = {"limit_minor": 100_000, "committed_minor": 100_000 - self.left}
            if self.mode is Mode.GATEWAY:
                return {"read_id": f"R-{self.reads:04d}", "key": key, "version": 1, "data": data}
            return {"answer": "fact", "key": key, "version": 1, "data": data}
        queue = self.queues[call.name]
        if not queue:
            raise AssertionError(f"no answer queued for {call.name}")
        return queue.pop(0)

    def run(self, scenario: Scenario, *, rank: int = 0) -> list[ToolCall]:
        policy = scripted_policy(scenario.intents, mode=self.mode, rank=rank)
        try:
            call = next(policy)
            while True:
                call = policy.send(self.answer(call))
        except StopIteration:
            return self.calls


def action(state: str, reason: str = "", effect: str | None = None, **more: Any) -> Result:
    return {
        "result": "action",
        "action_id": more.pop("action_id", "A-0001"),
        "intent_id": "REQ-1",
        "state": state,
        "effect_id": effect,
        "reason": reason,
        "stale": more.pop("stale", []),
        **more,
    }


def refused(reason: str, **details: Any) -> Result:
    return {"result": "refused", "reason": reason, "details": details}


ACCEPTED = {"result": "accepted"}


def names(calls: list[ToolCall]) -> list[str]:
    return [call.name for call in calls]


def report(calls: list[ToolCall]) -> list[Mapping[str, Any]]:
    last = [call for call in calls if call.name == "submit_final_report"][-1]
    claims = last.args["claims"]
    assert isinstance(claims, list)
    return claims


# G modes ---------------------------------------------------------------------------------


def test_g_reads_cites_its_two_dependencies_and_reports_after_a_refresh(
    scenario: Scenario,
) -> None:
    world = World(Mode.GATEWAY)
    world.then("submit_purchase_order", action("confirmed", "committed", "PO-0001"))
    world.then("get_action_status", action("confirmed", "committed", "PO-0001"))
    world.then("submit_final_report", ACCEPTED)
    calls = world.run(scenario)
    assert names(calls) == [
        "get_quote",
        "get_quote",
        "get_budget",
        "submit_purchase_order",
        "get_action_status",
        "submit_final_report",
    ]
    order = calls[3].args
    assert order == {
        "intent_id": "REQ-1",
        "supplier_id": "S-1",
        "sku": "W-100",
        "quantity": 100,
        "unit_price_minor": 540,
        "currency": "USD",
        "cost_center": "CC-OPS",
        "based_on": ["R-0001", "R-0003"],
    }, "the cheapest quote, citing exactly its quote and the budget; no optional fields"
    assert calls[4].args == {"action_id": "A-0001", "wait_ms": 0}
    assert report(calls) == [
        {
            "intent_id": "REQ-1",
            "status": "done",
            "effect_ids": ["PO-0001"],
            "note": "Done as PO-0001.",
        }
    ]


def test_the_second_worker_of_f9_different_args_picks_the_second_cheapest(
    scenario: Scenario,
) -> None:
    world = World(Mode.GATEWAY)
    world.then("submit_purchase_order", action("confirmed", "committed", "PO-0001"))
    world.then("get_action_status", action("confirmed", "committed", "PO-0001"))
    world.then("submit_final_report", ACCEPTED)
    calls = world.run(scenario, rank=1)
    assert calls[3].args["supplier_id"] == "S-2"
    assert calls[3].args["based_on"] == ["R-0002", "R-0003"]


def test_no_option_that_fits_the_budget_means_no_write_and_not_done(scenario: Scenario) -> None:
    world = World(Mode.GATEWAY)
    world.left = 53_999
    world.then("submit_final_report", ACCEPTED)
    calls = world.run(scenario)
    assert names(calls) == ["get_quote", "get_quote", "get_budget", "submit_final_report"]
    assert report(calls)[0]["status"] == "not_done"


def test_an_unsettled_action_is_waited_on_three_times_then_reported_unknown(
    scenario: Scenario,
) -> None:
    world = World(Mode.GATEWAY)
    unknown = action("outcome_unknown", "timeout")
    world.then("submit_purchase_order", unknown)
    world.then("get_action_status", unknown, unknown, unknown, unknown)
    world.then("submit_final_report", ACCEPTED)
    calls = world.run(scenario)
    waits = [call.args["wait_ms"] for call in calls if call.name == "get_action_status"]
    assert waits == [60_000, 60_000, 60_000, 0]
    assert report(calls)[0]["status"] == "unknown"


def test_a_stale_rejection_is_read_again_at_most_twice(scenario: Scenario) -> None:
    world = World(Mode.GATEWAY)
    stale = [{"key": "quote/S-1/W-100", "cited_version": 1, "current_version": 2,
              "changed_fields": ["unit_price_minor"]}]  # fmt: skip
    world.then(
        "submit_purchase_order",
        action("rejected", "stale_input", stale=stale),
        action("rejected", "stale_input", stale=stale, action_id="A-0002"),
        action("rejected", "stale_input", stale=stale, action_id="A-0003"),
    )
    world.then(
        "get_action_status",
        action("rejected", "stale_input"),
        action("rejected", "stale_input", action_id="A-0002"),
        action("rejected", "stale_input", action_id="A-0003"),
    )
    world.then("submit_final_report", ACCEPTED)
    calls = world.run(scenario)
    assert names(calls).count("submit_purchase_order") == 3
    assert names(calls).count("get_budget") == 3, "each try reads again"
    assert report(calls)[0]["status"] == "not_done"


@pytest.mark.parametrize(
    "reason", ["approval_denied", "approval_revoked", "approval_expired", "approval_timeout"]
)
def test_a_rejection_for_authority_stops_work_on_the_intent(
    scenario: Scenario, reason: str
) -> None:
    world = World(Mode.GATEWAY)
    world.then("submit_purchase_order", action("rejected", reason))
    world.then("get_action_status", action("rejected", reason))
    world.then("submit_final_report", ACCEPTED)
    calls = world.run(scenario)
    assert names(calls).count("submit_purchase_order") == 1
    assert report(calls)[0]["status"] == "not_done"


def test_a_failure_for_another_reason_is_tried_again_once(scenario: Scenario) -> None:
    world = World(Mode.GATEWAY)
    world.then(
        "submit_purchase_order",
        action("failed", "destination_rejected"),
        action("failed", "destination_rejected", action_id="A-0002"),
    )
    world.then(
        "get_action_status",
        action("failed", "destination_rejected"),
        action("failed", "destination_rejected", action_id="A-0002"),
    )
    world.then("submit_final_report", ACCEPTED)
    calls = world.run(scenario)
    assert names(calls).count("submit_purchase_order") == 2
    assert report(calls)[0]["status"] == "not_done"


def test_a_busy_intent_is_waited_on_and_its_result_adopted(scenario: Scenario) -> None:
    world = World(Mode.GATEWAY)
    world.then(
        "submit_purchase_order", refused("intent_busy", action_id="A-0001", state="dispatched")
    )
    world.then(
        "get_action_status",
        action("confirmed", "committed", "PO-0001"),
        action("confirmed", "committed", "PO-0001"),
    )
    world.then("submit_final_report", ACCEPTED)
    calls = world.run(scenario)
    assert calls[4].args == {"action_id": "A-0001", "wait_ms": 60_000}
    assert report(calls)[0]["effect_ids"] == ["PO-0001"]


def test_a_done_intent_takes_the_existing_effect(scenario: Scenario) -> None:
    world = World(Mode.GATEWAY)
    world.then(
        "submit_purchase_order", refused("intent_done", action_id="A-0001", effect_id="PO-0001")
    )
    world.then("get_action_status", action("confirmed", "committed", "PO-0001"))
    world.then("submit_final_report", ACCEPTED)
    calls = world.run(scenario)
    assert names(calls).count("submit_purchase_order") == 1
    assert report(calls)[0]["effect_ids"] == ["PO-0001"]


def test_a_read_refusal_is_retried_once_and_other_refusals_stop(scenario: Scenario) -> None:
    world = World(Mode.GATEWAY)
    world.then(
        "submit_purchase_order",
        refused("unknown_read", read_ids=["R-0009"]),
        refused("run_closed"),
    )
    world.then("submit_final_report", ACCEPTED)
    calls = world.run(scenario)
    assert names(calls).count("submit_purchase_order") == 2
    assert names(calls)[-1] == "submit_final_report"
    assert report(calls)[0]["status"] == "not_done"


def test_an_inconsistent_report_is_corrected_from_the_records_at_most_twice(
    scenario: Scenario,
) -> None:
    world = World(Mode.GATEWAY)
    world.then("submit_purchase_order", action("pending_approval"))
    world.then("get_action_status", *[action("pending_approval")] * 4)
    conflict = refused(
        "report_inconsistent",
        conflicts=[
            {
                "intent_id": "REQ-1",
                "claimed": "not_done",
                "claimed_effect_ids": [],
                "recorded": [
                    {"action_id": "A-0001", "state": "confirmed", "effect_ids": ["PO-0001"]}
                ],
            }
        ],
    )
    world.then("submit_final_report", conflict, conflict, conflict)
    calls = world.run(scenario)
    reports = [call for call in calls if call.name == "submit_final_report"]
    assert len(reports) == 3, "the first report and two corrections"
    corrected = reports[1].args["claims"]
    assert isinstance(corrected, list)
    assert corrected[0]["status"] == "done" and corrected[0]["effect_ids"] == ["PO-0001"]


# B0 --------------------------------------------------------------------------------------


def approval(status: str, approval_id: str = "APR-0001") -> Result:
    return {"answer": "approval", "approval_id": approval_id, "status": status}


def committed(effect: str) -> Result:
    return {"answer": "committed", "effect_id": effect, "replay": False}


def test_b0_requests_approval_then_orders_with_the_approval_and_versions(
    scenario: Scenario,
) -> None:
    world = World(Mode.BASELINE)
    world.then("request_approval", approval("approved"))
    world.then("submit_purchase_order", committed("PO-0001"))
    world.then("submit_final_report", ACCEPTED)
    calls = world.run(scenario)
    assert names(calls) == [
        "get_quote",
        "get_quote",
        "get_budget",
        "request_approval",
        "submit_purchase_order",
        "submit_final_report",
    ]
    assert calls[3].args["scope"] == {
        "supplier_id": "S-1",
        "sku": "W-100",
        "max_quantity": 100,
        "max_amount_minor": 54_000,
        "currency": "USD",
        "cost_center": "CC-OPS",
    }
    order = dict(calls[4].args)
    assert order.pop("approval_id") == "APR-0001"
    assert order.pop("basis") == {"quote/S-1/W-100": 1, "budget/CC-OPS": 1}
    assert "idempotency_key" not in order, "no optional fields (0008)"
    assert report(calls)[0]["effect_ids"] == ["PO-0001"]


def test_b0_waits_three_times_for_a_pending_approval_then_stops(scenario: Scenario) -> None:
    world = World(Mode.BASELINE)
    world.then("request_approval", approval("pending"))
    world.then("get_approval_status", *[approval("pending")] * 3)
    world.then("submit_final_report", ACCEPTED)
    calls = world.run(scenario)
    waits = [call.args for call in calls if call.name == "get_approval_status"]
    assert waits == [{"approval_id": "APR-0001", "wait_ms": 60_000}] * 3
    assert "submit_purchase_order" not in names(calls)
    assert report(calls)[0]["status"] == "not_done"


def test_b0_reads_again_after_a_stale_precondition_and_keeps_a_covering_approval(
    scenario: Scenario,
) -> None:
    world = World(Mode.BASELINE)
    world.then("request_approval", approval("approved"))
    stale = {
        "answer": "rejected",
        "reason": "precondition_failed",
        "failed_checks": [{"check": "stale_input", "key": "quote/S-1/W-100"}],
    }
    world.then("submit_purchase_order", stale, committed("PO-0001"))
    world.then("submit_final_report", ACCEPTED)
    calls = world.run(scenario)
    assert names(calls).count("request_approval") == 1, "the approval still covers the order"
    assert names(calls).count("submit_purchase_order") == 2


# The scripted model's replay -------------------------------------------------------------


def test_the_model_replays_the_policy_over_the_conversation(scenario: Scenario) -> None:
    def policy() -> Any:
        return scripted_policy(scenario.intents, mode=Mode.GATEWAY)

    messages: list[Any] = [HumanMessage("task")]
    first = next_call(policy(), messages)
    assert first == ToolCall("get_quote", {"supplier_id": "S-1", "sku": "W-100"})
    messages += [
        AIMessage(content="", tool_calls=[{"name": "get_quote", "args": {}, "id": call_id(1)}]),
        ToolMessage(
            content='{"read_id": "R-0001", "key": "quote/S-1/W-100", "version": 1, "data": '
            '{"unit_price_minor": 540, "currency": "USD", "min_quantity": 10, "status": "active"}}',
            tool_call_id=call_id(1),
            name="get_quote",
        ),
    ]
    assert next_call(policy(), messages) == ToolCall(
        "get_quote", {"supplier_id": "S-2", "sku": "W-100"}
    )
    wrong = [*messages[:-1], ToolMessage(content="{}", tool_call_id="call-0099")]
    with pytest.raises(ScriptError, match="call-0099"):
        next_call(policy(), wrong)


def test_the_policy_is_a_pure_function_of_the_results(scenario: Scenario) -> None:
    """Two replays over the same results make the same calls: what F11's resume needs."""

    def drive() -> list[ToolCall]:
        world = World(Mode.GATEWAY)
        world.then("submit_purchase_order", action("confirmed", "committed", "PO-0001"))
        world.then("get_action_status", action("confirmed", "committed", "PO-0001"))
        world.then("submit_final_report", ACCEPTED)
        return world.run(scenario)

    assert drive() == drive()


Responder = Callable[[ToolCall], Result]
