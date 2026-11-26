"""The split topology's parts that need no database or service (0008, 0009)."""

from __future__ import annotations

import asyncio
import json
import re
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx
import mcp_types.jsonrpc as jsonrpc
import pytest
import yaml
from mcp.server import streamable_http as mcp_streamable_http

from fivefold.domain.contracts import Contract, ContractParams
from fivefold.domain.scenario import scenario_from_data
from fivefold.faults.catalog import Action
from fivefold.faults.plan import FaultInjector, Fired, parse_plan
from fivefold.gateway.destination import FactNow
from fivefold.gateway.dispatcher import Dispatcher
from fivefold.gateway.memory import MemoryStore
from fivefold.gateway.records import Action as GatewayAction
from fivefold.gateway.records import (
    ActionResult,
    Answer,
    Attempt,
    BasisCheck,
    CheckRecord,
    WriteCall,
)
from fivefold.gateway.sim_client import SimClient
from fivefold.gateway.state_machine import ActionState, CheckKind, GatewaySettings, Reason
from fivefold.harness.clock import HostClock
from fivefold.harness.log import MemoryHarnessLog, holds_nul
from fivefold.harness.migrations import check_database_name
from fivefold.harness.scheduler import ComponentFailed, HostScheduler, Scheduler
from fivefold.harness.split import SERVICE_STOPPED
from fivefold.harness.split import __main__ as split_main
from fivefold.harness.split.__main__ import main
from fivefold.harness.split.credentials import AGENT, GATEWAY, HARNESS, Callers, bearer, issue
from fivefold.harness.split.faults import SPLIT_POINTS, SplitRefused, check_split
from fivefold.harness.split.gateway import Attestations, AttestationsClosed, SimulatorLink
from fivefold.harness.split.hops import SEQ_HEADER, Hops, answer_class
from fivefold.harness.split.lifecycle import default_database, start_run
from fivefold.harness.split.rundir import (
    Config,
    RunDirError,
    SplitRun,
    new_run_directory,
    read_run,
    write_run,
)
from fivefold.harness.transport import Envelope

START_S = 30.0
"""How long a wait in these tests may take at most, in wall-clock seconds."""

UNREACHABLE_DSN = "postgresql://nobody@127.0.0.1:1/none"
"""A server nothing listens on: a command that reached it would fail to connect."""


# Credentials ------------------------------------------------------------------------------


def test_each_holder_gets_its_own_secret_and_is_known_by_it() -> None:
    secrets = issue()
    assert sorted(secrets) == sorted([GATEWAY, AGENT, HARNESS])
    assert len(set(secrets.values())) == 3
    callers = Callers.of(secrets)
    for holder, secret in secrets.items():
        assert callers.identify(f"Bearer {secret}") == holder
        assert callers.identify(f"bearer {secret}") == holder
    assert secrets[GATEWAY] not in callers.digests.values()


@pytest.mark.parametrize(
    "header", [None, "", "Bearer", "Bearer   ", "Basic abc", "Bearer not-a-secret-of-this-run"]
)
def test_a_caller_without_a_secret_of_the_run_is_nobody(header: str | None) -> None:
    assert Callers.of(issue()).identify(header) is None


def test_bearer_reads_only_bearer_tokens() -> None:
    assert bearer("Bearer abc") == "abc"
    assert bearer("Token abc") is None
    assert bearer(None) is None


# The run's directory ------------------------------------------------------------------------


def _run(directory: Path) -> SplitRun:
    return SplitRun(
        directory, "dev-001-single-po.G2.C2.7", "fivefold_split_x", "G2", Contract.C2, 7
    )


def _write_run(run: SplitRun, secrets: dict[str, str], scenario_text: str = "") -> None:
    with new_run_directory(run.directory) as reserved:
        write_run(
            run,
            reserved,
            scenario_text=scenario_text,
            secrets=secrets,
            callers=Callers.of(secrets),
        )


def test_a_run_directory_reads_back_as_written(tmp_path: Path) -> None:
    secrets = issue()
    run = _run(tmp_path / "run")
    _write_run(run, secrets, "id: x\n")
    read = read_run(tmp_path / "run")
    assert read == run
    assert read.secret(AGENT) == secrets[AGENT]
    assert read.callers().identify(f"Bearer {secrets[HARNESS]}") == HARNESS
    assert stat.S_IMODE(run.directory.stat().st_mode) == 0o700
    for holder in secrets:
        mode = (tmp_path / "run" / f"{holder}.token").stat().st_mode
        assert stat.S_IMODE(mode) == 0o600
    assert not read.ended
    read.mark_ended(1234)
    assert read.ended


def test_a_run_directory_holds_one_run(tmp_path: Path) -> None:
    secrets = issue()
    run = _run(tmp_path / "run")
    _write_run(run, secrets)
    with pytest.raises(RunDirError, match="already exists") as refused:
        _write_run(run, issue())
    assert str(run.directory) in str(refused.value)
    assert read_run(run.directory).secret(AGENT) == secrets[AGENT]


@pytest.mark.parametrize("is_link", [False, True])
def test_existing_tokens_are_left_untouched(tmp_path: Path, is_link: bool) -> None:
    run = _run(tmp_path / "run")
    secrets = issue()
    token = run.directory / f"{AGENT}.token"
    target = tmp_path / "target" if is_link else token
    with (
        pytest.raises(RunDirError, match=f"already contains {AGENT}.token"),
        new_run_directory(run.directory) as reserved,
    ):
        target.write_text("sentinel\n", encoding="utf-8")
        target.chmod(0o644)
        if is_link:
            token.symlink_to(target)
        write_run(run, reserved, scenario_text="", secrets=secrets, callers=Callers.of(secrets))
    assert target.read_text(encoding="utf-8") == "sentinel\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert token.is_symlink() == is_link
    assert list(run.directory.iterdir()) == [token]
    with pytest.raises(RunDirError, match="start one first"):
        read_run(run.directory)
    with pytest.raises(RunDirError, match="remove this one") as refused:
        _write_run(run, secrets)
    assert str(run.directory) in str(refused.value)


def test_a_failed_start_removes_its_files_and_directory(tmp_path: Path) -> None:
    run = _run(tmp_path / "run")
    sibling = tmp_path / "sibling"
    sibling.write_text("keep me", encoding="utf-8")
    secrets = issue()
    with (
        pytest.raises(RuntimeError, match="start failed"),
        new_run_directory(run.directory) as reserved,
    ):
        write_run(run, reserved, scenario_text="", secrets=secrets, callers=Callers.of(secrets))
        assert list(run.directory.iterdir())
        raise RuntimeError("start failed")
    assert not run.directory.exists()
    assert sibling.read_text(encoding="utf-8") == "keep me"


def test_default_database_names_are_safe_and_short() -> None:
    name = default_database("dev-001-single-po.G2.C2.7")
    assert name == "fivefold_split_dev_001_single_po_g2_c2_7"
    assert len(default_database("x" * 200)) == 63


def test_a_long_default_database_name_keeps_its_start_and_a_hash_of_the_run_id() -> None:
    """PostgreSQL keeps 63 bytes of a name. Cut short there, the names of seeds 7 and 8 of
    a scenario with a 42-character id were one, and `start` with replace for one dropped
    the other's database."""
    scenario = "s" * 42
    seven, eight = (default_database(f"{scenario}.G2.C2.{seed}") for seed in (7, 8))
    assert seven != eight
    for name in (seven, eight):
        assert len(name.encode()) == 63
        assert re.fullmatch(rf"fivefold_split_{'s' * 39}_[0-9a-f]{{8}}", name), name
        check_database_name(name)
    assert default_database(f"{scenario}.G2.C2.7") == seven


# Faults the split topology runs -----------------------------------------------------------------


def _entry(fault: str, point: str, **extra: Any) -> dict[str, Any]:
    return {"fault": fault, "at": {"point": point, "occurrence": 1}, **extra}


SUPPORTED = [
    _entry("F12", "net.agent_gw.request"),
    _entry("F4", "net.gw_sim.request"),
    _entry("F2", "net.gw_sim.answer"),
    _entry("F12", "net.agent_sim.request"),
    _entry("F2", "net.agent_sim.answer"),
    _entry("F6", "net.agent_sim.answer", variant="early"),
    _entry("F7", "net.gw_sim.request", variant="price_up"),
    _entry("F5", "sim.write.received"),
    _entry("F10", "sim.status.answer", variant="delayed"),
]


@pytest.mark.parametrize("entry", SUPPORTED, ids=lambda entry: str(entry["at"]["point"]))
def test_faults_on_the_network_and_in_the_simulator_run(
    entry: dict[str, Any], dev_data: dict[str, object]
) -> None:
    check_split(parse_plan([entry]), scenario_from_data(dev_data))
    assert entry["at"]["point"] in SPLIT_POINTS


@pytest.mark.parametrize(
    "entry",
    [
        _entry("F1", "gw.dispatch.after_check", variant="a"),
        _entry("F3", "gw.dispatch.after_answer"),
        _entry("F6", "gw.dispatch.before_check", variant="early"),
        _entry("F11", "agent.after_tool_call", variant="after_tool_call"),
        _entry("F9", "agent.step", variant="same_args"),
        _entry("F1", "agent.before_tool_call", variant="a"),
    ],
    ids=lambda entry: str(entry["at"]["point"]),
)
def test_faults_inside_the_gateway_or_the_agent_are_refused_in_one_line(
    entry: dict[str, Any], dev_data: dict[str, object]
) -> None:
    with pytest.raises(SplitRefused) as refused:
        check_split(
            parse_plan([_entry("F2", "net.gw_sim.answer"), entry]), scenario_from_data(dev_data)
        )
    message = str(refused.value)
    assert "\n" not in message
    assert f"{entry['fault']} at {entry['at']['point']}" in message
    assert "F2" not in message


def test_a_scenario_with_a_scripted_resolver_is_refused(dev_data: dict[str, object]) -> None:
    authority = dict(dev_data["authority"])  # type: ignore[call-overload]
    authority["resolver"] = {"after_ms": 30_000, "attests": "truth"}
    dev_data["authority"] = authority
    with pytest.raises(SplitRefused, match="no scripted resolver"):
        check_split((), scenario_from_data(dev_data))


def test_start_refuses_a_plan_it_cannot_run_before_touching_a_database(
    tmp_path: Path, dev_data: dict[str, object]
) -> None:
    scenario = tmp_path / "scenario.yaml"
    scenario.write_text(yaml.safe_dump(dev_data), encoding="utf-8")
    plan = [_entry("F11", "agent.after_tool_call", variant="after_tool_call")]
    with pytest.raises(SplitRefused, match=r"F11 at agent\.after_tool_call"):
        start_run(
            admin_dsn=UNREACHABLE_DSN,
            scenario_path=scenario,
            config="G2",
            contract=Contract.C2,
            seed=7,
            run_dir=tmp_path / "run",
            plan=plan,
        )
    assert not (tmp_path / "run").exists()


def test_start_refuses_a_random_plan_in_one_line_before_touching_a_database(
    tmp_path: Path, dev_data: dict[str, object]
) -> None:
    """A random plan draws from points the split topology mostly cannot fire at; it runs
    in the inproc topology (0006, Plans)."""
    dev_data["faults"] = {"random": {"from": ["F2", "F12"], "max": 2}}
    scenario = tmp_path / "scenario.yaml"
    scenario.write_text(yaml.safe_dump(dev_data), encoding="utf-8")
    with pytest.raises(SplitRefused, match="runs a fixed fault plan only") as refused:
        start_run(
            admin_dsn=UNREACHABLE_DSN,
            scenario_path=scenario,
            config="G2",
            contract=Contract.C2,
            seed=7,
            run_dir=tmp_path / "run",
        )
    assert "\n" not in str(refused.value) and "inproc topology" in str(refused.value)
    assert not (tmp_path / "run").exists()


def test_the_command_line_refuses_such_a_plan_in_one_line(
    tmp_path: Path, dev_data: dict[str, object], capsys: pytest.CaptureFixture[str]
) -> None:
    scenario = tmp_path / "scenario.yaml"
    scenario.write_text(yaml.safe_dump(dev_data), encoding="utf-8")
    plan = tmp_path / "plan.yaml"
    plan.write_text(
        yaml.safe_dump([_entry("F1", "gw.dispatch.after_record", variant="b")]), encoding="utf-8"
    )
    status = main(
        [
            "--run-dir",
            str(tmp_path / "run"),
            "--dsn",
            UNREACHABLE_DSN,
            "start",
            "--scenario",
            str(scenario),
            "--config",
            "G2",
            "--contract",
            "C2",
            "--plan",
            str(plan),
        ]
    )
    error = capsys.readouterr().err
    assert status == 1
    assert error.count("\n") == 1
    assert "cannot run F1 at gw.dispatch.after_record" in error


def _started(
    tmp_path: Path,
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> tuple[str, dict[str, str]]:
    """What `start` prints for a run in `config`, made here without a database; and the
    run's secrets."""
    secrets = issue()
    run = SplitRun(
        tmp_path / "run",
        f"dev-001-single-po.{config}.C2.7",
        "fivefold_split_x",
        config,
        Contract.C2,
        7,
    )
    _write_run(run, secrets)

    def made(**_: object) -> SplitRun:
        return run

    monkeypatch.setattr(split_main, "start_run", made)
    argv = ["--run-dir", str(run.directory), "--dsn", UNREACHABLE_DSN, "start"]
    argv += ["--scenario", "unused.yaml", "--config", config, "--contract", "C2"]
    assert main(argv) == 0
    return " ".join(capsys.readouterr().out.split()), secrets


@pytest.mark.parametrize("config", ["G1", "G2"])
def test_start_sends_a_g_mode_agent_to_a_container_on_the_agents_network(
    tmp_path: Path,
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Record 0008's boundary holds only for an agent in a container on `fivefold-agent`:
    on the host, an agent can reach the simulator, PostgreSQL and the run's directory."""
    out, _ = _started(tmp_path, config, monkeypatch, capsys)
    assert "run your agent in a container on the Docker network fivefold-agent" in out
    assert "pointed at the gateway's MCP endpoint, http://gateway:8000/mcp." in out
    assert "127.0.0.1" not in out
    assert "an agent on this host can reach the simulator, PostgreSQL and this run's" in out


def test_start_points_a_b0_agent_at_the_simulators_api_on_the_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    out, secrets = _started(tmp_path, "B0", monkeypatch, capsys)
    assert "point your agent at its API, http://127.0.0.1:8100/api/CALL" in out
    assert f"Authorization: Bearer {secrets[AGENT]}" in out


# The harness's view of a service's traffic -----------------------------------------------------


def _hops(plan: list[dict[str, Any]], applied: list[str]) -> tuple[Hops, MemoryHarnessLog]:
    log = MemoryHarnessLog()

    def effect(fired: Fired, envelope: Envelope) -> None:
        applied.append(f"{fired.fault} on {envelope.seq}")

    return Hops(Scheduler(), log, FaultInjector(parse_plan(plan)), effect=effect), log


def _request(hops: Hops, api: str = "submit_purchase_order") -> Envelope:
    return hops.sent(
        hop="gw_sim",
        kind="request",
        sender="gateway",
        receiver="simulator",
        api=api,
        body={"intent_id": "REQ-1"},
        intent_id="REQ-1",
    )


def test_a_dropped_message_is_recorded_with_the_fault_that_dropped_it() -> None:
    plan = [{"fault": "F12", "at": {"point": "net.gw_sim.request", "occurrence": 2}}]
    hops, log = _hops(plan, [])
    first = _request(hops)
    assert not hops.fire("net.gw_sim.request", first).dropped
    hops.delivered(first)
    second = _request(hops)
    assert hops.fire("net.gw_sim.request", second).dropped
    hops.dropped(second, "fault")
    assert [
        (fault.fault, fault.occurrence, fault.target_seq, fault.intent_id) for fault in log.faults
    ] == [("F12", 2, second.seq, "REQ-1")]
    fates = {message.seq: (message.fate, message.drop_reason) for message in log.messages}
    assert fates == {first.seq: ("delivered", None), second.seq: ("dropped", "fault")}


def test_a_copy_repeats_the_request_and_names_it() -> None:
    hops, log = _hops([_entry("F4", "net.gw_sim.request", params={"copy_delay_ms": 7})], [])
    original = _request(hops)
    outcome = hops.fire("net.gw_sim.request", original)
    assert not outcome.dropped
    (fired,) = outcome.copies
    assert fired.planned.kind.action is Action.DUPLICATE
    copy = hops.copy(fired, "net.gw_sim.request", original)
    assert copy.copy_of == original.seq
    assert (copy.body, copy.sent_vt, copy.api) == (original.body, original.sent_vt, original.api)
    (fault,) = log.faults
    assert (fault.target_seq, fault.intent_id) == (original.seq, "REQ-1")
    assert fault.params == {"copy_delay_ms": 7, "copy_seq": copy.seq}
    assert [message.copy_of for message in log.messages] == [None, original.seq]


def test_a_fault_that_changes_the_simulator_is_applied_where_it_fires() -> None:
    applied: list[str] = []
    hops, log = _hops([_entry("F6", "net.gw_sim.request", variant="late")], applied)
    first, second = _request(hops), _request(hops)
    assert not hops.fire("net.gw_sim.request", first).dropped
    assert not hops.fire("net.gw_sim.request", second).dropped
    assert applied == [f"F6 on {first.seq}"]
    assert [fault.target_seq for fault in log.faults] == [first.seq]


def test_only_a_writes_answer_has_an_answer_class() -> None:
    assert answer_class("submit_purchase_order", {"answer": "committed"}) == "committed"
    assert answer_class("reserve_inventory", {"answer": "in_progress"}) == "in_progress"
    assert answer_class("get_quote", {"answer": "fact"}) is None
    assert answer_class("submit_purchase_order", ["not", "a", "body"]) is None


# The gateway's writes to the simulator -----------------------------------------------------------

ORDER_ARGS: dict[str, str | int] = {
    "supplier_id": "S-1",
    "sku": "W-100",
    "quantity": 100,
    "unit_price_minor": 540,
    "currency": "USD",
    "cost_center": "CC-OPS",
}


def _dispatched_order(sent_vt: int) -> tuple[GatewayAction, Attempt]:
    """A G2 order at C2, whose first attempt the gateway recorded at `sent_vt`."""
    action = GatewayAction(
        action_id="A-0001",
        task_id="T-1",
        intent_id="REQ-1",
        operation="submit_purchase_order",
        target="S-1",
        args=ORDER_ARGS,
        action_key="REQ-1",
        identity=True,
        contract=Contract.C2,
        basis={"quote/S-1/W-100": 1, "budget/CC-OPS": 1},
        state=ActionState.DISPATCHED,
        grant_id="G-0001",
        idempotency_key="A-0001",
        effect_id=None,
        created_vt=0,
        updated_vt=sent_vt,
    )
    check = CheckRecord(
        kind=CheckKind.EXEMPT, approval=None, basis=None, seen_seq=1, check_vt=sent_vt
    )
    attempt = Attempt(
        attempt_id="A-0001/1",
        action_id="A-0001",
        attempt_no=1,
        idempotency_key="A-0001",
        grant_id="G-0001",
        check=check,
        sent_vt=sent_vt,
        answer_class=None,
        answer_ref=None,
        answered_vt=None,
    )
    return action, attempt


@pytest.mark.parametrize(
    "response_kind", ["5xx", "non_json", "missing_seq", "invalid_seq", "unlogged_seq", "encoding"]
)
def test_the_gateway_logs_a_write_at_its_attempts_send_time(response_kind: str) -> None:
    """The gateway records a write's attempt, with its send time, in a transaction before
    it sends the write (0003); on the host's clock milliseconds pass before the link logs
    the request. The log takes the attempt's time, so the gateway's settle bounds and the
    evaluator's start from one number (0004, 0005)."""
    log = MemoryHarnessLog()
    scheduler = HostScheduler(HostClock(HostClock.host_ms() - 1_000))
    action, attempt = _dispatched_order(sent_vt=scheduler.now - 7)

    def simulator(request: httpx.Request) -> httpx.Response:
        if response_kind == "5xx":
            return httpx.Response(500)
        if response_kind == "non_json":
            return httpx.Response(200, headers={SEQ_HEADER: "2"}, text="service unavailable")
        if response_kind == "encoding":
            raise httpx.DecodingError("invalid response encoding", request=request)
        headers = {
            "missing_seq": {},
            "invalid_seq": {SEQ_HEADER: "unavailable"},
            "unlogged_seq": {SEQ_HEADER: "2"},
        }[response_kind]
        return httpx.Response(200, headers=headers, json={})

    async def send() -> Answer:
        transport = httpx.MockTransport(simulator)
        async with httpx.AsyncClient(base_url="http://simulator", transport=transport) as client:
            hops = Hops(scheduler, log, FaultInjector())
            link = SimulatorLink(
                scheduler, hops, client, secret="fixture-secret", read_message=lambda _: None
            )
            return await SimClient(link, timeout_ms=800).send(action, attempt, "APR-0001")

    answer = asyncio.run(send())
    (request,) = log.messages
    assert isinstance(request.body, dict)
    assert (request.hop, request.api, request.body["client_ref"]) == (
        "gw_sim",
        "submit_purchase_order",
        "A-0001/1",
    )
    assert request.sent_vt == attempt.sent_vt
    assert answer.reason is Reason.SERVER_ERROR


class NoDependencies:
    def keys(self, action: GatewayAction) -> Sequence[str]:
        return ()

    def check(self, action: GatewayAction, now: Mapping[str, FactNow], *, full: bool) -> BasisCheck:
        return BasisCheck(exact_ok=True, sufficient_ok=True if full else None)


def _dispatch_over_http(
    contract: Contract, *, mismatched_approval: str | None = None
) -> tuple[MemoryStore, list[str]]:
    scheduler = Scheduler()
    log = MemoryHarnessLog()
    hops = Hops(scheduler, log, FaultInjector())
    store = MemoryStore(
        task_id="T-1",
        intents={"REQ-1": "submit_purchase_order"},
        contract=contract,
        settings=GatewaySettings(
            approval_wait_ms=5_000,
            params=ContractParams(max_hop_latency_ms=50, commit_deadline_ms=1_000),
            dispatch_timeout_ms=200,
            max_attempts=3,
            max_probes=3,
        ),
    )
    effects: list[str] = []
    messages: dict[int, tuple[Envelope, bool]] = {}
    sends = 0
    scope: dict[str, Any] = {}

    def simulator(request: httpx.Request) -> httpx.Response:
        nonlocal sends, scope
        api = request.url.path.rsplit("/", 1)[1]
        data = json.loads(request.content)
        if api in {"request_approval", "get_approval_status"}:
            if api == "request_approval":
                scope = data["scope"]
            body: Any = {
                "answer": "approval",
                "approval_id": "APR-0001",
                "intent_id": "REQ-1",
                "operation": "submit_purchase_order",
                "scope": scope,
                "status": "approved",
                "requested_vt": 0,
                "expires_at": 50_000,
            }
            if api == "get_approval_status" and mismatched_approval is not None:
                if mismatched_approval == "scope":
                    body["scope"] = {**scope, "max_quantity": 1}
                elif mismatched_approval == "operation":
                    body["operation"] = "reserve_inventory"
                    body["scope"] = {"warehouse_id": "WH-1", "sku": "W-100", "max_quantity": 1}
                else:
                    body[mismatched_approval] = "different"
        elif api == "submit_purchase_order":
            sends += 1
            if sends == 1 and mismatched_approval is None:
                raise httpx.ConnectError("simulated connection failure", request=request)
            effects.append("PO-0001")
            body = {
                "answer": "committed",
                "effect_id": effects[-1],
                "replay": False,
                "intent_id": "REQ-1",
                "operation": api,
                "args": ORDER_ARGS,
                "committed_vt": scheduler.now,
            }
        else:
            assert api == "get_order_status"
            body = {
                "answer": "status",
                "intent_id": "REQ-1",
                "operation": "submit_purchase_order",
                "effects": [],
                "as_of_vt": scheduler.now,
            }
        answer = hops.sent(
            hop="gw_sim",
            kind="answer",
            sender="simulator",
            receiver="gateway",
            api=api,
            body=body,
            correlation=int(request.headers[SEQ_HEADER]),
        )
        messages[answer.seq] = (answer, False)
        return httpx.Response(200, headers={SEQ_HEADER: str(answer.seq)}, json=body)

    async def main() -> None:
        async with httpx.AsyncClient(
            base_url="http://simulator", transport=httpx.MockTransport(simulator)
        ) as client:
            link = SimulatorLink(
                scheduler, hops, client, secret="fixture-secret", read_message=messages.get
            )
            destination = SimClient(link, timeout_ms=200)
            dispatcher = Dispatcher(scheduler, store, destination, NoDependencies())
            result = store.propose(
                WriteCall("REQ-1", "submit_purchase_order", "S-1", ORDER_ARGS, "REQ-1", (), ()),
                scheduler.now,
            )
            assert isinstance(result, ActionResult)
            dispatcher.drive(result.action_id)
            await scheduler.run(deadline_ms=10_000)

    if mismatched_approval is None:
        asyncio.run(main())
    else:
        with pytest.raises(ComponentFailed, match="simulator returned a mismatched approval"):
            asyncio.run(main())
    return store, effects


@pytest.mark.parametrize("contract", [Contract.C1, Contract.C2])
def test_split_connection_failure_leaves_dispatched(contract: Contract) -> None:
    store, effects = _dispatch_over_http(contract)
    view = store.view("A-0001")
    assert any(event.reason is Reason.TRANSPORT_ERROR for event in view.events)
    if contract is Contract.C2:
        assert view.action.state is ActionState.CONFIRMED
        assert [attempt.idempotency_key for attempt in view.attempts] == ["A-0001", "A-0001"]
        assert effects == ["PO-0001"]
    else:
        assert view.action.state is ActionState.FAILED
        assert effects == []


@pytest.mark.parametrize("field", ["approval_id", "intent_id", "operation", "scope"])
def test_split_dispatch_stops_on_a_mismatched_approval(field: str) -> None:
    store, effects = _dispatch_over_http(Contract.C2, mismatched_approval=field)
    view = store.view("A-0001")
    assert view.action.state is ActionState.AUTHORIZED
    assert view.attempts == ()
    assert effects == []


def test_a_message_cannot_be_logged_as_sent_later_than_now() -> None:
    hops, _ = _hops([], [])
    with pytest.raises(ValueError, match="cannot be sent later"):
        hops.sent(
            hop="gw_sim",
            kind="request",
            sender="gateway",
            receiver="simulator",
            api="submit_purchase_order",
            body={},
            sent_vt=1,
        )


# The code that tells the agent its run is over ---------------------------------------------------


def test_the_stopped_runs_error_code_is_none_the_mcp_sdk_uses() -> None:
    """The gateway answered an agent once the agents' phase was over with -32000, which
    mcp 2.3.0 calls `CONNECTION_CLOSED`, so the scripted agent read the run's end as a
    closed connection and reconnected. Its code is now one the SDK does not use."""
    defined = {
        value for name, value in vars(jsonrpc).items() if name.isupper() and isinstance(value, int)
    }
    defined.add(mcp_streamable_http.REQUEST_CANCELLED)
    assert jsonrpc.CONNECTION_CLOSED in defined, "the scan finds the SDK's codes"
    assert SERVICE_STOPPED not in defined
    assert -32019 <= SERVICE_STOPPED <= -32000, "an implementation-defined server error"


# The gateway's gate for attestations ------------------------------------------------------------


def test_the_attestation_gate_settles_once_the_last_attestation_in_flight_has_logged() -> None:
    """Stop closes the gate, so no resolution commits after it, and closes the harness's
    records only once no attestation is in flight."""

    async def gated() -> list[str]:
        gate, settled = Attestations(), list[str]()
        clock = HostClock(HostClock.host_ms())
        gate.when_settled(lambda: settled.append("open, none in flight"), clock=clock, limit_ms=1)
        with gate.attesting():
            gate.close()
            with pytest.raises(AttestationsClosed), gate.attesting():
                pass
            gate.when_settled(lambda: settled.append("closed"), clock=clock, limit_ms=60_000)
            assert settled == ["open, none in flight"]
        return settled

    assert asyncio.run(gated()) == ["open, none in flight", "closed"]


def test_the_attestation_gate_settles_after_its_bound_if_one_never_logs() -> None:
    async def stuck() -> list[str]:
        gate, settled, done = Attestations(), list[str](), asyncio.Event()
        clock = HostClock(HostClock.host_ms())

        def close_records() -> None:
            settled.append("closed")
            done.set()

        with gate.attesting():
            gate.close()
            gate.when_settled(close_records, clock=clock, limit_ms=20)
            await asyncio.wait_for(done.wait(), START_S)
        return settled

    assert asyncio.run(stuck()) == ["closed"]


# What the harness's log cannot store -------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "holds"),
    [
        ("text", False),
        ("a\x00b", True),
        ({"claims": [{"effect_ids": ["PO-0001\x00"]}]}, True),
        ({"key\x00": 1}, True),
        ([1, 2.5, None, True, {"a": ["b"]}], False),
    ],
)
def test_a_nul_anywhere_in_a_json_value_is_found(value: object, holds: bool) -> None:
    assert holds_nul(value) is holds
