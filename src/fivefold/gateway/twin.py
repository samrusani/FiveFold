"""A store that runs every operation on two stores and compares them (0003, testing plan).

`TwinStore` checks a store against the in-memory store, the oracle. Every operation runs
on both. Both must return equal results, or raise the same kind of error with the same
message, and must hold equal records afterwards, down to the counters that number them.
Testing plan item 3 of record 0003 runs the gateway's Hypothesis machine this way against
the Postgres store. It belongs to the tests, not to a running gateway.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from fivefold.gateway.memory import MemoryStore
from fivefold.gateway.pg_store import PgStore
from fivefold.gateway.records import (
    ActionResult,
    Answer,
    ApprovalStatus,
    Attempt,
    BasisCheck,
    CheckRecord,
    CheckTicket,
    Claim,
    ProbeAnswer,
    Proposal,
    Refusal,
    ReportAccepted,
    Resolution,
    Snapshot,
)
from fivefold.gateway.state_machine import GatewaySettings
from fivefold.gateway.steps import ActionView


class StoreMismatch(AssertionError):
    """The two stores disagreed."""


class TwinStore:
    """The in-memory oracle and a Postgres store, run side by side."""

    def __init__(self, oracle: MemoryStore, pg: PgStore) -> None:
        self.oracle = oracle
        self.pg = pg

    def _both(self, name: str, *args: object, **kwargs: object) -> Any:
        try:
            expected = getattr(self.oracle, name)(*args, **kwargs)
        except Exception as error:
            try:
                getattr(self.pg, name)(*args, **kwargs)
            except Exception as other:
                if type(other) is not type(error) or str(other) != str(error):
                    raise StoreMismatch(
                        f"{name}: memory raised {error!r}, Postgres raised {other!r}"
                    ) from other
            else:
                raise StoreMismatch(f"{name}: memory raised {error!r}, Postgres did not") from error
            self.compare()
            raise
        actual = getattr(self.pg, name)(*args, **kwargs)
        if actual != expected:
            raise StoreMismatch(f"{name}: Postgres gave {actual!r}, memory {expected!r}")
        self.compare()
        return expected

    def compare(self) -> None:
        """Raise `StoreMismatch` unless both stores hold the same records."""
        pg, oracle = self.pg.snapshot(), self.oracle.snapshot()
        parts = ("intents", "actions", "grants", "attempts", "events", "escalations")
        for part in (*parts, "closed_intents", "reports"):
            if getattr(pg, part) != getattr(oracle, part):
                raise StoreMismatch(f"{part} differ")
        if pg.tool_calls != oracle.tool_calls:
            raise StoreMismatch("tool calls differ")
        if pg.run_closed != oracle.run_closed:
            raise StoreMismatch("run_closed differs")
        last_seq = oracle.events[-1].seq if oracle.events else 0
        expected = (len(oracle.actions), last_seq, len(oracle.tool_calls))
        if self.pg.counters() != expected:
            raise StoreMismatch(f"counters {self.pg.counters()} differ from {expected}")

    @property
    def settings(self) -> GatewaySettings:
        return self.oracle.settings

    def snapshot(self) -> Snapshot:
        return self.oracle.snapshot()

    def view(self, action_id: str) -> ActionView:
        expected = self.oracle.view(action_id)
        if self.pg.view(action_id) != expected:
            raise StoreMismatch(f"views of {action_id} differ")
        return expected

    def begin_check(self, action_id: str) -> CheckTicket:
        return cast(CheckTicket, self._both("begin_check", action_id))

    def propose(self, proposal: Proposal, vt: int) -> ActionResult | Refusal:
        return cast(ActionResult | Refusal, self._both("propose", proposal, vt))

    def status(self, action_id: str, vt: int, *, tool: str = "get_action_status") -> ActionResult:
        return cast(ActionResult, self._both("status", action_id, vt, tool=tool))

    def submit_report(
        self, claims: Sequence[Claim], vt: int, *, worker: str
    ) -> ReportAccepted | Refusal:
        outcome = self._both("submit_report", claims, vt, worker=worker)
        return cast(ReportAccepted | Refusal, outcome)

    def file_approval(
        self, action_id: str, basis: BasisCheck, approval_id: str | None, vt: int, *, seen_seq: int
    ) -> None:
        self._both("file_approval", action_id, basis, approval_id, vt, seen_seq=seen_seq)

    def poll_approval(
        self,
        action_id: str,
        approval: ApprovalStatus,
        basis: BasisCheck | None,
        vt: int,
        *,
        seen_seq: int,
    ) -> None:
        self._both("poll_approval", action_id, approval, basis, vt, seen_seq=seen_seq)

    def time_out_approval(
        self, action_id: str, approval: ApprovalStatus, vt: int, *, seen_seq: int
    ) -> None:
        self._both("time_out_approval", action_id, approval, vt, seen_seq=seen_seq)

    def dispatch(self, action_id: str, check: CheckRecord, vt: int) -> Attempt | None:
        return cast(Attempt | None, self._both("dispatch", action_id, check, vt))

    def retry(self, action_id: str, check: CheckRecord, vt: int) -> Attempt | None:
        return cast(Attempt | None, self._both("retry", action_id, check, vt))

    def answer(self, action_id: str, attempt_no: int, answer: Answer, vt: int) -> None:
        self._both("answer", action_id, attempt_no, answer, vt)

    def probe(self, action_id: str, result: ProbeAnswer, vt: int) -> None:
        self._both("probe", action_id, result, vt)

    def attest(
        self, action_id: str, resolution: Resolution, effect_id: str | None, actor: str, vt: int
    ) -> None:
        self._both("attest", action_id, resolution, effect_id, actor, vt)

    def restart(self, vt: int) -> None:
        self._both("restart", vt)

    def close_run(self, vt: int) -> None:
        self._both("close_run", vt)
