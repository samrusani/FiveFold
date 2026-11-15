"""Read one run's ground truth from its database (0004, Ground truth the evaluator reads).

The evaluator connects as `ff_eval`, which can read the `sim`, `gw` and `harness` schemas
and write only `eval` (0008). It reads the simulator's and the harness's tables with SQL,
never their code, and leaves the gateway's tables alone: they may only explain a failure,
never decide one. The scenario comes from the caller, since scenario files can live
outside the repository (0009); the run's records must name the same scenario.

Everything is read in one repeatable-read, read-only transaction, so the records are one
consistent snapshot, and every list in a fixed order.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import psycopg
from psycopg.rows import TupleRow

from fivefold.domain.contracts import Contract, ContractParams
from fivefold.domain.rules import Operation
from fivefold.domain.scenario import Scenario
from fivefold.evaluator.records import (
    ActorAction,
    Approval,
    Config,
    Destination,
    Effect,
    FactVersion,
    Fault,
    Message,
    Read,
    RecordsError,
    RunInfo,
    RunRecords,
    SubmittedReport,
)

Connection = psycopg.Connection[TupleRow]


def load_run(conn: Connection, scenario: Scenario) -> RunRecords:
    """Read the run in `conn`'s database, scored against `scenario`.

    `conn` must be in autocommit mode: the loader opens its own transaction. Raises
    `RecordsError` if the database holds no run, or a run of another scenario.
    """
    if not conn.autocommit:
        raise ValueError("load_run opens its own transaction: use an autocommit connection")
    with conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        records = RunRecords(
            run=_run(conn),
            scenario=scenario,
            destination=_destination(conn),
            effects=_effects(conn),
            messages=_messages(conn),
            faults=_faults(conn),
            actor_actions=_actor_actions(conn),
            reports=_reports(conn),
            approvals=_approvals(conn),
            fact_versions=_fact_versions(conn),
            reads=_reads(conn),
        )
    records.check_scenario()
    return records


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


def _integer(value: Any) -> int | None:
    return None if value is None else int(value)


def _object(value: Any, what: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise RecordsError(f"{what} is not a JSON object")
    return value


def _version(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RecordsError(f"{what} is not an integer version: {value!r}")
    return value


def _run(conn: Connection) -> RunInfo:
    rows = conn.execute(
        "SELECT run_id, scenario_id, scenario_digest, config, contract, seed, topology,"
        " model_adapter, model, cassette_digest, parent_run_id, branch_point, params"
        " FROM harness.run"
    ).fetchall()
    if len(rows) != 1:
        raise RecordsError(f"harness.run holds {len(rows)} rows; a run database holds one")
    row = rows[0]
    return RunInfo(
        run_id=str(row[0]),
        scenario_id=str(row[1]),
        scenario_digest=str(row[2]),
        config=Config(row[3]),
        contract=Contract(row[4]),
        seed=int(row[5]),
        topology=str(row[6]),
        model_adapter=str(row[7]),
        model=str(row[8]),
        cassette_digest=_text(row[9]),
        parent_run_id=_text(row[10]),
        branch_point=_integer(row[11]),
        params=row[12],
    )


def _destination(conn: Connection) -> Destination:
    row = conn.execute(
        "SELECT scenario_id, task_id, capabilities, max_hop_latency_ms, commit_deadline_ms,"
        " status_lag_ms, last_vt FROM sim.run"
    ).fetchone()
    if row is None:
        raise RecordsError("sim.run is empty: the simulator was never set up")
    return Destination(
        scenario_id=str(row[0]),
        task_id=str(row[1]),
        capabilities=frozenset(str(name) for name in row[2]),
        params=ContractParams(
            max_hop_latency_ms=int(row[3]),
            commit_deadline_ms=int(row[4]),
            status_lag_ms=int(row[5]),
        ),
        last_vt=int(row[6]),
    )


def _effects(conn: Connection) -> tuple[Effect, ...]:
    rows = conn.execute(
        "SELECT effect_id, intent_id, operation, args, approval_id, basis, idempotency_key,"
        " client_ref, received_seq, commit_seq, committed_vt FROM sim.effect"
        " ORDER BY commit_seq"
    ).fetchall()
    effects = []
    for row in rows:
        basis = _object(row[5], f"the basis of {row[0]}")
        effects.append(
            Effect(
                effect_id=str(row[0]),
                intent_id=str(row[1]),
                operation=Operation(row[2]),
                args=_object(row[3], f"the args of {row[0]}"),
                approval_id=str(row[4]),
                basis={
                    key: _version(version, f"the basis of {row[0]} for {key}")
                    for key, version in basis.items()
                },
                idempotency_key=_text(row[6]),
                client_ref=_text(row[7]),
                received_seq=int(row[8]),
                commit_seq=int(row[9]),
                committed_vt=int(row[10]),
            )
        )
    return tuple(effects)


def _messages(conn: Connection) -> tuple[Message, ...]:
    rows = conn.execute(
        "SELECT seq, hop, kind, sender, receiver, api, correlation, intent_id,"
        " idempotency_key, answer_class, body, sent_vt, fate, drop_reason, delivered_seq,"
        " delivered_vt FROM harness.message_log ORDER BY seq"
    ).fetchall()
    return tuple(
        Message(
            seq=int(row[0]),
            hop=str(row[1]),
            kind=str(row[2]),
            sender=str(row[3]),
            receiver=str(row[4]),
            api=_text(row[5]),
            correlation=_integer(row[6]),
            intent_id=_text(row[7]),
            idempotency_key=_text(row[8]),
            answer_class=_text(row[9]),
            body=row[10],
            sent_vt=int(row[11]),
            fate=_text(row[12]),
            drop_reason=_text(row[13]),
            delivered_seq=_integer(row[14]),
            delivered_vt=_integer(row[15]),
        )
        for row in rows
    )


def _faults(conn: Connection) -> tuple[Fault, ...]:
    rows = conn.execute(
        "SELECT seq, fault, variant, point, occurrence, action, target_seq, component, voided,"
        " params, vt FROM harness.fault_log ORDER BY seq"
    ).fetchall()
    return tuple(
        Fault(
            seq=int(row[0]),
            fault=str(row[1]),
            variant=_text(row[2]),
            point=str(row[3]),
            occurrence=int(row[4]),
            action=str(row[5]),
            target_seq=_integer(row[6]),
            component=_text(row[7]),
            voided=tuple(int(seq) for seq in row[8]),
            params=_object(row[9], f"the params of fault {row[0]}"),
            vt=int(row[10]),
        )
        for row in rows
    )


def _actor_actions(conn: Connection) -> tuple[ActorAction, ...]:
    rows = conn.execute(
        "SELECT seq, actor, action, subject, details, vt FROM harness.actor_log ORDER BY seq"
    ).fetchall()
    return tuple(
        ActorAction(
            seq=int(row[0]),
            actor=str(row[1]),
            action=str(row[2]),
            subject=str(row[3]),
            details=_object(row[4], f"the details of actor action {row[0]}"),
            vt=int(row[5]),
        )
        for row in rows
    )


def _approvals(conn: Connection) -> tuple[Approval, ...]:
    rows = conn.execute(
        "SELECT approval_id, intent_id, operation, scope, requested_seq, requested_vt, status,"
        " decided_seq, decided_vt, decided_by, expires_at, revoked_seq, revoked_vt, revoked_by"
        " FROM sim.approval ORDER BY requested_seq"
    ).fetchall()
    return tuple(
        Approval(
            approval_id=str(row[0]),
            intent_id=str(row[1]),
            operation=Operation(row[2]),
            scope=_object(row[3], f"the scope of {row[0]}"),
            requested_seq=int(row[4]),
            requested_vt=int(row[5]),
            status=str(row[6]),
            decided_seq=_integer(row[7]),
            decided_vt=_integer(row[8]),
            decided_by=_text(row[9]),
            expires_at=_integer(row[10]),
            revoked_seq=_integer(row[11]),
            revoked_vt=_integer(row[12]),
            revoked_by=_text(row[13]),
        )
        for row in rows
    )


def _fact_versions(conn: Connection) -> tuple[FactVersion, ...]:
    rows = conn.execute(
        "SELECT key, version, value, valid_from_seq, valid_from_vt, cause, cause_effect_id"
        " FROM sim.fact_version ORDER BY key, version"
    ).fetchall()
    return tuple(
        FactVersion(
            key=str(row[0]),
            version=int(row[1]),
            value=_object(row[2], f"version {row[1]} of {row[0]}"),
            valid_from_seq=int(row[3]),
            valid_from_vt=int(row[4]),
            cause=str(row[5]),
            cause_effect_id=_text(row[6]),
        )
        for row in rows
    )


def _reads(conn: Connection) -> tuple[Read, ...]:
    rows = conn.execute("SELECT seq, key, version, caller, vt FROM sim.read_log ORDER BY seq")
    return tuple(
        Read(
            seq=int(row[0]),
            key=str(row[1]),
            version=int(row[2]),
            caller=str(row[3]),
            vt=int(row[4]),
        )
        for row in rows.fetchall()
    )


def _reports(conn: Connection) -> tuple[SubmittedReport, ...]:
    rows = conn.execute(
        "SELECT seq, worker, report, valid, errors, accepted_by, message_seq, vt"
        " FROM harness.final_report ORDER BY seq"
    ).fetchall()
    return tuple(
        SubmittedReport(
            seq=int(row[0]),
            worker=str(row[1]),
            report=row[2],
            valid=bool(row[3]),
            errors=tuple(str(error) for error in row[4]) if isinstance(row[4], list) else (),
            accepted_by=str(row[5]),
            message_seq=_integer(row[6]),
            vt=int(row[7]),
        )
        for row in rows
    )
