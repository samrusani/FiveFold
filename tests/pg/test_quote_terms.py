"""Record 0025 scores altered orders without modifying the fault matrices."""

import asyncio
from collections.abc import Callable, Sequence

import pytest

from fivefold.agents.policy import Mode, Policy, ToolCall, scripted_policy
from fivefold.domain.contracts import Contract
from fivefold.domain.scenario import Intent, load_scenario_file
from fivefold.evaluator import PropertyVerdict, Verdict
from fivefold.harness import run as inproc
from fivefold.harness.demo import DEV_001
from fivefold.harness.run import Config, InprocRun, RunSpec

pytestmark = pytest.mark.pg


@pytest.mark.parametrize("config", ["B0", "G2"])
@pytest.mark.parametrize("delta", [-1, 1], ids=["cuts_price", "overpays"])
def test_order_terms_are_scored_against_the_cited_quote(
    run_database: Callable[[str], str],
    monkeypatch: pytest.MonkeyPatch,
    config: Config,
    delta: int,
) -> None:
    def alter_price(policy: Policy) -> Policy:
        call = next(policy)
        while True:
            args = dict(call.args)
            if call.name == "request_approval":
                scope = args["scope"]
                assert isinstance(scope, dict)
                args["scope"] = {
                    **scope,
                    "max_amount_minor": scope["max_amount_minor"] + delta * scope["max_quantity"],
                }
            elif call.name == "submit_purchase_order":
                price = args["unit_price_minor"]
                assert isinstance(price, int)
                args["unit_price_minor"] = price + delta
            reply = yield ToolCall(call.name, args)
            try:
                call = policy.send(reply)
            except StopIteration:
                return

    def script(
        intents: Sequence[Intent],
        *,
        mode: Mode,
        rank: int = 0,
        max_wait_ms: int = 60_000,
        memory_enabled: bool = False,
    ) -> Policy:
        return alter_price(
            scripted_policy(
                intents,
                mode=mode,
                rank=rank,
                max_wait_ms=max_wait_ms,
                memory_enabled=memory_enabled,
            )
        )

    monkeypatch.setattr(inproc, "scripted_policy", script)
    spec = RunSpec(load_scenario_file(DEV_001), config, Contract.C2, 7, plan=[])
    run = InprocRun(spec, run_database("altered_quote_terms"))
    outcome = asyncio.run(run.execute()).evaluation

    assert outcome is not None
    assert outcome.verdict is Verdict.FAIL
    assert [p.property for p in outcome.properties if p.verdict is PropertyVerdict.FAIL] == [3]
    assert outcome.property_result(3).reason_codes == ("quote_terms_mismatch",)
    assert outcome.property_result(3).failures[0].effect_ids == ("PO-0001",)
