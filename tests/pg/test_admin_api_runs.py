"""The admin API over HTTP in whole runs of dev-001 at C0 in G2 (0003, 0010).

At C0 the gateway escalates a lost order at once (0005). Two runs, from the cells' seed:

- demo 3, F2: the order commits and its answer is lost;
- F12 on `gw_sim`: the order's request is lost, so nothing commits.

In both, the gateway sends the order at 4,788 and escalates it at 6,788, and the agent
then waits on the action, from about 7,800 to 67,800. A scripted operator acts twice
while it waits. At 10,000 it attests "not committed", which the gateway refuses: S after
the send is 14,838. At 20,000 it lists the open escalations and attests the truth: PO-0001
committed after F2, nothing committed after F12. In one run the operator works over HTTP,
through httpx's ASGI transport into `admin_app`; in the other it calls `GatewayAdmin` in
process, at the same virtual times. Every record of the two runs must be equal: the
gateway's tables, the harness's logs, the ledger and the evaluation.

The scenario has no scripted resolver, and the operator writes nothing to the harness's
actor log, so the evaluator counts no attestation in either run (0004 reads attestations
from the scripted resolver's rows there).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, LiteralString

import httpx
import psycopg
import pytest

from fivefold.domain.contracts import Contract
from fivefold.domain.scenario import load_scenario_file
from fivefold.gateway.admin import BeforeSettleTime, GatewayAdmin
from fivefold.gateway.admin_api import admin_app
from fivefold.gateway.records import Resolution
from fivefold.harness.cells import Cell, cell_plan
from fivefold.harness.demo import DEMO_BY_KEY, DEV_001
from fivefold.harness.run import InprocRun, RunSpec
from fivefold.recorder.playback import WALL_CLOCK_SUFFIXES, records

pytestmark = pytest.mark.pg

SEED = 7
SENT = 4_788
"""When the gateway sends the order, in both runs."""
OPENED = 6_788
"""When it escalates the order: at once, when no answer came within T (2,000)."""
SETTLE_MS = 10_050
"""dev-001's settle time S: `max_hop_latency` 50 plus `commit_deadline` 10,000."""
EARLY_VT = 10_000
"""Before S past the send: "not committed" is refused."""
RESOLVE_VT = 20_000
"""After S past the send, while the agent waits on the action."""

ACTOR = "ops-1"
TOKEN = "run-admin-token"
RESOLVE = "/escalations/ESC-0001/resolve"
NOT_COMMITTED = {"resolution": "not_committed", "actor": ACTOR}
READS = ("/escalations", "/actions", "/actions/A-0001", "/grants", "/events")


def demo_3() -> RunSpec:
    """Demo 3 in G2: F2 at C0, with no resolver."""
    return DEMO_BY_KEY["3"].spec("G2", SEED)


def f12() -> RunSpec:
    """F12 on the gateway's hop to the simulator, at C0 in G2."""
    cell = Cell(
        fault="F12", variant="gw_sim", contract=Contract.C0, config="G2", verdict="", failing=()
    )
    return RunSpec(load_scenario_file(DEV_001), "G2", Contract.C0, SEED, plan=cell_plan(cell))


@dataclass
class Operator:
    """A human at the gateway's admin path, over HTTP or in process.

    It acts at `EARLY_VT` and `RESOLVE_VT`, and keeps what each request answered.
    """

    run: InprocRun
    over_http: bool
    resolution: Resolution
    effect_id: str | None
    answers: dict[str, tuple[int, Any]] = field(default_factory=dict)

    def plan(self) -> None:
        scheduler = self.run.scheduler
        scheduler.call_at(EARLY_VT, lambda: self._act(early=True), label="operator, early")
        scheduler.call_at(RESOLVE_VT, lambda: self._act(early=False), label="operator")

    def _act(self, *, early: bool) -> None:
        run = self.run
        admin = GatewayAdmin(run.scheduler, run.store, run.dispatcher)
        if self.over_http:
            requests = self._over_http(admin, early=early)
            run.scheduler.spawn("operator", requests, name="operator's requests")
        elif early:
            try:
                admin.resolve("ESC-0001", Resolution.NOT_COMMITTED, actor=ACTOR)
            except BeforeSettleTime as refused:
                self.answers["early"] = (409, refused.not_committed_from_vt)
        else:
            (escalation,) = admin.open_escalations()
            result = admin.resolve(
                escalation.escalation_id, self.resolution, self.effect_id, actor=ACTOR
            )
            self.answers["resolve"] = (200, str(result.state))

    async def _over_http(self, admin: GatewayAdmin, *, early: bool) -> None:
        app = admin_app(admin, self.run.store, token=TOKEN)
        transport = httpx.ASGITransport(app=app)
        headers = {"Authorization": f"Bearer {TOKEN}"}
        async with httpx.AsyncClient(
            transport=transport, base_url="http://gateway", headers=headers
        ) as client:
            if early:
                response = await client.post(RESOLVE, json=NOT_COMMITTED)
                self.answers["early"] = (response.status_code, response.json())
                return
            for path in READS:
                response = await client.get(path)
                self.answers[path] = (response.status_code, response.json())
            body = {"resolution": str(self.resolution), "actor": ACTOR}
            if self.effect_id is not None:
                body["effect_id"] = self.effect_id
            response = await client.post(RESOLVE, json=body)
            self.answers["resolve"] = (response.status_code, response.json())


def run_with_operator(
    dsn: str, spec: RunSpec, *, over_http: bool, resolution: Resolution, effect_id: str | None
) -> Operator:
    run = InprocRun(spec, dsn)
    operator = Operator(run, over_http, resolution, effect_id)
    operator.plan()
    asyncio.run(run.execute())
    return operator


GATEWAY_TABLES: tuple[tuple[str, LiteralString], ...] = (
    ("gw.run_state", "SELECT to_jsonb(t) FROM gw.run_state t"),
    ("gw.intent", "SELECT to_jsonb(t) FROM gw.intent t ORDER BY intent_id"),
    ("gw.read", "SELECT to_jsonb(t) FROM gw.read t ORDER BY read_id"),
    ("gw.proposal", "SELECT to_jsonb(t) FROM gw.proposal t ORDER BY proposal_id"),
    ("gw.action", "SELECT to_jsonb(t) FROM gw.action t ORDER BY action_id"),
    ("gw.grant", 'SELECT to_jsonb(t) FROM gw."grant" t ORDER BY grant_id'),
    ("gw.attempt", "SELECT to_jsonb(t) FROM gw.attempt t ORDER BY attempt_id"),
    ("gw.escalation", "SELECT to_jsonb(t) FROM gw.escalation t ORDER BY escalation_id"),
    ("gw.tool_call", "SELECT to_jsonb(t) FROM gw.tool_call t ORDER BY call_no"),
    ("gw.report", "SELECT to_jsonb(t) FROM gw.report t ORDER BY worker"),
)
"""The gateway's tables beyond its action events, which `records` already reads."""


def every_record(dsn: str) -> dict[str, list[Any]]:
    """The run's records, wall-clock columns aside: what playback compares, and the rest
    of the gateway's tables."""
    found: dict[str, list[Any]] = dict(records(dsn))
    with psycopg.connect(dsn) as conn:
        for name, query in GATEWAY_TABLES:
            found[name] = [
                {k: v for k, v in row[0].items() if not k.endswith(WALL_CLOCK_SUFFIXES)}
                for row in conn.execute(query)
            ]
    return found


def rows(dsn: str, query: LiteralString) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn) as conn:
        return conn.execute(query).fetchall()


@pytest.mark.parametrize(
    ("spec", "resolution", "effect_id", "state"),
    [
        (demo_3, Resolution.COMMITTED, "PO-0001", "confirmed"),
        (f12, Resolution.NOT_COMMITTED, None, "failed"),
    ],
    ids=["demo 3, committed", "F12, not committed"],
)
def test_an_http_resolution_leaves_the_records_a_call_in_process_leaves(
    run_database: Callable[[str], str],
    spec: Callable[[], RunSpec],
    resolution: Resolution,
    effect_id: str | None,
    state: str,
) -> None:
    name = "f2" if resolution is Resolution.COMMITTED else "f12"
    http_dsn, direct_dsn = run_database(f"adm_{name}_http"), run_database(f"adm_{name}_direct")
    http = run_with_operator(
        http_dsn, spec(), over_http=True, resolution=resolution, effect_id=effect_id
    )
    direct = run_with_operator(
        direct_dsn, spec(), over_http=False, resolution=resolution, effect_id=effect_id
    )

    # What the operator was told.
    status, early = http.answers["early"]
    assert status == 409
    assert (early["reason"], early["not_committed_from_vt"]) == (
        "before_settle_time",
        SENT + SETTLE_MS,
    )
    assert direct.answers["early"] == (409, SENT + SETTLE_MS)
    assert http.answers["/escalations"] == (
        200,
        {
            "escalations": [
                {
                    "escalation_id": "ESC-0001",
                    "action_id": "A-0001",
                    "intent_id": "REQ-1",
                    "operation": "submit_purchase_order",
                    "target": "supplier/S-1",
                    "args": {
                        "supplier_id": "S-1",
                        "sku": "W-100",
                        "quantity": 100,
                        "unit_price_minor": 540,
                        "currency": "USD",
                        "cost_center": "CC-OPS",
                    },
                    "approval_id": "APR-0001",
                    "opened_vt": OPENED,
                    "last_sent_vt": SENT,
                    "not_committed_from_vt": SENT + SETTLE_MS,
                }
            ]
        },
    )
    status, resolved = http.answers["resolve"]
    assert (status, resolved["state"], resolved["reason"]) == (200, state, "human_attested")
    assert resolved["effect_id"] == effect_id
    assert direct.answers["resolve"] == (200, state)

    # What the reads served is what the database holds, before the attestation.
    events = rows(
        http_dsn,
        "SELECT seq, action_id, kind, from_state, to_state, reason, details, vt"
        " FROM gw.action_event WHERE vt < 20000 ORDER BY seq",
    )
    keys = ("seq", "action_id", "kind", "from_state", "to_state", "reason", "details", "vt")
    served = [dict(zip(keys, row, strict=True)) for row in events]
    assert http.answers["/events"] == (200, {"events": served})
    assert http.answers["/actions/A-0001"][1]["events"] == served
    (listed,) = http.answers["/actions"][1]["actions"]
    assert (listed["state"], listed["approval_id"], listed["effect_id"]) == (
        "outcome_unknown",
        "APR-0001",
        None,
    )
    (grant,) = http.answers["/grants"][1]["grants"]
    assert (grant["grant_id"], grant["state"], grant["consumed_vt"]) == ("G-0001", "consumed", SENT)

    # The attestation: T11 or T12 with reason human_attested, at 20,000, by the operator.
    assert rows(http_dsn, "SELECT * FROM gw.escalation") == [
        ("ESC-0001", "A-0001", OPENED, RESOLVE_VT, str(resolution), effect_id, ACTOR)
    ]
    assert rows(
        http_dsn,
        "SELECT to_state, reason, vt FROM gw.action_event WHERE action_id = 'A-0001'"
        " AND kind = 'transition' ORDER BY seq DESC LIMIT 1",
    ) == [(state, "human_attested", RESOLVE_VT)]
    told = rows(
        http_dsn,
        "SELECT tool, result->>'state', vt FROM gw.tool_call WHERE action_id = 'A-0001'"
        " ORDER BY call_no LIMIT 2",
    )
    assert told[1] == ("get_action_status", state, RESOLVE_VT), "the wait ended at once"

    # Every record of the two runs is the same.
    assert every_record(http_dsn) == every_record(direct_dsn)
