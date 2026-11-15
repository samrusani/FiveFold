"""The config hash each run's result records (0004, Metrics).

It is the SHA-256 of the run config as `harness.run` records it: the configuration, the
contract level, the topology and the params, which hold the values the run used, its
fault plan and its number of agent workers. The scenario's digest, the seed, the model,
the cassette's digest and the code version are recorded on their own, so the hash leaves
them out.
"""

from __future__ import annotations

import dataclasses
import hashlib
from pathlib import Path

from pydantic import JsonValue

from fivefold.domain.contracts import Contract, ContractParams
from fivefold.domain.scenario import load_scenario_file, scenario_digest
from fivefold.evaluator import (
    Config,
    Destination,
    FactVersion,
    RunInfo,
    RunRecords,
    config_hash,
    evaluate,
)

ROOT = Path(__file__).resolve().parents[2]
SCENARIO = load_scenario_file(ROOT / "scenarios" / "dev" / "dev-001-single-po.yaml")
LOST_REPLY: JsonValue = {
    "fault": "F2",
    "variant": None,
    "at": {"point": "net.gw_sim.answer", "match": {"api": "submit_purchase_order"}},
    "params": {},
}
RUN = RunInfo(
    run_id="dev-001-single-po.G2.C2.7",
    scenario_id=SCENARIO.id,
    scenario_digest=scenario_digest(SCENARIO),
    config=Config.G2,
    contract=Contract.C2,
    seed=7,
    topology="inproc",
    model_adapter="scripted",
    model="scripted",
    params={"params": {"think_ms": 1000}, "plan": [LOST_REPLY], "workers": 1, "agent_workers": 1},
)


def hashed(run: RunInfo) -> str:
    return config_hash(
        config=str(run.config), contract=str(run.contract), topology=run.topology, params=run.params
    )


def test_the_config_hash_is_the_sha256_of_the_run_config_in_canonical_json() -> None:
    canonical = (
        '{"config":"G2","contract":"C2","params":{"agent_workers":1,"params":{"think_ms":1000},'
        '"plan":[{"at":{"match":{"api":"submit_purchase_order"},"point":"net.gw_sim.answer"},'
        '"fault":"F2","params":{},"variant":null}],"workers":1},"topology":"inproc"}'
    )
    assert hashed(RUN) == hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_the_config_hash_changes_with_the_run_config() -> None:
    timing: JsonValue = {"think_ms": 1000}
    other_plan: JsonValue = {"params": timing, "plan": [], "workers": 1, "agent_workers": 1}
    two: JsonValue = {"params": timing, "plan": [LOST_REPLY], "workers": 1, "agent_workers": 2}
    slower: JsonValue = {
        "params": {"think_ms": 2000},
        "plan": [LOST_REPLY],
        "workers": 1,
        "agent_workers": 1,
    }
    changed = [
        dataclasses.replace(RUN, config=Config.B0),
        dataclasses.replace(RUN, contract=Contract.C3),
        dataclasses.replace(RUN, topology="split"),
        dataclasses.replace(RUN, params=other_plan),
        dataclasses.replace(RUN, params=two),
        dataclasses.replace(RUN, params=slower),
    ]
    hashes = {hashed(run) for run in [RUN, *changed]}
    assert len(hashes) == 1 + len(changed)


def test_the_config_hash_leaves_out_what_the_result_records_on_its_own() -> None:
    same = [
        dataclasses.replace(RUN, run_id="another"),
        dataclasses.replace(RUN, scenario_digest="ab" * 32),
        dataclasses.replace(RUN, seed=8),
        dataclasses.replace(RUN, model_adapter="replay", model="model-x"),
        dataclasses.replace(RUN, cassette_digest="cd" * 32),
        dataclasses.replace(RUN, parent_run_id="dev-001-single-po.G2.C2.7", branch_point=12),
        dataclasses.replace(RUN, stopped_vt=58_200),
    ]
    assert {hashed(run) for run in same} == {hashed(RUN)}


def test_every_result_records_the_config_hash_of_its_run() -> None:
    records = RunRecords(
        run=RUN,
        scenario=SCENARIO,
        destination=Destination(
            scenario_id=SCENARIO.id,
            task_id=SCENARIO.task_id,
            capabilities=frozenset(),
            params=ContractParams(
                max_hop_latency_ms=50, commit_deadline_ms=10_000, status_lag_ms=0
            ),
        ),
        fact_versions=tuple(
            FactVersion(
                key=key,
                version=1,
                value=fact.model_dump(mode="json"),
                valid_from_seq=1,
                valid_from_vt=0,
                cause="initial",
            )
            for key, fact in SCENARIO.facts.items()
        ),
    )
    assert evaluate(records).run.config_hash == hashed(RUN)
