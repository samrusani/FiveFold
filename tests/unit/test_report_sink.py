"""The harness's report sink records every report and whether it was valid (0004, 0008)."""

from __future__ import annotations

from fivefold.harness.log import MemoryHarnessLog
from fivefold.harness.reports import ReportSink
from fivefold.harness.scheduler import Scheduler

DONE = {"claims": [{"intent_id": "REQ-1", "status": "done", "effect_ids": ["PO-0001"]}]}


def test_the_sink_records_valid_and_invalid_reports_in_order() -> None:
    log = MemoryHarnessLog()
    sink = ReportSink(Scheduler(), log, ["REQ-1"])
    assert sink.submit("agent-1", DONE, accepted_by="gateway") == ()
    errors = sink.submit("agent-2", {"claims": []}, accepted_by="sink", message_seq=7)
    assert errors == ("REQ-1 has no claim",)
    first, second = log.reports
    assert (first.seq, first.worker, first.valid, first.accepted_by) == (
        1,
        "agent-1",
        True,
        "gateway",
    )
    assert (second.seq, second.valid, second.errors, second.message_seq) == (
        2,
        False,
        ("REQ-1 has no claim",),
        7,
    )
    assert first.vt == 0


def test_the_sink_keeps_its_own_copy_of_the_report() -> None:
    log = MemoryHarnessLog()
    report = {"claims": [{"intent_id": "REQ-1", "status": "unknown"}]}
    ReportSink(Scheduler(), log, ["REQ-1"]).submit("agent-1", report, accepted_by="sink")
    report["claims"].clear()
    assert log.reports[0].report == {"claims": [{"intent_id": "REQ-1", "status": "unknown"}]}
