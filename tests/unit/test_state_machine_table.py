"""The transition and grant tables against records 0002, 0003 and 0005.

The expected tables below are copied by hand from the records, so a change to the
module's tables fails here until the records change too.
"""

from __future__ import annotations

import itertools

import pytest

from fivefold.domain.contracts import Capability, Contract, ContractParams
from fivefold.gateway.state_machine import (
    FINAL_STATES,
    GRANT_TRANSITIONS,
    HOLDING_STATES,
    LIVE_STATES,
    TRANSITIONS,
    ActionState,
    CheckKind,
    GatewayRuleError,
    GatewaySettings,
    GrantState,
    IllegalTransition,
    Reason,
    ReconcileHow,
    check_grant_transition,
    check_transition,
    reconcile_how,
    required_check,
    transition_for,
)

S = ActionState
R = Reason
G = GrantState

# Record 0003, transition table: id, from, to, reason codes.
EXPECTED: dict[str, tuple[ActionState | None, ActionState, set[str]]] = {
    "T1": (None, S.PROPOSED, {"admitted"}),
    "T2": (S.PROPOSED, S.PENDING_APPROVAL, {"approval_requested"}),
    "T3": (S.PROPOSED, S.REJECTED, {"stale_input", "run_closed"}),
    "T4": (S.PENDING_APPROVAL, S.AUTHORIZED, {"approved"}),
    "T5": (
        S.PENDING_APPROVAL,
        S.REJECTED,
        {"approval_denied", "approval_timeout", "stale_input", "run_closed"},
    ),
    "T6": (S.AUTHORIZED, S.DISPATCHED, {"dispatched"}),
    "T7": (
        S.AUTHORIZED,
        S.REJECTED,
        {"approval_revoked", "approval_expired", "stale_input", "run_closed"},
    ),
    "T8": (S.DISPATCHED, S.CONFIRMED, {"committed"}),
    "T9": (S.DISPATCHED, S.FAILED, {"destination_rejected", "precondition_failed"}),
    "T10": (
        S.DISPATCHED,
        S.OUTCOME_UNKNOWN,
        {"timeout", "transport_error", "server_error", "in_progress", "recovered_open_attempt"},
    ),
    "T11": (S.OUTCOME_UNKNOWN, S.CONFIRMED, {"status_found", "retry_committed", "human_attested"}),
    "T12": (
        S.OUTCOME_UNKNOWN,
        S.FAILED,
        {"status_settled_empty", "retry_rejected", "human_attested"},
    ),
    "T13": (S.OUTCOME_UNKNOWN, S.DISPATCHED, {"retry"}),
}

# Reasons that need a capability, from 0003 ("C2 or C3 only", "C1 status") and 0005.
NEEDS: dict[str, Capability] = {
    "in_progress": Capability.IDEMPOTENCY_KEY,
    "retry": Capability.IDEMPOTENCY_KEY,
    "retry_committed": Capability.IDEMPOTENCY_KEY,
    "retry_rejected": Capability.IDEMPOTENCY_KEY,
    "status_found": Capability.STATUS,
    "status_settled_empty": Capability.STATUS,
    "precondition_failed": Capability.CONDITIONAL_WRITE,
}

# Record 0002, rule 14.
EXPECTED_GRANT_MOVES: set[tuple[GrantState | None, GrantState]] = {
    (None, G.PENDING),
    (G.PENDING, G.ACTIVE),
    (G.PENDING, G.DENIED),
    (G.PENDING, G.TIMED_OUT),
    (G.PENDING, G.RELEASED),
    (G.PENDING, G.REVOKED),
    (G.PENDING, G.EXPIRED),
    (G.ACTIVE, G.CONSUMED),
    (G.ACTIVE, G.RELEASED),
    (G.ACTIVE, G.REVOKED),
    (G.ACTIVE, G.EXPIRED),
    (G.CONSUMED, G.REVOKED),
    (G.CONSUMED, G.EXPIRED),
}


def test_the_table_is_record_0003s_table() -> None:
    actual = {t.tid: (t.source, t.target, {str(r) for r in t.reasons}) for t in TRANSITIONS}
    assert actual == EXPECTED


def test_state_sets() -> None:
    assert {S.CONFIRMED, S.REJECTED, S.FAILED} == FINAL_STATES
    assert len(LIVE_STATES) == 5
    assert LIVE_STATES | {S.CONFIRMED} == HOLDING_STATES


def test_final_states_have_no_outgoing_transition() -> None:
    assert not [t.tid for t in TRANSITIONS if t.source in FINAL_STATES]


@pytest.mark.parametrize("source", [None, *ActionState])
@pytest.mark.parametrize("target", list(ActionState))
def test_moves_outside_the_table_are_illegal(
    source: ActionState | None, target: ActionState
) -> None:
    listed = {(row[0], row[1]) for row in EXPECTED.values()}
    if (source, target) in listed:
        assert transition_for(source, target) is not None
        return
    assert transition_for(source, target) is None
    for reason, contract in itertools.product(Reason, Contract):
        with pytest.raises(IllegalTransition):
            check_transition(source, target, reason, contract)


@pytest.mark.parametrize("tid", list(EXPECTED))
@pytest.mark.parametrize("contract", list(Contract))
def test_each_reason_is_legal_only_on_its_transition_and_contracts(
    tid: str, contract: Contract
) -> None:
    source, target, reasons = EXPECTED[tid]
    for reason in Reason:
        allowed = str(reason) in reasons and (
            str(reason) not in NEEDS or contract.has(NEEDS[str(reason)])
        )
        if allowed:
            assert check_transition(source, target, reason, contract).tid == tid
        else:
            with pytest.raises(IllegalTransition):
                check_transition(source, target, reason, contract)


@pytest.mark.parametrize("contract", list(Contract))
def test_retries_and_in_progress_exist_only_at_c2_and_c3(contract: Contract) -> None:
    moves = [
        (S.OUTCOME_UNKNOWN, S.DISPATCHED, R.RETRY),
        (S.DISPATCHED, S.OUTCOME_UNKNOWN, R.IN_PROGRESS),
    ]
    for source, target, reason in moves:
        if contract in (Contract.C2, Contract.C3):
            check_transition(source, target, reason, contract)
        else:
            with pytest.raises(IllegalTransition):
                check_transition(source, target, reason, contract)


def test_human_attestation_works_at_every_level() -> None:
    for contract in Contract:
        check_transition(S.OUTCOME_UNKNOWN, S.CONFIRMED, R.HUMAN_ATTESTED, contract)
        check_transition(S.OUTCOME_UNKNOWN, S.FAILED, R.HUMAN_ATTESTED, contract)


def test_the_grant_table_is_record_0002s_table() -> None:
    actual = {
        (source, target) for source, targets in GRANT_TRANSITIONS.items() for target in targets
    }
    assert actual == EXPECTED_GRANT_MOVES
    for source, target in itertools.product([None, *GrantState], GrantState):
        if (source, target) in EXPECTED_GRANT_MOVES:
            check_grant_transition(source, target)
        else:
            with pytest.raises(IllegalTransition):
                check_grant_transition(source, target)


def test_a_consumed_grant_never_backs_another_action() -> None:
    assert GRANT_TRANSITIONS[G.CONSUMED] == {G.REVOKED, G.EXPIRED}
    for final in (G.DENIED, G.TIMED_OUT, G.RELEASED, G.REVOKED, G.EXPIRED):
        assert final not in GRANT_TRANSITIONS


def test_contract_presets_match_record_0005() -> None:
    assert Contract.C0.capabilities == set()
    assert Contract.C1.capabilities == {Capability.STATUS}
    assert Contract.C2.capabilities == {Capability.IDEMPOTENCY_KEY}
    assert Contract.C3.capabilities == {Capability.IDEMPOTENCY_KEY, Capability.CONDITIONAL_WRITE}


def test_required_check_per_attempt_and_contract() -> None:
    for contract in Contract:
        assert required_check(contract, 1) is CheckKind.FULL
    for contract in (Contract.C0, Contract.C1):
        with pytest.raises(GatewayRuleError):
            required_check(contract, 2)
    assert required_check(Contract.C2, 2) is CheckKind.EXACT_ONLY
    assert required_check(Contract.C3, 5) is CheckKind.EXEMPT
    with pytest.raises(ValueError):
        required_check(Contract.C2, 0)


def test_reconcile_how() -> None:
    assert reconcile_how(Contract.C0, escalated=False) is ReconcileHow.ESCALATED_TO_HUMAN
    assert reconcile_how(Contract.C1, escalated=False) is ReconcileHow.QUERYING_STATUS
    assert reconcile_how(Contract.C2, escalated=False) is ReconcileHow.RETRYING_WITH_SAME_KEY
    assert reconcile_how(Contract.C3, escalated=False) is ReconcileHow.RETRYING_WITH_SAME_KEY
    for contract in Contract:
        assert reconcile_how(contract, escalated=True) is ReconcileHow.ESCALATED_TO_HUMAN


def test_settings_defaults_and_backoff() -> None:
    settings = GatewaySettings(approval_wait_ms=600_000)
    assert settings.settle_ms == 10_050
    assert [settings.backoff_ms(n) for n in range(1, 7)] == [
        2_000,
        4_000,
        8_000,
        16_000,
        16_000,
        16_000,
    ]
    with pytest.raises(ValueError):
        settings.backoff_ms(0)


def test_settings_reject_a_timeout_that_cannot_outlast_a_round_trip() -> None:
    with pytest.raises(ValueError, match="dispatch_timeout_ms"):
        GatewaySettings(approval_wait_ms=1, dispatch_timeout_ms=100)
    GatewaySettings(approval_wait_ms=1, dispatch_timeout_ms=101)
    with pytest.raises(ValueError):
        GatewaySettings(approval_wait_ms=0)
    with pytest.raises(ValueError):
        ContractParams(commit_deadline_ms=0)
