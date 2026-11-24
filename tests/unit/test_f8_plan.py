"""F8 plan bounds and the compatibility of dev-001's seeded plans."""

from itertools import product

import pytest

from fivefold.domain.contracts import Contract
from fivefold.domain.scenario import load_scenario_file
from fivefold.faults.plan import FaultInjector, drawable_faults, parse_plan
from fivefold.harness.run import CONFIGS, RunSpec, run_plan
from fivefold.paths import DEV_001
from fivefold.recorder.cassette import canonical_json, sha256

EARLY = "net.agent_sim.answer"
F8 = {"fault": "F8", "variant": "early_relevant", "at": {"point": EARLY}}


def test_an_early_direct_f8_counts_only_approved_approval_replies() -> None:
    injector = FaultInjector(parse_plan([F8]))
    request = {"api": "request_approval"}
    assert not injector.at(EARLY, request, approval_status="pending")
    assert not injector.at(EARLY, {"api": "get_approval_status"}, approval_status="approved")
    (fired,) = injector.at(EARLY, request, approval_status="approved")
    assert fired.occurrence == 1
    assert not injector.at(EARLY, request, approval_status="approved")


def test_f8_needs_memory_and_fires_at_most_once() -> None:
    scenario = load_scenario_file(DEV_001)
    with pytest.raises(ValueError, match="F8 needs a memory-enabled scenario"):
        run_plan(RunSpec(scenario, "B0", Contract.C2, seed=7, plan=[F8]))
    with pytest.raises(ValueError, match="F8 fires at occurrence 1"):
        parse_plan([{**F8, "at": {"point": EARLY, "occurrence": 2}}])
    with pytest.raises(ValueError, match="a plan can contain only one F8"):
        parse_plan([F8, F8])


@pytest.mark.parametrize(
    ("seed", "digest"),
    [
        (1, "d30dc3cc57e80333ecb913bb2afb519b9e0b6810e2078643b225479bd019d574"),
        (7, "ce1263b41054a46bdb4697e228e1e1f9c78c88d3f4cfd6401f2c1d10c9718601"),
        (42, "c67958c9a4b087f96af85ced9173e7360ff8b3415aff7ce395e8fa4f99fedcc7"),
        (2026, "17839aff754f74cd67c502880d4cc775b963f2fac3d217e23d922e4321404680"),
    ],
)
def test_dev_001_seeded_plans_match_the_pre_f8_digests(seed: int, digest: str) -> None:
    scenario = load_scenario_file(DEV_001)
    plans = []
    for config, contract in product(CONFIGS, Contract):
        faults = drawable_faults(scenario=scenario, config=config, contract=contract)
        spec = RunSpec(
            scenario, config, contract, seed, plan={"random": {"from": list(faults), "max": 5}}
        )
        plans.append([fault.model_dump(mode="json") for fault in run_plan(spec)])
    assert sha256(canonical_json(plans)) == digest
