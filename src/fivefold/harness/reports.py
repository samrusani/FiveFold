"""The harness's report sink, where every final report lands (0004, 0008).

In G modes the gateway hands each report it accepted (G2) or forwarded (G1) to the sink;
in baselines the agent submits to the sink directly, unchecked. The sink checks the
report schema, records the report with whether it was valid, and never refuses one: the
evaluator counts the last valid report before the run stops (spec, Gateway).

PostgreSQL's `jsonb` cannot hold a NUL character or a number that is not finite. A
report that holds either is recorded as `{"json_text": ...}`, its JSON as text, from
which `json.loads` gives back the report unchanged. Such a report is always invalid,
because the report schema allows neither.
"""

from __future__ import annotations

import json
import math
from collections.abc import Collection
from typing import Literal

from fivefold.domain.report import InvalidReport, parse_report
from fivefold.harness.log import HarnessLog, JsonValue, ReportRecord
from fivefold.harness.scheduler import Scheduler
from fivefold.harness.transport import Envelope, Handler, Reply

TEXT_KEY = "json_text"
"""The one key of a report recorded as JSON text."""


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
        text = json.dumps(report, sort_keys=True)
        kept: JsonValue = json.loads(text)
        errors: tuple[str, ...] = ()
        try:
            parse_report(kept, self._intents)
        except InvalidReport as invalid:
            errors = tuple(invalid.errors)
        if not storable(kept):
            kept = {TEXT_KEY: text}
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


def storable(value: JsonValue) -> bool:
    """Whether `jsonb` can hold `value`: no NUL character, and only finite numbers."""
    if isinstance(value, str):
        return "\x00" not in value
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(storable(key) and storable(item) for key, item in value.items())
    if isinstance(value, list):
        return all(storable(item) for item in value)
    return True


def sink_endpoint(sink: ReportSink) -> Handler:
    """The report sink on the transport, for baselines' `submit_final_report` (0008).

    The sink records every report, valid or not. Like the gateway's tool, it refuses a
    report that breaks the schema, so the agent can fix it and resubmit.
    """

    async def handle(request: Envelope) -> Reply:
        errors = sink.submit(
            request.sender, request.body, accepted_by="sink", message_seq=request.seq
        )
        if errors:
            details = {"message": "; ".join(errors)}
            return Reply({"result": "refused", "reason": "invalid_args", "details": details})
        return Reply({"result": "accepted"})

    return handle
