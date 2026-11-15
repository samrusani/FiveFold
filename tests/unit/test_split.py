"""The split topology's parts that need no database or service (0008, 0009)."""

from __future__ import annotations

import stat
from pathlib import Path
from typing import Any

import pytest
import yaml

from fivefold.domain.contracts import Contract
from fivefold.domain.scenario import scenario_from_data
from fivefold.faults.catalog import Action
from fivefold.faults.plan import FaultInjector, Fired, parse_plan
from fivefold.harness.log import MemoryHarnessLog
from fivefold.harness.scheduler import Scheduler
from fivefold.harness.split.__main__ import main
from fivefold.harness.split.credentials import AGENT, GATEWAY, HARNESS, Callers, bearer, issue
from fivefold.harness.split.faults import SPLIT_POINTS, SplitRefused, check_split
from fivefold.harness.split.hops import Hops, answer_class
from fivefold.harness.split.lifecycle import default_database, start_run
from fivefold.harness.split.rundir import (
    RunDirError,
    SplitRun,
    clear_run,
    read_run,
    write_run,
)
from fivefold.harness.transport import Envelope

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


def test_a_run_directory_reads_back_as_written(tmp_path: Path) -> None:
    secrets = issue()
    run = _run(tmp_path / "run")
    write_run(run, scenario_text="id: x\n", secrets=secrets, callers=Callers.of(secrets))
    read = read_run(tmp_path / "run")
    assert read == run
    assert read.secret(AGENT) == secrets[AGENT]
    assert read.callers().identify(f"Bearer {secrets[HARNESS]}") == HARNESS
    for holder in secrets:
        mode = (tmp_path / "run" / f"{holder}.token").stat().st_mode
        assert stat.S_IMODE(mode) == 0o600
    assert not read.ended
    read.mark_ended(1234)
    assert read.ended


def test_a_run_directory_holds_one_run(tmp_path: Path) -> None:
    secrets = issue()
    run = _run(tmp_path)
    write_run(run, scenario_text="", secrets=secrets, callers=Callers.of(secrets))
    with pytest.raises(RunDirError, match="already holds a run"):
        write_run(run, scenario_text="", secrets=secrets, callers=Callers.of(secrets))
    clear_run(tmp_path)
    with pytest.raises(RunDirError, match="start one first"):
        read_run(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_default_database_names_are_safe_and_short() -> None:
    name = default_database("dev-001-single-po.G2.C2.7")
    assert name == "fivefold_split_dev_001_single_po_g2_c2_7"
    assert len(default_database("x" * 200)) == 63


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
