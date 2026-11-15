"""The harness's report sink, where every final report lands (0004, 0008).

In G modes the gateway hands each report it accepted (G2) or forwarded (G1) to the sink;
in baselines the agent submits to the sink directly, unchecked. The sink checks the
report schema, records the report with whether it was valid, and never refuses one: the
evaluator counts the last valid report before the run stops (spec, Gateway).
"""

from __future__ import annotations

import json
from collections.abc import Collection
from typing import Literal

from fivefold.domain.report import InvalidReport, parse_report
from fivefold.harness.log import HarnessLog, JsonValue, ReportRecord
from fivefold.harness.scheduler import Scheduler


class ReportSink:
    """Records final reports in the harness log."""

    def __init__(self, scheduler: Scheduler, log: HarnessLog, intents: Collection[str]) -> None:
        self._scheduler = scheduler
        self._log = log
        self._intents = frozenset(intents)

    def submit(
        self,
        worker: str,
        report: JsonValue,
        *,
        accepted_by: Literal["gateway", "sink"],
        message_seq: int | None = None,
    ) -> tuple[str, ...]:
        """Record a report; return what breaks its schema, if anything."""
        kept = json.loads(json.dumps(report))
        errors: tuple[str, ...] = ()
        try:
            parse_report(kept, self._intents)
        except InvalidReport as invalid:
            errors = tuple(invalid.errors)
        self._log.report_submitted(
            ReportRecord(
                seq=self._log.next_seq(),
                worker=worker,
                report=kept,
                valid=not errors,
                errors=errors,
                accepted_by=accepted_by,
                vt=self._scheduler.now,
                message_seq=message_seq,
            )
        )
        return errors
