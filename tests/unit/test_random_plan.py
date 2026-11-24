"""Random fault plans: drawn from the run's seed, as fixed plans the run accepts (0006, 0009).

A random plan, `{random: {from: [...], max: N}}`, becomes a fixed list before the run
starts. Every choice comes from the run's `faults` generator, so a seed gives one plan, and
only what a fixed plan could say is drawn (`fivefold.faults.plan`, Random plans).
"""

from __future__ import annotations

import copy
import itertools
from collections.abc import Mapping
from typing import Any

import pytest
from pydantic import ValidationError

from fivefold.domain.contracts import Contract
from fivefold.domain.scenario import Scenario, load_scenario_file, scenario_from_data
from fivefold.faults.catalog import BUILT, EARLY_POINTS, Action, FaultId
from fivefold.faults.plan import (
    AGENT_RESTART_MAX_MS,
    GATEWAY_RESTART_MAX_MS,
    PERCENT_MAX,
    PlannedFault,
    check_configuration,
    check_run,
    draw_plan,
    drawable_faults,
    parse_plan,
    parse_random_plan,
    seed_parameters,
)
from fivefold.harness.demo import DEV_001, DEV_002
from fivefold.harness.rng import Generator, Generators
from fivefold.harness.run import CONFIGS, GATEWAY_CONFIGS, RunSpec, run_plan

DEV = load_scenario_file(DEV_001)
HUMAN = load_scenario_file(DEV_002)
"""dev-002: dev-001 with an order above the auto-approval threshold, so a human approves."""

SEEDS = range(200)
EVERY = [str(fault) for fault in BUILT if fault is not FaultId.F8]
"""Every fault this build can inject."""


def random(*faults: str, most: int = 3) -> dict[str, Any]:
    """A random plan as a scenario or a run writes it (0006, Plans)."""
    return {"random": {"from": list(faults), "max": most}}


def drawn(
    plan: Mapping[str, Any],
    seed: int,
    *,
    config: str = "G2",
    contract: Contract = Contract.C1,
    scenario: Scenario = DEV,
) -> tuple[PlannedFault, ...]:
    """The plan a run with this seed draws, before its seeded parameters. C1 by default,
    where every fault this build has can fire in G2."""
    return draw_plan(
        parse_random_plan(plan),
        Generators(seed).get("faults"),
        scenario=scenario,
        config=config,
        contract=contract,
    )


def as_text(plan: tuple[PlannedFault, ...]) -> list[dict[str, Any]]:
    return [entry.model_dump(mode="json") for entry in plan]


# The format ------------------------------------------------------------------------------


def test_a_random_plan_reads_as_record_0006_writes_it() -> None:
    plan = parse_random_plan({"random": {"from": ["F2", "F4", "F5", "F12"], "max": 3}})
    assert plan.faults == (FaultId.F2, FaultId.F4, FaultId.F5, FaultId.F12)
    assert plan.most == 3


@pytest.mark.parametrize(
    ("faults", "message"),
    [
        ({"random": {"from": ["F2"]}}, "max"),
        ({"random": {"from": ["F2"], "max": 0}}, "max"),
        ({"random": {"from": [], "max": 1}}, "from"),
        ({"random": {"from": ["F2", "F2"], "max": 2}}, "each fault once"),
        ({"random": {"from": ["F13"], "max": 1}}, "from"),
        ({"random": {"from": ["F2"], "max": 1, "seed": 7}}, "seed"),
        ({"random": {"faults": ["F2"], "max": 1}}, "faults"),
        ({"random": {"from": ["F2"], "max": 1}, "fixed": []}, "fixed"),
        ({"chosen": {"from": ["F2"], "max": 1}}, "random"),
    ],
)
def test_another_shape_is_refused(faults: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        parse_random_plan(faults)


# The draw ----------------------------------------------------------------------------------


def test_the_same_seed_draws_the_same_plan() -> None:
    """0009: a seed reproduces the run, so it must reproduce the plan first."""
    plan = random(*EVERY, most=len(EVERY))
    for seed in SEEDS:
        assert as_text(drawn(plan, seed)) == as_text(drawn(plan, seed))


def test_other_seeds_draw_other_plans() -> None:
    """The plan follows the seed. A plan of one fault with no parameter, such as F2, has
    only a few forms, so a few seeds share one: 200 seeds draw 193 plans."""
    plan = random(*EVERY, most=len(EVERY))
    assert len({str(as_text(drawn(plan, seed))) for seed in SEEDS}) >= 0.9 * len(SEEDS)


def test_a_draw_takes_from_the_faults_generator_alone() -> None:
    """Each named generator has its own stream (0009): the latency draws the transport
    takes never move a fault plan, and a plan never moves them."""
    plan = parse_random_plan(random(*EVERY, most=len(EVERY)))

    def latencies(generators: Generators) -> list[float]:
        latency = generators.get("latency")
        return [latency.random() for _ in range(5)]

    def draw(generators: Generators) -> list[dict[str, Any]]:
        faults = generators.get("faults")
        return as_text(draw_plan(plan, faults, scenario=DEV, config="G2", contract=Contract.C1))

    alone = latencies(Generators(7))
    plan_first = Generators(7)
    first = draw(plan_first)
    assert latencies(plan_first) == alone, "a plan never moves the latency draws"
    latency_first = Generators(7)
    latencies(latency_first)
    assert draw(latency_first) == first, "latency draws never move the plan"


@pytest.mark.parametrize(("config", "contract"), list(itertools.product(CONFIGS, Contract)))
def test_a_drawn_plan_is_a_fixed_plan_the_run_accepts(config: str, contract: Contract) -> None:
    """Every entry fits the run as a fixed plan's must; the count is from 1 to `max`; each
    fault is one `from` names, drawn once, at its first matching event unless it is a
    crash; and each drawn parameter stays in its range."""
    faults = [
        str(fault) for fault in drawable_faults(scenario=DEV, config=config, contract=contract)
    ]
    for seed in range(60):
        most = seed % len(faults) + 1
        plan = drawn(random(*faults, most=most), seed, config=config, contract=contract)
        again = parse_plan(as_text(plan))
        check_configuration(again, gateway=config in GATEWAY_CONFIGS)
        check_run(again, params=DEV.params, contract=contract)
        assert 1 <= len(plan) <= most
        assert len({entry.fault for entry in plan}) == len(plan)
        assert {str(entry.fault) for entry in plan} <= set(faults)
        for entry in plan:
            assert entry.at.occurrence == 1 or entry.kind.action is Action.KILL
            assert entry.variant is not None or not entry.kind.variants, "a variant is named"
            _assert_in_range(entry)


def _assert_in_range(entry: PlannedFault) -> None:
    """The drawn parameters of `entry` are in their ranges (the module's docstring)."""
    t_ms, d_ms = DEV.params.dispatch_timeout_ms, DEV.params.commit_deadline_ms
    names = set(entry.params)
    if entry.kind.action is Action.KILL:
        assert names == {"restart_delay_ms"}
        gateway = entry.at.point.startswith("gw.")
        longest = GATEWAY_RESTART_MAX_MS if gateway else AGENT_RESTART_MAX_MS
        assert 0 <= entry.param("restart_delay_ms") <= longest
    elif entry.fault is FaultId.F5:
        delay = entry.param("delay_ms")
        if entry.variant == "beyond_deadline":
            assert d_ms < delay <= 2 * d_ms
        else:
            assert t_ms < delay < d_ms
    elif entry.fault is FaultId.F7 and entry.variant == "price_up":
        assert names == {"percent"} and 1 <= entry.param("percent") <= PERCENT_MAX
    elif entry.fault is FaultId.F10:
        assert names == {"count"} and 1 <= entry.param("count") <= 20
    else:
        assert names == set(), "F4's copy delay is left to seed_parameters"


@pytest.mark.parametrize("config", CONFIGS)
def test_every_variant_and_point_the_run_has_is_drawn(config: str) -> None:
    """Over many seeds, each fault is drawn with each of its variants at each of its points
    that the configuration has, so a suite of random runs can reach all of them."""
    contract = Contract.C1
    faults = drawable_faults(scenario=DEV, config=config, contract=contract)
    seen: set[tuple[str, str | None, str]] = set()
    for fault, seed in itertools.product(faults, range(40)):
        (entry,) = drawn(random(str(fault), most=1), seed, config=config, contract=contract)
        seen.add((str(entry.fault), entry.variant, entry.at.point))
    expected: set[tuple[str, str | None, str]] = set()
    for fault in faults:
        kind = BUILT[fault]
        variants: tuple[str | None, ...] = tuple(sorted(kind.variants)) or (None,)
        for variant in variants:
            points = kind.variants[variant] if variant is not None else None
            for point in points if points is not None else kind.points:
                planned = PlannedFault.model_validate(
                    {"fault": str(fault), "variant": variant, "at": {"point": point}}
                )
                try:
                    check_configuration([planned], gateway=config in GATEWAY_CONFIGS)
                except ValueError:
                    continue
                if fault is FaultId.F10 and variant == "stale_within_lag":
                    continue  # dev-001 has no status lag (0006, notes)
                expected.add((str(fault), variant, point))
    assert seen == expected


def test_each_trigger_targets_the_intents_write_as_the_cells_do() -> None:
    """The cells of faults/expected.yaml target the purchase order; a drawn trigger targets
    one intent's write, naming the intent and its call at the point."""
    seen: dict[str, dict[str, str | int]] = {}
    for config, seed in itertools.product(CONFIGS, range(80)):
        faults = [
            str(fault)
            for fault in drawable_faults(scenario=DEV, config=config, contract=Contract.C1)
        ]
        for entry in drawn(random(*faults, most=len(faults)), seed, config=config):
            seen[f"{entry.fault} {entry.variant} {entry.at.point}"] = entry.at.match
    order: dict[str, str | int] = {"api": "submit_purchase_order", "intent_id": "REQ-1"}
    assert seen["F2 None net.gw_sim.answer"] == {**order, "answer": "committed"}
    assert seen["F2 None net.agent_sim.answer"] == {**order, "answer": "committed"}
    assert seen["F12 None net.agent_gw.request"] == order
    assert seen["F5 in_contract sim.write.received"] == order
    assert seen["F1 b gw.dispatch.after_record"] == order
    assert seen["F6 early gw.dispatch.before_check"] == order
    assert seen["F6 early net.agent_sim.answer"] == {
        "api": "request_approval",
        "intent_id": "REQ-1",
    }
    assert seen["F11 after_tool_call agent.after_tool_call"] == {
        "tool": "submit_purchase_order",
        "intent_id": "REQ-1",
    }
    assert seen["F9 same_args agent.step"] == {"worker": "agent-1"}


def test_f10_at_a_status_answer_targets_the_status_call() -> None:
    matches = {
        str(entry.at.match)
        for seed in range(40)
        for entry in drawn(random("F10", most=1), seed, contract=Contract.C1)
        if entry.at.point == "sim.status.answer"
    }
    assert matches == {str({"api": "get_order_status", "intent_id": "REQ-1"})}


def test_crash_faults_never_share_a_trigger() -> None:
    """F3's B0 equivalent and F11 both kill the agent at `agent.after_tool_result`. A plan
    with both at one trigger is refused (`parse_plan`), so the second one drawn takes the
    next occurrence."""
    together = 0
    for seed in range(200):
        plan = drawn(random("F3", "F11", most=2), seed, config="B0")
        points = [entry.at.point for entry in plan]
        if points.count("agent.after_tool_result") == 2:
            together += 1
            assert (plan[0].at.occurrence, plan[1].at.occurrence) == (1, 2)
    assert together > 0, "the two meet at that point, at different occurrences"


def test_b0s_early_f6_and_f7_fire_only_where_the_request_answer_holds_the_approval() -> None:
    """B0 meets F6 and F7 early as its approval reaches it. dev-001's policy approves the
    order at once, in the answer to the request. dev-002's waits for a human, so that answer
    holds no approval yet, and only the late variant is drawn there."""

    def early(scenario: Scenario, fault: str) -> set[str]:
        return {
            entry.at.point
            for seed in range(60)
            for entry in drawn(random(fault, most=1), seed, config="B0", scenario=scenario)
            if entry.at.point in EARLY_POINTS
        }

    for fault in ("F6", "F7"):
        assert early(DEV, fault) == {"net.agent_sim.answer"}
        assert early(HUMAN, fault) == set()


# Refusals ----------------------------------------------------------------------------------


def test_f8_is_refused_in_a_random_plan_in_one_line() -> None:
    with pytest.raises(ValueError, match="F8 needs a fixed plan") as refused:
        drawn(random("F2", "F8"), 7)
    assert "\n" not in str(refused.value)


@pytest.mark.parametrize(
    ("config", "contract", "reason"),
    [
        ("G2", Contract.C2, "F10 needs a status lookup, which C2 does not offer"),
        ("B0", Contract.C0, "F10 needs a status lookup, which C0 does not offer"),
        ("G1", Contract.C1, "nothing in G1 asks for one"),
    ],
)
def test_f10_where_no_status_answer_is_asked_for_is_refused_in_one_line(
    config: str, contract: Contract, reason: str
) -> None:
    with pytest.raises(ValueError, match=reason) as refused:
        drawn(random("F2", "F10"), 7, config=config, contract=contract)
    message = str(refused.value)
    assert "\n" not in message and f"F10 cannot fire in {config} at {contract}" in message
    assert "F2" not in message


def test_f7_is_refused_where_no_intent_orders_from_a_quote(dev_data: dict[str, Any]) -> None:
    data = copy.deepcopy(dev_data)
    data["facts"]["inventory/WH-1/W-100"] = {"available": 50}
    data["intents"] = [
        {
            "id": "REQ-1",
            "operation": "reserve_inventory",
            "accept": {"warehouse_id": "WH-1", "sku": "W-100", "quantity": 10},
        }
    ]
    stock = scenario_from_data(data)
    with pytest.raises(ValueError, match="F7 cannot fire") as refused:
        drawn(random("F6", "F7"), 7, scenario=stock)
    assert "no task intent orders from a quote" in str(refused.value)
    plan = drawn(random("F5", "F10"), 7, scenario=stock, contract=Contract.C1)
    calls = {entry.at.match.get("api") for entry in plan}
    assert calls <= {"reserve_inventory", "get_reservation_status"}


def test_the_faults_a_random_plan_can_draw_in_dev_001() -> None:
    """F10 needs C1 and a status probe. F8 only joins fixed plans. All other faults draw."""
    others = [
        fault for fault in FaultId if fault in BUILT and fault not in (FaultId.F8, FaultId.F10)
    ]
    for config, contract in itertools.product(CONFIGS, Contract):
        found = drawable_faults(scenario=DEV, config=config, contract=contract)
        with_f10 = contract is Contract.C1 and config != "G1"
        expected = sorted([*others, *([FaultId.F10] if with_f10 else [])], key=list(FaultId).index)
        assert found == tuple(expected), (config, contract)


# The run's plan ------------------------------------------------------------------------------


def test_a_run_draws_its_random_plan_from_its_seed() -> None:
    """A run with a random plan has a fixed list, drawn from its seed, with F4's copy delay
    drawn after it from the same generator; the run's plan and the scenario's draw alike."""
    plan = random("F2", "F4", "F12", most=3)
    spec = RunSpec(DEV, "G2", Contract.C2, 11, plan=plan)
    expected = seed_parameters(drawn(plan, 11, contract=Contract.C2), _after_draw(plan, 11))
    assert as_text(run_plan(spec)) == as_text(expected)
    in_scenario = RunSpec(DEV.model_copy(update={"faults": plan}), "G2", Contract.C2, 11)
    assert as_text(run_plan(in_scenario)) == as_text(run_plan(spec))
    for seed in range(40):
        for entry in run_plan(RunSpec(DEV, "G2", Contract.C2, seed, plan=plan)):
            if entry.fault is FaultId.F4:
                assert 5 <= entry.param("copy_delay_ms") <= 50


def _after_draw(plan: Mapping[str, Any], seed: int) -> Generator:
    """The run's `faults` generator, past the draws the random plan takes."""
    generator = Generators(seed).get("faults")
    draw_plan(parse_random_plan(plan), generator, scenario=DEV, config="G2", contract=Contract.C2)
    return generator


def test_a_fixed_plan_is_checked_and_seeded_as_before() -> None:
    """A list draws only its seeded parameters, from a fresh `faults` generator, as runs
    always did, so the digests of the demos and the cells stay the same."""
    plan = [
        {"fault": "F4", "at": {"point": "net.gw_sim.request", "match": {"api": "x"}}},
        {"fault": "F2", "at": {"point": "net.gw_sim.answer"}},
    ]
    spec = RunSpec(DEV, "G2", Contract.C2, 7, plan=plan)
    before = seed_parameters(parse_plan(plan), Generators(7).get("faults"))
    assert as_text(run_plan(spec)) == as_text(before)
    with pytest.raises(ValueError, match=r"F2 at net\.gw_sim\.answer"):
        run_plan(RunSpec(DEV, "B0", Contract.C2, 7, plan=plan))
