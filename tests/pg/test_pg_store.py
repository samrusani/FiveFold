"""The Postgres store across connections: locks, restarts and the run's intents (0003).

The Hypothesis machines and the step tests compare the Postgres store with the in-memory
one through a single connection. These tests cover what only a second connection can
show. A run has one gateway, and the gateway may use further connections (a second
worker of its dispatcher, say), so each lock the store takes gets a test here: two
connections reach the same records at once, and the test shows that one waits for the
other and that the records stay right.

To make an interleaving happen on purpose, a test holds one connection's operation at a
chosen point of `_PgTransaction` (`Pause`) while another connection acts, and watches
`pg_stat_activity` to see a connection wait for a lock.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from fivefold.domain.contracts import Contract, ContractParams
from fivefold.gateway import pg_store
from fivefold.gateway.pg_store import GatewayBusy, IntentMismatch, PgStore
from fivefold.gateway.records import (
    ActionResult,
    Answer,
    ApprovalStatus,
    Attempt,
    BasisCheck,
    Claim,
    ClaimStatus,
    Proposal,
    Refusal,
    RefusalReason,
    ReportAccepted,
)
from fivefold.gateway.state_machine import ActionState, AnswerClass, GatewaySettings, Reason
from fivefold.gateway.store import UnknownAction

pytestmark = pytest.mark.pg

SETTINGS = GatewaySettings(
    approval_wait_ms=5_000,
    params=ContractParams(max_hop_latency_ms=50, commit_deadline_ms=1_000),
    dispatch_timeout_ms=200,
)
INTENTS = {"REQ-1": "submit_purchase_order", "REQ-2": "submit_purchase_order"}
FRESH = BasisCheck(exact_ok=True, sufficient_ok=True)
FAR = 10**9
"""An approval expiry far beyond any test's virtual time."""
WAIT_S = 30
"""How long a test waits for another thread before it fails."""


def _store(
    dsn: str,
    intents: dict[str, str] = INTENTS,
    *,
    exclusive: bool = True,
    settings: GatewaySettings = SETTINGS,
) -> PgStore:
    gateway = make_conninfo(dsn, user="ff_gw")
    return PgStore(
        gateway,
        task_id="TASK-1",
        intents=intents,
        contract=Contract.C2,
        settings=settings,
        exclusive=exclusive,
    )


def _proposal(intent_id: str = "REQ-1", key: str = "k") -> Proposal:
    return Proposal(intent_id, "submit_purchase_order", "supplier/S-1", {"q": 1}, key, {"f": 1})


def _authorized(store: PgStore, intent_id: str = "REQ-1", key: str = "k", vt: int = 0) -> str:
    result = store.propose(_proposal(intent_id, key), vt)
    assert isinstance(result, ActionResult)
    action_id = result.action_id
    seen = store.view(action_id).last_seq
    store.file_approval(action_id, FRESH, f"AP-{action_id}", vt + 1, seen_seq=seen)
    seen = store.view(action_id).last_seq
    store.poll_approval(action_id, ApprovalStatus.APPROVED, FRESH, vt + 2, seen_seq=seen)
    return action_id


def _dispatched(store: PgStore, intent_id: str = "REQ-1", key: str = "k", vt: int = 0) -> str:
    action_id = _authorized(store, intent_id, key, vt)
    ticket = store.begin_check(action_id)
    check = ticket.record(
        approval=ApprovalStatus.APPROVED, basis=FRESH, check_vt=vt + 3, expires_at=FAR
    )
    assert store.dispatch(action_id, check, vt + 3) is not None
    return action_id


class Call:
    """One store operation on its own thread, keeping what it returned or raised."""

    def __init__(self, operation: Callable[[], object]) -> None:
        self.outcome: list[object] = []

        def run() -> None:
            try:
                self.outcome.append(operation())
            except Exception as error:  # the test inspects it
                self.outcome.append(error)

        self.thread = threading.Thread(target=run, daemon=True)

    def start(self) -> Call:
        self.thread.start()
        return self

    def result(self) -> object:
        self.thread.join(WAIT_S)
        assert not self.thread.is_alive(), "the operation never finished"
        return self.outcome[0]


class Pause:
    """Hold one call at a point of `_PgTransaction` until the test releases it.

    The call stops when it reaches `method` for the `nth` time, after that method
    returns. Other threads pass through.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, method: str, *, nth: int = 1) -> None:
        self.call: Call | None = None
        self._reached = threading.Event()
        self._released = threading.Event()
        self._seen = 0
        original = getattr(pg_store._PgTransaction, method)

        def paused(tx: object, *args: object, **kwargs: object) -> object:
            value = original(tx, *args, **kwargs)
            if self.call is not None and threading.current_thread() is self.call.thread:
                self._seen += 1
                if self._seen == nth:
                    self._reached.set()
                    assert self._released.wait(WAIT_S), "the test never released the call"
            return value

        monkeypatch.setattr(pg_store._PgTransaction, method, paused)

    def hold(self, call: Call) -> Call:
        """Start `call` and wait until it reaches the pause."""
        self.call = call
        call.start()
        assert self._reached.wait(WAIT_S), "the call never reached the pause"
        return call

    def release(self) -> None:
        self._released.set()


def _a_gateway_connection_waits(dsn: str) -> bool:
    """Whether some `ff_gw` connection waits for a lock, within `WAIT_S` seconds."""
    with psycopg.connect(dsn, autocommit=True) as monitor:
        deadline = time.monotonic() + WAIT_S
        while time.monotonic() < deadline:
            row = monitor.execute(
                "SELECT count(*) FROM pg_catalog.pg_stat_activity WHERE datname ="
                " current_database() AND usename = 'ff_gw' AND wait_event_type = 'Lock'"
            ).fetchone()
            if row is not None and row != (0,):
                return True
            time.sleep(0.01)
    return False


def _no_gateway_connection_left(dsn: str) -> bool:
    """Whether every `ff_gw` connection has ended, within `WAIT_S` seconds.

    A server process ends shortly after its client closes the connection, so this waits.
    """
    with psycopg.connect(dsn, autocommit=True) as monitor:
        deadline = time.monotonic() + WAIT_S
        while time.monotonic() < deadline:
            row = monitor.execute(
                "SELECT count(*) FROM pg_catalog.pg_stat_activity"
                " WHERE datname = current_database() AND usename = 'ff_gw'"
            ).fetchone()
            if row == (0,):
                return True
            time.sleep(0.01)
    return False


# One gateway per run ------------------------------------------------------------------------


def test_one_gateway_holds_a_run_at_a_time(migrated_database: str) -> None:
    gateway = _store(migrated_database)
    with pytest.raises(GatewayBusy):
        _store(migrated_database)
    with _store(migrated_database, exclusive=False) as worker:
        assert worker.snapshot() == gateway.snapshot()
    gateway.close()
    gateway.close()
    with _store(migrated_database):
        pass


def test_the_run_keeps_its_intents(migrated_database: str) -> None:
    with _store(migrated_database):
        pass
    with pytest.raises(IntentMismatch) as refused:
        _store(migrated_database, {"REQ-1": "reserve_inventory"})
    # The error's traceback keeps the half-made store alive, so garbage collection cannot
    # end its connection: the store must have closed it, and released the run, itself.
    assert refused.value.__traceback__ is not None
    assert _no_gateway_connection_left(migrated_database), "the refused store kept its connection"
    with _store(migrated_database) as again:
        assert again.snapshot().intents == INTENTS
        with pytest.raises(UnknownAction):
            again.view("A-0099")


def test_only_the_gateway_records_the_intents(migrated_database: str) -> None:
    with pytest.raises(IntentMismatch, match="no intents yet"):
        _store(migrated_database, exclusive=False)
    with psycopg.connect(migrated_database) as conn:
        assert conn.execute("SELECT count(*) FROM gw.intent").fetchone() == (0,)


def test_a_restarted_gateway_recovers_from_the_database(migrated_database: str) -> None:
    with _store(migrated_database) as before_crash:
        action_id = _dispatched(before_crash)
    with _store(migrated_database) as after_restart:
        after_restart.restart(1_000)
        view = after_restart.view(action_id)
        assert view.action.state is ActionState.OUTCOME_UNKNOWN
        assert view.events[-1].reason is Reason.RECOVERED_OPEN_ATTEMPT
        assert view.attempts[0].answer_class is None


def test_a_restarted_gateway_keeps_the_runs_clock(migrated_database: str) -> None:
    with _store(migrated_database) as before_crash:
        action_id = _authorized(before_crash, vt=1_000)
        before_crash.status(action_id, 1_500)
    with _store(migrated_database) as after_restart:
        assert after_restart.vt == 1_500
        with pytest.raises(ValueError, match="backwards"):
            after_restart.restart(1_499)
        after_restart.restart(1_500)


# Two connections, one record ------------------------------------------------------------------


def test_a_second_connection_sees_the_first_ones_writes(migrated_database: str) -> None:
    with _store(migrated_database) as first, _store(migrated_database, exclusive=False) as second:
        action_id = _authorized(first)
        assert second.snapshot() == first.snapshot()
        assert second.view(action_id).action.state is ActionState.AUTHORIZED


def test_a_check_overtaken_on_another_connection_commits_nothing(migrated_database: str) -> None:
    with _store(migrated_database) as first, _store(migrated_database, exclusive=False) as second:
        action_id = _authorized(first)
        ticket = first.begin_check(action_id)
        check = ticket.record(
            approval=ApprovalStatus.APPROVED, basis=FRESH, check_vt=3, expires_at=FAR
        )
        assert first.dispatch(action_id, check, 3) is not None
        assert second.dispatch(action_id, check, 3) is None
        assert len(second.snapshot().attempts[action_id]) == 1


def test_a_step_reads_the_action_only_once_it_holds_its_row(
    migrated_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two connections send the same action at once: the second waits, then sends nothing.

    Both checks passed against the same records. The first send has written its attempt
    but not committed when the second begins. The second must wait for the action's row
    before it reads the action, so that it sees the first send and stands down.
    """
    with _store(migrated_database) as gateway, _store(migrated_database, exclusive=False) as worker:
        action_id = _authorized(gateway)
        ticket = gateway.begin_check(action_id)
        check = ticket.record(
            approval=ApprovalStatus.APPROVED, basis=FRESH, check_vt=3, expires_at=FAR
        )
        pause = Pause(monkeypatch, "apply")
        first = pause.hold(Call(lambda: gateway.dispatch(action_id, check, 3)))
        second = Call(lambda: worker.dispatch(action_id, check, 3)).start()
        assert _a_gateway_connection_waits(migrated_database), "the second send did not wait"
        pause.release()
        sent, second_sent = first.result(), second.result()
        assert isinstance(sent, Attempt) and sent.attempt_no == 1
        assert second_sent is None
        assert len(gateway.snapshot().attempts[action_id]) == 1
        assert gateway.view(action_id).action.state is ActionState.DISPATCHED


def test_a_refusal_locks_the_holder_before_the_runs_state(migrated_database: str) -> None:
    """A proposal refused as busy waits for the holder's row before it logs its answer.

    A step holds the holder's row and then takes the run's state row, as every step
    does. If the proposal took the run's state row first, to log its refusal, and then
    waited for the holder (its log row refers to the holder), the two would deadlock.
    """
    with _store(migrated_database) as gateway:
        holder = _dispatched(gateway)
        refusing = Call(lambda: gateway.propose(_proposal("REQ-1", key="other"), 10))
        with psycopg.connect(migrated_database, autocommit=True) as step, step.transaction():
            step.execute("SET LOCAL lock_timeout = '10s'")
            step.execute("SELECT 1 FROM gw.action WHERE action_id = %s FOR UPDATE", (holder,))
            refusing.start()
            assert _a_gateway_connection_waits(migrated_database)
            step.execute("UPDATE gw.run_state SET last_call_no = last_call_no")
        refusal = refusing.result()
    assert isinstance(refusal, Refusal)
    assert (refusal.reason, refusal.action_id) == (RefusalReason.INTENT_BUSY, holder)


def test_a_proposal_answers_with_the_holder_as_it_stands(
    migrated_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The holder cannot change between the matching rule's read and the answer."""
    with _store(migrated_database) as gateway, _store(migrated_database, exclusive=False) as worker:
        holder = _dispatched(gateway)
        pause = Pause(monkeypatch, "holders")
        refusing = pause.hold(Call(lambda: gateway.propose(_proposal("REQ-1", "other"), 300)))
        lost = Answer(AnswerClass.UNKNOWN, Reason.SERVER_ERROR)
        answering = Call(lambda: worker.answer(holder, 1, lost, 300)).start()
        assert _a_gateway_connection_waits(migrated_database), "the answer did not wait"
        pause.release()
        refusal, answered = refusing.result(), answering.result()
        assert isinstance(refusal, Refusal)
        assert (refusal.reason, refusal.state) == (
            RefusalReason.INTENT_BUSY,
            ActionState.DISPATCHED,
        )
        assert answered is None
        assert gateway.view(holder).action.state is ActionState.OUTCOME_UNKNOWN


@pytest.mark.parametrize("point", ["run_closed", "next_action_number"])
def test_closing_the_run_waits_for_a_proposal_in_progress(
    migrated_database: str, monkeypatch: pytest.MonkeyPatch, point: str
) -> None:
    """A proposal that found the run open lands before the close, which then rejects it.

    The proposal holds its intent's row from before it reads whether the run is closed,
    and closing takes every intent's row first, so the close waits for the proposal.
    """
    with _store(migrated_database) as gateway, _store(migrated_database, exclusive=False) as closer:
        pause = Pause(monkeypatch, point)
        proposing = pause.hold(Call(lambda: gateway.propose(_proposal("REQ-2", "late"), 10)))
        closing = Call(lambda: closer.close_run(20)).start()
        assert _a_gateway_connection_waits(migrated_database), "the close did not wait"
        pause.release()
        proposed, closed = proposing.result(), closing.result()
        assert isinstance(proposed, ActionResult)
        assert (proposed.action_id, proposed.state) == ("A-0001", ActionState.PROPOSED)
        assert closed is None
        snapshot = closer.snapshot()
    assert snapshot.run_closed
    assert snapshot.actions["A-0001"].state is ActionState.REJECTED
    assert snapshot.events[-1].reason is Reason.RUN_CLOSED


def test_a_report_that_closes_an_intent_waits_for_a_proposal_in_progress(
    migrated_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A proposal that found its intent open lands before the report that closes it.

    The report locks every intent before it reads the actions, as closing the run does,
    so it sees the new action and rejects it with the intent (0010, item 6).
    """
    with _store(migrated_database) as gateway, _store(migrated_database, exclusive=False) as worker:
        pause = Pause(monkeypatch, "run_closed")
        proposing = pause.hold(Call(lambda: gateway.propose(_proposal("REQ-2", "late"), 10)))
        claims = [Claim("REQ-1", ClaimStatus.NOT_DONE), Claim("REQ-2", ClaimStatus.NOT_DONE)]
        reporting = Call(lambda: worker.submit_report(claims, 20, worker="W1")).start()
        assert _a_gateway_connection_waits(migrated_database), "the report did not wait"
        pause.release()
        proposed, reported = proposing.result(), reporting.result()
        assert isinstance(proposed, ActionResult) and proposed.action_id == "A-0001"
        assert isinstance(reported, ReportAccepted), reported
        snapshot = worker.snapshot()
    assert snapshot.closed_intents == {"REQ-1", "REQ-2"}
    assert snapshot.actions["A-0001"].state is ActionState.REJECTED
    assert snapshot.run_closed, "the run's only worker reported"


def test_a_report_takes_the_intents_before_the_actions(
    migrated_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A report waits behind a proposal that holds its intent, then sees what it did.

    The proposal holds REQ-1's row and is about to lock its holder, A-0001, to attach to
    it. A report that took the actions first would hold A-0001 and wait for REQ-1, and
    the two would deadlock. Taking the intents first, as the lock order says, it waits.
    """
    with _store(migrated_database) as gateway, _store(migrated_database, exclusive=False) as worker:
        first = gateway.propose(_proposal("REQ-1", "k"), 0)
        assert isinstance(first, ActionResult) and first.action_id == "A-0001"
        pause = Pause(monkeypatch, "lock_intent")
        attaching = pause.hold(Call(lambda: gateway.propose(_proposal("REQ-1", "k"), 10)))
        claims = [Claim("REQ-1", ClaimStatus.NOT_DONE), Claim("REQ-2", ClaimStatus.NOT_DONE)]
        reporting = Call(lambda: worker.submit_report(claims, 20, worker="W1")).start()
        assert _a_gateway_connection_waits(migrated_database), "the report did not wait"
        pause.release()
        attached, reported = attaching.result(), reporting.result()
        assert isinstance(attached, ActionResult) and attached.action_id == "A-0001", attached
        assert isinstance(reported, ReportAccepted), reported
        assert worker.view("A-0001").action.state is ActionState.REJECTED


def test_accepted_reports_survive_a_restart(migrated_database: str) -> None:
    two_workers = GatewaySettings(
        approval_wait_ms=5_000,
        params=ContractParams(max_hop_latency_ms=50, commit_deadline_ms=1_000),
        dispatch_timeout_ms=200,
        agent_workers=2,
    )
    claims = [Claim("REQ-1", ClaimStatus.NOT_DONE), Claim("REQ-2", ClaimStatus.NOT_DONE)]
    with _store(migrated_database, settings=two_workers) as before_crash:
        assert isinstance(before_crash.submit_report(claims, 10, worker="W1"), ReportAccepted)
        assert not before_crash.snapshot().run_closed
    with _store(migrated_database, settings=two_workers) as after_restart:
        after_restart.restart(20)
        assert isinstance(after_restart.submit_report(claims, 30, worker="W1"), ReportAccepted)
        assert not after_restart.snapshot().run_closed
        assert isinstance(after_restart.submit_report(claims, 40, worker="W2"), ReportAccepted)
        assert after_restart.snapshot().run_closed


def test_the_invariant_check_reads_one_state(
    migrated_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No other connection commits while a transaction's invariant check reads.

    The check reads every table in turn. The proposal below is an identical re-send,
    which changes nothing, so the check's own lock on the run's state row is all that
    keeps the answer on the other connection from committing between those reads.
    """
    with _store(migrated_database) as gateway, _store(migrated_database, exclusive=False) as worker:
        assert isinstance(gateway.propose(_proposal("REQ-1", "k"), 0), ActionResult)
        other = _dispatched(gateway, "REQ-2", "k2")
        # The second read of the intents comes in the middle of the check's snapshot:
        # after it read the actions, before it reads the log.
        pause = Pause(monkeypatch, "intents", nth=2)
        resending = pause.hold(Call(lambda: gateway.propose(_proposal("REQ-1", "k"), 400)))
        committed = Answer(AnswerClass.COMMITTED, Reason.COMMITTED, "PO-0001")
        answering = Call(lambda: worker.answer(other, 1, committed, 400)).start()
        assert _a_gateway_connection_waits(migrated_database), "the answer did not wait"
        pause.release()
        resent, answered = resending.result(), answering.result()
        assert isinstance(resent, ActionResult), resent
        assert answered is None
        assert gateway.view(other).action.state is ActionState.CONFIRMED
