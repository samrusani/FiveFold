"""The scripted agent policy of record 0008, as a generator of tool calls.

The policy is the same in every configuration, except where 0008 says B0 differs:

1. Read the quotes of the intent's allowed suppliers and the budget. Choose the cheapest
   acceptable quote that fits the budget (with `rank` 1, the second-cheapest: F9's
   `different_args` variant for the second worker).
2. B0 only: request approval, then wait with `get_approval_status` for at most the
   scenario limit or 60,000 ms, at most 3 times (0015).
3. Submit the write once, with no optional fields. G modes cite exactly the reads of the
   operation's dependencies; B0 passes the approval id and those reads' versions.
4. Act on the result, as 0008's table says.
5. Report. G modes first call `get_action_status` once per action with `wait_ms = 0`. If
   G2 refuses the report as inconsistent, correct the named claims and resubmit, at
   most twice.

`scripted_policy` returns a generator that yields `ToolCall`s and is sent each call's
result: the tool's JSON, parsed. The scripted model replays it over the conversation's
tool results to find the next call, so the policy keeps no state outside the
conversation, and a worker resumed from its checkpoint (F11) decides the same way.

An action rejected with reason `run_closed`, which another worker's report can cause
(0010), stops work on the intent, as 0008's table says since record 0011 amended it.
"""

from __future__ import annotations

from collections.abc import Generator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from fivefold.domain.rules import Operation, dependencies
from fivefold.domain.scenario import Intent, PurchaseOrderAccept, ReservationAccept

WAIT_MS = 60_000
"""The maximum the scripted policy asks for in each status wait (0015)."""

MAX_WAITS = 3
"""Status waits per action, and approval waits per request (0008)."""

MAX_STALE_RETRIES = 2
"""Re-reads after a stale-input rejection (0008)."""

MAX_REPORT_CORRECTIONS = 2
"""Resubmissions after G2 refuses a report as inconsistent (0008)."""

APPROVAL_REASONS = frozenset(
    {"approval_denied", "approval_revoked", "approval_expired", "approval_timeout"}
)
"""Rejections that end work on an intent (0008)."""

WAITING_STATES = frozenset(
    {"pending_approval", "proposed", "authorized", "dispatched", "outcome_unknown"}
)
"""States the agent waits on with `get_action_status` (0008)."""

UNSETTLED_STATES = frozenset({"dispatched", "outcome_unknown"})
"""States in which a write may or may not have committed: the claim is `unknown`."""

REREAD_REFUSALS = frozenset({"invalid_args", "unknown_read", "missing_basis"})
"""Refusals after which the agent re-reads and tries once more (0008)."""

INPUT_CHECKS = frozenset({"stale_input", "undeclared_input", "unobserved_input"})
"""A conditional write's failed checks about its inputs (0005)."""

Result = Mapping[str, Any]


class Mode(StrEnum):
    """Which tool surface the agent has (0008)."""

    GATEWAY = "gateway"
    """G1 and G2: the gateway's tools."""

    BASELINE = "baseline"
    """B0: the simulator's API and the report sink."""


@dataclass(frozen=True)
class ToolCall:
    """One tool call the policy makes."""

    name: str
    args: Mapping[str, object]


@dataclass
class _Outcome:
    """Where work on one intent ended, as the claim the report will make."""

    status: str
    effect_ids: list[str] = field(default_factory=list)
    note: str = ""
    action_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Choice:
    """The option chosen for an intent, and the reads it relies on."""

    args: dict[str, str | int]
    reads: tuple[Result, ...]


Policy = Generator[ToolCall, Result, None]
_Step = Generator[ToolCall, Result, Any]


def scripted_policy(
    intents: Sequence[Intent],
    *,
    mode: Mode,
    rank: int = 0,
    max_wait_ms: int = WAIT_MS,
    memory_enabled: bool = False,
) -> Policy:
    """The policy for one worker, with its scenario limit and option rank."""
    wait_ms = min(WAIT_MS, max_wait_ms)
    outcomes: dict[str, _Outcome] = {}
    for intent in intents:
        if mode is Mode.GATEWAY:
            outcomes[intent.id] = yield from _gateway_intent(intent, rank, wait_ms, memory_enabled)
        else:
            outcomes[intent.id] = yield from _baseline_intent(intent, rank, wait_ms, memory_enabled)
    if mode is Mode.GATEWAY:
        for outcome in outcomes.values():
            yield from _refresh(outcome)
    yield from _report(outcomes)


# Reading and choosing --------------------------------------------------------------------


def _is_fact(result: Result) -> bool:
    """Whether a read returned a fact: G modes give a read id, B0 an answer `fact`."""
    return "read_id" in result or result.get("answer") == "fact"


def _read_and_choose(intent: Intent, rank: int, memory_enabled: bool) -> _Step:
    """Read what the intent depends on and choose an option, or return None."""
    accept = intent.accept
    if isinstance(accept, ReservationAccept):
        stock = yield ToolCall(
            "get_inventory", {"warehouse_id": accept.warehouse_id, "sku": accept.sku}
        )
        if not _is_fact(stock) or int(stock["data"]["available"]) < accept.quantity:
            return None
        args: dict[str, str | int] = {
            "warehouse_id": accept.warehouse_id,
            "sku": accept.sku,
            "quantity": accept.quantity,
        }
        return _Choice(args, (stock,))
    assert isinstance(accept, PurchaseOrderAccept)
    quotes: list[tuple[str, Result]] = []
    for supplier_id in sorted(set(accept.suppliers)):
        quote = yield ToolCall("get_quote", {"supplier_id": supplier_id, "sku": accept.sku})
        if _is_fact(quote):
            quotes.append((supplier_id, quote))
    budget = yield ToolCall("get_budget", {"cost_center": accept.cost_center})
    if not _is_fact(budget):
        return None
    left = int(budget["data"]["limit_minor"]) - int(budget["data"]["committed_minor"])
    acceptable: list[tuple[int, str, Result]] = []
    for supplier_id, quote in quotes:
        data = quote["data"]
        price = int(data["unit_price_minor"])
        if (
            data.get("status") == "active"
            and price <= accept.max_unit_price_minor
            and data.get("currency") == accept.currency
            and accept.quantity >= int(data["min_quantity"])
            and accept.quantity * price <= left
        ):
            acceptable.append((price, supplier_id, quote))
    acceptable.sort(key=lambda option: (option[0], option[1]))
    if rank >= len(acceptable):
        return None
    price, supplier_id, quote = acceptable[rank]
    args = {
        "supplier_id": supplier_id,
        "sku": accept.sku,
        "quantity": accept.quantity,
        "unit_price_minor": price,
        "currency": accept.currency,
        "cost_center": accept.cost_center,
    }
    reads: tuple[Result, ...] = (quote, budget)
    if memory_enabled:
        memory = yield ToolCall("get_memory", {"supplier_id": supplier_id, "sku": accept.sku})
        if not _is_fact(memory) or memory["data"].get("order_channel") not in ("edi", "portal"):
            return None
        reads += (memory,)
    return _Choice(args, reads)


# G modes ---------------------------------------------------------------------------------


def _gateway_intent(intent: Intent, rank: int, wait_ms: int, memory_enabled: bool) -> _Step:
    """Steps 1, 3 and 4 for one intent, through the gateway's tools."""
    actions: list[str] = []
    stale_retries = failure_retries = reread_retries = 0
    while True:
        choice = yield from _read_and_choose(intent, rank, memory_enabled)
        if choice is None:
            return _Outcome("not_done", note="No acceptable option fits.", action_ids=actions)
        cited = [read["read_id"] for read in choice.reads]
        result = yield ToolCall(
            str(intent.operation), {"intent_id": intent.id, **choice.args, "based_on": cited}
        )
        waits: dict[str, int] = {}
        while True:
            if result.get("result") == "refused":
                reason = str(result.get("reason"))
                details = result.get("details") or {}
                if reason == "intent_busy":
                    holder = str(details["action_id"])
                    if holder not in actions:
                        actions.append(holder)
                    if waits.get(holder, 0) >= MAX_WAITS:
                        return _settled_outcome(str(details.get("state")), actions, None)
                    waits[holder] = waits.get(holder, 0) + 1
                    result = yield ToolCall(
                        "get_action_status", {"action_id": holder, "wait_ms": wait_ms}
                    )
                    continue
                if reason == "intent_done":
                    holder = str(details["action_id"])
                    if holder not in actions:
                        actions.append(holder)
                    effect = str(details["effect_id"])
                    return _Outcome("done", [effect], f"Done as {effect}.", actions)
                if reason in REREAD_REFUSALS and reread_retries < 1:
                    reread_retries += 1
                    break
                return _Outcome("not_done", note=f"Refused: {reason}.", action_ids=actions)
            action_id = str(result["action_id"])
            if action_id not in actions:
                actions.append(action_id)
            state = str(result["state"])
            if state == "confirmed":
                effect = str(result["effect_id"])
                return _Outcome("done", [effect], f"Done as {effect}.", actions)
            if state in WAITING_STATES:
                if waits.get(action_id, 0) >= MAX_WAITS:
                    return _settled_outcome(state, actions, None)
                waits[action_id] = waits.get(action_id, 0) + 1
                result = yield ToolCall(
                    "get_action_status", {"action_id": action_id, "wait_ms": wait_ms}
                )
                continue
            reason = str(result.get("reason"))
            if result.get("stale") or reason == "stale_input":
                if stale_retries < MAX_STALE_RETRIES:
                    stale_retries += 1
                    break
                return _Outcome("not_done", note="Inputs kept changing.", action_ids=actions)
            if state == "rejected" and reason in APPROVAL_REASONS:
                return _Outcome("not_done", note=f"Stopped: {reason}.", action_ids=actions)
            if state == "failed" and failure_retries < 1:
                failure_retries += 1
                break
            return _Outcome("not_done", note=f"Ended {state}: {reason}.", action_ids=actions)


def _settled_outcome(state: str, actions: list[str], effect: str | None) -> _Outcome:
    """The claim for an action the agent stopped waiting on (0008, step 5)."""
    if state == "confirmed" and effect is not None:
        return _Outcome("done", [effect], f"Done as {effect}.", actions)
    if state in UNSETTLED_STATES:
        return _Outcome("unknown", note="The outcome is not known yet.", action_ids=actions)
    return _Outcome("not_done", note=f"Stopped while {state}.", action_ids=actions)


def _refresh(outcome: _Outcome) -> _Step:
    """Step 5: read each action's state once, with no wait, and settle the claim."""
    if not outcome.action_ids:
        return None
    effects: list[str] = []
    unsettled = False
    for action_id in outcome.action_ids:
        result = yield ToolCall("get_action_status", {"action_id": action_id, "wait_ms": 0})
        state = str(result.get("state"))
        if state == "confirmed":
            effects.append(str(result["effect_id"]))
        elif state in UNSETTLED_STATES:
            unsettled = True
    if effects:
        outcome.status, outcome.effect_ids = "done", effects
        outcome.note = f"Done as {', '.join(effects)}."
    elif unsettled:
        outcome.status, outcome.effect_ids = "unknown", []
        outcome.note = "The outcome is not known yet."
    elif outcome.status != "not_done":
        outcome.status, outcome.effect_ids = "not_done", []
        outcome.note = "Nothing was committed."
    return None


# B0 --------------------------------------------------------------------------------------


def _scope(operation: Operation, args: Mapping[str, str | int]) -> dict[str, str | int]:
    """What an approval for exactly this write covers (0002, rule 5)."""
    if operation is Operation.RESERVE_INVENTORY:
        return {
            "warehouse_id": args["warehouse_id"],
            "sku": args["sku"],
            "max_quantity": args["quantity"],
        }
    return {
        "supplier_id": args["supplier_id"],
        "sku": args["sku"],
        "max_quantity": args["quantity"],
        "max_amount_minor": int(args["quantity"]) * int(args["unit_price_minor"]),
        "currency": args["currency"],
        "cost_center": args["cost_center"],
    }


def _covers(scope: Mapping[str, object], wanted: Mapping[str, object]) -> bool:
    """Whether an approval's scope still covers a write with scope `wanted`."""
    for name, value in wanted.items():
        held = scope.get(name)
        if name.startswith("max_"):
            if not isinstance(held, int) or not isinstance(value, int) or value > held:
                return False
        elif held != value:
            return False
    return True


def _basis(intent: Intent, choice: _Choice, memory_enabled: bool) -> dict[str, int]:
    """The versions of the reads the write relies on: its dependencies (0004)."""
    declared = dependencies(intent.operation, choice.args, memory_enabled=memory_enabled)
    return {dep.key: int(read["version"]) for dep, read in zip(declared, choice.reads, strict=True)}


def _baseline_intent(intent: Intent, rank: int, wait_ms: int, memory_enabled: bool) -> _Step:
    """Steps 1 to 4 for one intent, against the simulator's API."""
    approval: tuple[str, dict[str, str | int]] | None = None
    stale_retries = failure_retries = 0
    while True:
        choice = yield from _read_and_choose(intent, rank, memory_enabled)
        if choice is None:
            return _Outcome("not_done", note="No acceptable option fits.")
        wanted = _scope(intent.operation, choice.args)
        if approval is None or not _covers(approval[1], wanted):
            answer = yield ToolCall(
                "request_approval",
                {"intent_id": intent.id, "operation": str(intent.operation), "scope": wanted},
            )
            waits = 0
            while answer.get("status") == "pending" and waits < MAX_WAITS:
                waits += 1
                answer = yield ToolCall(
                    "get_approval_status",
                    {"approval_id": answer["approval_id"], "wait_ms": wait_ms},
                )
            if answer.get("status") != "approved":
                status = answer.get("status", answer.get("reason"))
                return _Outcome("not_done", note=f"Approval {status}.")
            approval = (str(answer["approval_id"]), wanted)
        result = yield ToolCall(
            str(intent.operation),
            {
                "intent_id": intent.id,
                "approval_id": approval[0],
                "basis": _basis(intent, choice, memory_enabled),
                **choice.args,
            },
        )
        kind = result.get("answer")
        if kind == "committed":
            effect = str(result["effect_id"])
            return _Outcome("done", [effect], f"Done as {effect}.")
        if kind == "in_progress":
            return _Outcome("unknown", note="The write is still in progress.")
        checks = {str(check.get("check")) for check in result.get("failed_checks", [])}
        if checks & INPUT_CHECKS:
            if stale_retries < MAX_STALE_RETRIES:
                stale_retries += 1
                continue
            return _Outcome("not_done", note="Inputs kept changing.")
        if checks:
            return _Outcome("not_done", note=f"Stopped: {', '.join(sorted(checks))}.")
        if failure_retries < 1:
            failure_retries += 1
            continue
        return _Outcome("not_done", note=f"Rejected: {result.get('reason')}.")


# The report ------------------------------------------------------------------------------


def _claim(intent_id: str, outcome: _Outcome) -> dict[str, object]:
    return {
        "intent_id": intent_id,
        "status": outcome.status,
        "effect_ids": list(outcome.effect_ids) if outcome.status == "done" else [],
        "note": outcome.note,
    }


def _from_records(recorded: Sequence[Mapping[str, Any]]) -> _Outcome:
    """The claim the gateway's records support, from a report refusal's details."""
    effects = [str(e) for action in recorded for e in action.get("effect_ids", [])]
    if effects:
        return _Outcome("done", effects, f"Done as {', '.join(effects)}.")
    if any(action.get("state") in UNSETTLED_STATES for action in recorded):
        return _Outcome("unknown", note="The outcome is not known yet.")
    return _Outcome("not_done", note="Nothing was committed.")


def _report(outcomes: dict[str, _Outcome]) -> _Step:
    """Submit the report; correct the named claims at most twice (0008, step 5)."""
    corrections = 0
    while True:
        claims = [_claim(intent_id, outcome) for intent_id, outcome in outcomes.items()]
        result = yield ToolCall("submit_final_report", {"claims": claims})
        if result.get("result") != "refused" or result.get("reason") != "report_inconsistent":
            return None
        if corrections >= MAX_REPORT_CORRECTIONS:
            return None
        corrections += 1
        for conflict in (result.get("details") or {}).get("conflicts", []):
            intent_id = str(conflict["intent_id"])
            outcomes[intent_id] = _from_records(conflict.get("recorded", []))
