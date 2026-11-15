"""The viewer's read-only API against run databases (0012).

Demo 1 runs in B0 and G2 on seed 7, each in its own run database, which the run registers
in the tests' registry. The API lists both runs from the registry, and serves each run's
scorecard, timeline, ledger and fact history: B0 fails properties 1, 2 and 3 with a second
order, G2 passes with one (0006, Demos). The timeline follows record 0012's order and
marks events that share a virtual time. The API only reads: every endpoint is a GET, and
every connection is `ff_viewer` in a read-only transaction.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from starlette.testclient import TestClient

from fivefold.harness.demo import demo_1_spec
from fivefold.harness.migrations import drop_database, recreate_database, registry_database
from fivefold.harness.registry import RegistryEntry, database_of, forget, register
from fivefold.harness.run import Config, InprocRun
from fivefold.harness.study import MAX_SEED
from fivefold.recorder.playback import records
from fivefold.viewer.app import create_app
from fivefold.viewer.source import RunSource
from fivefold.viewer.timeline import SOURCE_ORDER

pytestmark = pytest.mark.pg

RUN_IDS = {"B0": "dev-001-single-po.B0.C2.7", "G2": "dev-001-single-po.G2.C2.7"}


@pytest.fixture(scope="module")
def demo_1(admin_dsn: str, sim_template: str, db_prefix: str) -> Iterator[dict[Config, str]]:
    """Demo 1 in B0 and G2, seed 7: each configuration's run database."""
    made: dict[Config, str] = {}
    try:
        for config in ("B0", "G2"):
            name = f"{db_prefix}_viewer_demo_1_{config.lower()}"
            drop_database(admin_dsn, name)
            with psycopg.connect(admin_dsn, autocommit=True) as conn:
                conn.execute(
                    sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                        sql.Identifier(name), sql.Identifier(sim_template)
                    )
                )
            made[config] = name
            dsn = make_conninfo(admin_dsn, dbname=name)
            asyncio.run(InprocRun(demo_1_spec(config, 7), dsn).execute())
        yield made
    finally:
        for name in made.values():
            drop_database(admin_dsn, name)
            forget(admin_dsn, name)


@pytest.fixture
def client(admin_dsn: str) -> TestClient:
    """The viewer's API over the tests' registry, with no frontend."""
    return TestClient(create_app(RunSource(admin_dsn, registry_database()), static=None))


def get(client: TestClient, path: str) -> Any:
    response = client.get(path)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/json"
    return response.json()


def test_the_run_list_lists_both_runs_of_the_demo_from_the_registry(
    client: TestClient, demo_1: dict[Config, str]
) -> None:
    listed = get(client, "/api/runs")
    assert listed["registry"] == registry_database()
    runs = {run["database"]: run for run in listed["runs"]}
    for config, database in demo_1.items():
        run = runs[database]
        assert (run["run_id"], run["scenario_id"], run["config"]) == (
            RUN_IDS[config],
            "dev-001-single-po",
            config,
        )
        assert (run["contract"], run["seed"]) == ("C2", "7")
        assert run["created_wall_at"].startswith("20"), "wall-clock time, as ISO 8601 text"
        assert (run["parent_run_id"], run["branch_point"]) == (None, None), "no branch"


def test_a_seed_goes_as_its_exact_decimal_text_even_the_largest_a_study_takes(
    client: TestClient, run_database: Callable[[str], str]
) -> None:
    """A seed is a bigint, and a study takes seeds up to 2**63 - 1. A browser reads a JSON
    number as a double, exact only up to 2**53 - 1, so every seed goes as text: in the run
    list, and in a run's registry row and `harness.run`."""
    dsn = run_database("viewer_max_seed")
    asyncio.run(InprocRun(demo_1_spec("G2", MAX_SEED), dsn).execute())
    database = database_of(dsn)
    listed = {run["database"]: run for run in get(client, "/api/runs")["runs"]}
    assert listed[database]["seed"] == "9223372036854775807"
    summary = get(client, f"/api/runs/{database}")
    assert summary["registry"]["seed"] == summary["run"]["seed"] == "9223372036854775807"


def test_b0_s_scorecard_fails_properties_1_2_and_3_with_their_reason_codes(
    client: TestClient, demo_1: dict[Config, str]
) -> None:
    summary = get(client, f"/api/runs/{demo_1['B0']}")
    assert summary["run"]["run_id"] == RUN_IDS["B0"]
    assert summary["run"]["stopped_vt"] > 0
    card = summary["scorecard"]
    assert card["verdict"] == "fail"
    assert card["evaluated"] == [1, 2, 3, 4, 5]
    verdicts = {
        prop["property"]: (prop["verdict"], prop["reason_codes"]) for prop in card["properties"]
    }
    assert verdicts == {
        1: ("fail", ["duplicate_effects"]),
        2: ("fail", ["approval_used"]),
        3: ("fail", ["stale_input"]),
        4: ("pass", []),
        5: ("pass", []),
    }
    (duplicate,) = card["properties"][0]["failures"]
    assert duplicate["effect_ids"] == ["PO-0001", "PO-0002"]
    assert card["excused_intents"] == []
    (claim,) = card["report"]["claims"]
    assert (claim["intent_id"], claim["status"], claim["claimed_effects"]) == (
        "REQ-1",
        "done",
        ["PO-0002"],
    )


def test_g2_s_scorecard_passes_all_five(client: TestClient, demo_1: dict[Config, str]) -> None:
    card = get(client, f"/api/runs/{demo_1['G2']}")["scorecard"]
    assert card["verdict"] == "pass"
    assert [(p["name"], p["verdict"]) for p in card["properties"]] == [
        ("exactly_once", "pass"),
        ("authorized_at_commit", "pass"),
        ("inputs_still_fresh", "pass"),
        ("actually_committed", "pass"),
        ("truthfully_reported", "pass"),
    ]
    (claim,) = card["report"]["claims"]
    assert (claim["status"], claim["claimed_effects"], claim["passed"]) == (
        "done",
        ["PO-0001"],
        True,
    )


def counts(admin_dsn: str, database: str) -> Counter[str]:
    """How many events each log of the run holds."""
    tables = {
        "message": "harness.message_log",
        "fault": "harness.fault_log",
        "actor": "harness.actor_log",
        "report": "harness.final_report",
        "action_event": "gw.action_event",
    }
    found: Counter[str] = Counter()
    with psycopg.connect(make_conninfo(admin_dsn, dbname=database)) as conn:
        for kind, table in tables.items():
            row = conn.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.SQL(table))).fetchone()
            found[kind] = 0 if row is None else int(row[0])
    return +found


@pytest.mark.parametrize("config", ["B0", "G2"])
def test_the_timeline_holds_every_event_in_record_0012_s_order(
    client: TestClient, demo_1: dict[Config, str], admin_dsn: str, config: Config
) -> None:
    events = get(client, f"/api/runs/{demo_1[config]}/timeline")["events"]
    assert Counter(event["kind"] for event in events) == counts(admin_dsn, demo_1[config])
    keys = [(e["vt"], SOURCE_ORDER[e["source"]], e["seq"]) for e in events]
    assert keys == sorted(keys), "by virtual time; harness first; then each source's seq"
    per_vt = Counter(event["vt"] for event in events)
    assert [e["shares_vt"] for e in events] == [per_vt[e["vt"]] > 1 for e in events]
    for event in events:
        record = event[event["kind"]]
        assert record["seq"] == event["seq"]
        assert event["vt"] == (record["sent_vt"] if event["kind"] == "message" else record["vt"])
    faults = [(e["fault"]["fault"], e["fault"]["point"]) for e in events if e["kind"] == "fault"]
    hop = "gw_sim" if config == "G2" else "agent_sim"
    assert faults == [("F11", "agent.after_tool_call"), ("F2", f"net.{hop}.answer")]
    (report,) = [e["report"] for e in events if e["kind"] == "report"]
    assert report["valid"] is True


def test_g2_s_timeline_marks_gateway_events_that_share_a_virtual_time_with_the_harness(
    client: TestClient, demo_1: dict[Config, str]
) -> None:
    events = get(client, f"/api/runs/{demo_1['G2']}/timeline")["events"]
    sources_at: dict[int, set[str]] = {}
    for event in events:
        sources_at.setdefault(event["vt"], set()).add(event["source"])
    shared = [vt for vt, sources in sources_at.items() if sources == {"harness", "gateway"}]
    assert shared, "the demo has virtual times where both sources have events"
    for vt in shared:
        at = [e for e in events if e["vt"] == vt]
        assert all(e["shares_vt"] for e in at)
        ranks = [SOURCE_ORDER[e["source"]] for e in at]
        assert ranks == sorted(ranks), "harness events first"
    states = [e["action_event"]["to_state"] for e in events if e["kind"] == "action_event"]
    assert states[-1] == "confirmed"


def test_the_ledger_shows_b0_s_second_order_and_g2_s_one(
    client: TestClient, demo_1: dict[Config, str]
) -> None:
    b0 = get(client, f"/api/runs/{demo_1['B0']}/ledger")
    assert [(e["effect_id"], e["approval_id"]) for e in b0["effects"]] == [
        ("PO-0001", "APR-0001"),
        ("PO-0002", "APR-0001"),
    ]
    budget = [f for f in b0["facts"] if f["key"] == "budget/CC-OPS"]
    assert [(f["version"], f["value"]["committed_minor"], f["cause"]) for f in budget] == [
        (1, 20_000, "initial"),
        (2, 74_000, "effect"),
        (3, 128_000, "effect"),
    ]
    assert budget[2]["cause_effect_id"] == "PO-0002"
    g2 = get(client, f"/api/runs/{demo_1['G2']}/ledger")
    assert [e["effect_id"] for e in g2["effects"]] == ["PO-0001"]
    assert g2["effects"][0]["committed_vt"] > 0


def test_every_endpoint_is_a_get_and_reading_changes_no_record(
    client: TestClient, demo_1: dict[Config, str], admin_dsn: str
) -> None:
    database = demo_1["G2"]
    before = records(make_conninfo(admin_dsn, dbname=database))
    paths = ["/api/runs", *(f"/api/runs/{database}{end}" for end in ("", "/timeline", "/ledger"))]
    for path in paths:
        get(client, path)
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            assert client.request(method, path).status_code == 405, (method, path)
    assert records(make_conninfo(admin_dsn, dbname=database)) == before


def test_the_viewer_reads_as_ff_viewer_in_read_only_transactions(
    admin_dsn: str, demo_1: dict[Config, str]
) -> None:
    source = RunSource(admin_dsn, registry_database())
    with source.run(demo_1["B0"]) as conn:
        assert conn.execute("SELECT current_user AS role").fetchone() == {"role": "ff_viewer"}
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute("INSERT INTO eval.result SELECT * FROM eval.result")


def test_an_unknown_run_is_404(client: TestClient) -> None:
    for database in ("fivefold_no_such_run", "Not-A-Name", "x" * 70):
        response = client.get(f"/api/runs/{database}/timeline")
        assert response.status_code == 404
        assert response.json()["error"] == "unknown_run"


def test_a_registered_database_that_was_dropped_or_holds_no_run_is_404(
    client: TestClient, admin_dsn: str, db_prefix: str
) -> None:
    dropped, empty = f"{db_prefix}_viewer_dropped", f"{db_prefix}_viewer_empty"
    drop_database(admin_dsn, dropped)
    recreate_database(admin_dsn, empty)
    try:
        for database in (dropped, empty):
            register(admin_dsn, RegistryEntry(database, "r", "dev-001-single-po", "G2", "C2", 7))
        assert client.get(f"/api/runs/{dropped}").json()["error"] == "run_unavailable"
        response = client.get(f"/api/runs/{empty}/ledger")
        assert (response.status_code, response.json()["error"]) == (404, "not_a_run")
        listed = {run["database"]: run for run in get(client, "/api/runs")["runs"]}
        for database in (dropped, empty):
            assert (listed[database]["parent_run_id"], listed[database]["branch_point"]) == (
                None,
                None,
            ), "the list still lists a run it cannot read; its page says why"
    finally:
        for database in (dropped, empty):
            forget(admin_dsn, database)
        drop_database(admin_dsn, empty)


def test_a_registry_that_cannot_be_read_is_503(admin_dsn: str, db_prefix: str) -> None:
    missing, unmigrated = f"{db_prefix}_viewer_no_registry", f"{db_prefix}_viewer_bare_registry"
    drop_database(admin_dsn, missing)
    recreate_database(admin_dsn, unmigrated)
    try:
        for registry in (missing, unmigrated):
            client = TestClient(create_app(RunSource(admin_dsn, registry), static=None))
            response = client.get("/api/runs")
            assert response.status_code == 503, registry
            assert response.json()["error"] == "registry_unavailable"
    finally:
        drop_database(admin_dsn, unmigrated)


def test_the_built_frontend_is_served_and_its_absence_is_explained(
    admin_dsn: str, tmp_path: Path
) -> None:
    (tmp_path / "index.html").write_text("<!doctype html><title>Fivefold</title>", "utf-8")
    built = TestClient(create_app(RunSource(admin_dsn, registry_database()), static=tmp_path))
    assert built.get("/").text == "<!doctype html><title>Fivefold</title>"
    absent = TestClient(create_app(RunSource(admin_dsn, registry_database()), static=None))
    response = absent.get("/")
    assert response.status_code == 404
    assert "npm run build" in response.text
