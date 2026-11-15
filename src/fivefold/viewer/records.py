"""What the viewer reads from one run database: the run, its scorecard, its timeline, its
ledger and fact history (0012). Every query reads only `sim`, `gw`, `harness` or `eval`.

The viewer shows the evaluator's verdicts as the evaluator wrote them in `eval` (0004); it
judges nothing itself. Wall-clock columns are left out, except where a person needs them
to tell runs apart, and none of them orders anything (0009).
"""

from __future__ import annotations

from typing import Any, LiteralString

from fivefold.viewer.source import Connection, ViewerError, jsonable
from fivefold.viewer.timeline import Event, Kind

MESSAGES: LiteralString = (
    "SELECT seq, hop, kind, sender, receiver, api, correlation, intent_id, idempotency_key,"
    " answer_class, body, sent_vt, fate, drop_reason, delivered_seq, delivered_vt, copy_of"
    " FROM harness.message_log ORDER BY seq"
)
FAULTS: LiteralString = (
    "SELECT seq, fault, variant, point, occurrence, action, target_seq, component, voided,"
    " params, vt FROM harness.fault_log ORDER BY seq"
)
ACTORS: LiteralString = (
    "SELECT seq, actor, action, subject, details, vt FROM harness.actor_log ORDER BY seq"
)
REPORTS: LiteralString = (
    "SELECT seq, worker, report, valid, errors, accepted_by, message_seq, vt"
    " FROM harness.final_report ORDER BY seq"
)
ACTION_EVENTS: LiteralString = (
    "SELECT seq, action_id, kind, from_state, to_state, reason, details, vt"
    " FROM gw.action_event ORDER BY seq"
)


def run(conn: Connection) -> dict[str, Any]:
    """The run (`harness.run`) and when it stopped (`harness.run_stop`), if it has."""
    row = conn.execute(
        "SELECT run_id, scenario_id, scenario_digest, config, contract, seed, topology,"
        " model_adapter, model, cassette_digest, parent_run_id, branch_point, params"
        " FROM harness.run"
    ).fetchone()
    if row is None:
        raise ViewerError(404, "not_a_run", "the database holds no run yet")
    stop = conn.execute("SELECT stopped_vt FROM harness.run_stop").fetchone()
    return {**jsonable(row), "stopped_vt": None if stop is None else stop["stopped_vt"]}


def scorecard(conn: Connection, run_id: str) -> dict[str, Any] | None:
    """The evaluator's result for the run: the verdict, each property's verdict with its
    reason codes and failures, the excused intents and how each claim was judged (0004).
    None when the run has not been evaluated."""
    result = conn.execute(
        "SELECT verdict, evaluated, metrics, result FROM eval.run_result WHERE run_id = %s",
        (run_id,),
    ).fetchone()
    if result is None:
        return None
    properties = conn.execute(
        "SELECT property, name, verdict, reason_codes, failures, metrics FROM eval.result"
        " WHERE run_id = %s ORDER BY property",
        (run_id,),
    ).fetchall()
    whole = result["result"] if isinstance(result["result"], dict) else {}
    return {
        "verdict": result["verdict"],
        "evaluated": result["evaluated"],
        "properties": jsonable(properties),
        "excused_intents": jsonable(whole.get("excused_intents", [])),
        "report": jsonable(whole.get("report")),
        "metrics": jsonable(result["metrics"]),
        "code_version": whole.get("code_version"),
    }


def timeline_events(conn: Connection) -> list[Event]:
    """Every harness event and gateway action event of the run, unordered (`merge` orders
    them). A message is one event, at its send."""
    events: list[Event] = []
    for row in conn.execute(MESSAGES).fetchall():
        events.append(Event("harness", row["seq"], row["sent_vt"], "message", jsonable(row)))
    logs: tuple[tuple[LiteralString, Kind], ...] = (
        (FAULTS, "fault"),
        (ACTORS, "actor"),
        (REPORTS, "report"),
    )
    for query, kind in logs:
        for row in conn.execute(query).fetchall():
            events.append(Event("harness", row["seq"], row["vt"], kind, jsonable(row)))
    for row in conn.execute(ACTION_EVENTS).fetchall():
        events.append(Event("gateway", row["seq"], row["vt"], "action_event", jsonable(row)))
    return events


def ledger(conn: Connection) -> dict[str, Any]:
    """The effects ledger, in commit order, and every version of every fact (0004)."""
    effects = conn.execute(
        "SELECT effect_id, intent_id, operation, args, approval_id, basis, idempotency_key,"
        " client_ref, received_seq, commit_seq, committed_vt FROM sim.effect"
        " ORDER BY commit_seq"
    ).fetchall()
    facts = conn.execute(
        "SELECT key, version, value, valid_from_seq, valid_from_vt, cause, cause_effect_id,"
        " cause_actor FROM sim.fact_version ORDER BY valid_from_seq, key, version"
    ).fetchall()
    return {"effects": jsonable(effects), "facts": jsonable(facts)}
