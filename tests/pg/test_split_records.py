"""The split topology's harness records (0009, db/migrations/0013_split_topology.sql).

Two services write one run's harness records, so they number events from one sequence; a
split run records its clock origin once; and its agent's model adapter is `external`.
"""

from __future__ import annotations

import asyncio

import httpx
import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from fivefold.faults.plan import FaultInjector
from fivefold.harness.log import EVENT_STAMP_LOCK_KEY, MessageRecord, RunRecord, SharedPgHarnessLog
from fivefold.harness.scheduler import Scheduler
from fivefold.harness.split.gateway import SimulatorLink
from fivefold.harness.split.hops import SEQ_HEADER, Hops, logged_message

pytestmark = pytest.mark.pg


def _harness(dsn: str) -> psycopg.Connection[tuple[object, ...]]:
    return psycopg.connect(make_conninfo(dsn, user="ff_harness"), autocommit=True)


def _run(adapter: str) -> RunRecord:
    return RunRecord(
        run_id="dev-001-single-po.G2.C2.7",
        scenario_id="dev-001-single-po",
        scenario_digest="0" * 64,
        config="G2",
        contract="C2",
        seed=7,
        model_adapter=adapter,  # type: ignore[arg-type]
        model="my-agent",
        params={},
        topology="split",
    )


def test_two_writers_number_one_series_of_events(migrated_database: str) -> None:
    with _harness(migrated_database) as first, _harness(migrated_database) as second:
        gateway, simulator = SharedPgHarnessLog(first), SharedPgHarnessLog(second)
        numbers = [gateway.next_seq(), simulator.next_seq(), gateway.next_seq()]
        assert numbers == sorted(set(numbers))
        sent = gateway.next_seq()
        gateway.message_sent(
            MessageRecord(
                seq=sent,
                hop="gw_sim",
                kind="request",
                sender="gateway",
                receiver="simulator",
                api="get_budget",
                correlation=None,
                intent_id=None,
                idempotency_key=None,
                answer_class=None,
                body={"cost_center": "CC-OPS"},
                sent_vt=10,
            )
        )
        delivered = simulator.next_seq()
        simulator.message_fate(sent, "delivered", vt=11, delivered_seq=delivered)
        row = first.execute(
            "SELECT fate, delivered_seq, delivered_vt FROM harness.message_log WHERE seq = %s",
            (sent,),
        ).fetchone()
    assert row == ("delivered", delivered, 11)
    assert delivered > sent


def test_two_connections_share_the_stamp_lock(migrated_database: str) -> None:
    with _harness(migrated_database) as first, _harness(migrated_database) as second:
        one, two = SharedPgHarnessLog(first), SharedPgHarnessLog(second)
        with one.ordering(), second.transaction():
            assert second.execute(
                "SELECT pg_try_advisory_xact_lock(%s::bigint)", (EVENT_STAMP_LOCK_KEY,)
            ).fetchone() == (False,)
        assert one.stamp(lambda: 10)[1] == 10
        assert two.stamp(lambda: 11)[1] == 11


def test_a_split_run_records_its_agent_as_external(migrated_database: str) -> None:
    with _harness(migrated_database) as conn:
        SharedPgHarnessLog(conn).run_recorded(_run("external"))
        row = conn.execute("SELECT topology, model_adapter, model FROM harness.run").fetchone()
    assert row == ("split", "external", "my-agent")


def test_an_unknown_model_adapter_is_refused(migrated_database: str) -> None:
    with _harness(migrated_database) as conn, pytest.raises(psycopg.errors.CheckViolation):
        SharedPgHarnessLog(conn).run_recorded(_run("hosted"))


def test_the_clock_origin_is_one_row_written_once(migrated_database: str) -> None:
    with _harness(migrated_database) as conn:
        conn.execute("INSERT INTO harness.clock_origin (origin_ms) VALUES (1000)")
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute("INSERT INTO harness.clock_origin (origin_ms) VALUES (2000)")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE harness.clock_origin SET origin_ms = 3000")
        row = conn.execute("SELECT origin_ms FROM harness.clock_origin").fetchone()
    assert row == (1000,)


def test_an_exponent_answer_matches_its_postgresql_jsonb_record(migrated_database: str) -> None:
    async def send() -> None:
        with _harness(migrated_database) as conn:
            scheduler = Scheduler()
            hops = Hops(scheduler, SharedPgHarnessLog(conn), FaultInjector())

            def simulator(request: httpx.Request) -> httpx.Response:
                answer = hops.sent(
                    hop="gw_sim",
                    kind="answer",
                    sender="simulator",
                    receiver="gateway",
                    api="get_budget",
                    body={"sample": 1e23},
                    correlation=int(request.headers[SEQ_HEADER]),
                )
                assert logged_message(conn, answer.seq, body_text='{"sample": 9e22}') is None
                return httpx.Response(200, headers={SEQ_HEADER: str(answer.seq)}, json=answer.body)

            async with httpx.AsyncClient(
                base_url="http://simulator", transport=httpx.MockTransport(simulator)
            ) as client:
                link = SimulatorLink(
                    scheduler,
                    hops,
                    client,
                    secret="fixture-secret",
                    read_message=lambda seq, body: logged_message(conn, seq, body_text=body),
                )
                reply = await link.call(
                    hop="gw_sim",
                    sender="gateway",
                    receiver="simulator",
                    api="get_budget",
                    body={"cost_center": "CC-OPS"},
                    timeout_ms=500,
                )
            assert reply.body == {"sample": 1e23}
            assert conn.execute(
                "SELECT fate FROM harness.message_log WHERE kind = 'answer'"
            ).fetchone() == ("delivered",)

    asyncio.run(send())
