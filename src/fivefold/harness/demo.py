"""The demos of record 0006, run from a seed: `python -m fivefold.harness.demo` (`make demo`).

Each demo runs B0 and G2 on the same scenario and seed and shows them side by side
(0006, Demos):

1. "Order placed, reply lost" (dev-001, C2): F2 drops the purchase order's committed
   answer, and F11 kills the agent right after it sends that call, resuming it from its
   checkpoint 10,000 virtual ms later.
2. "Approval goes stale" (dev-002, C2): a human approves the order, then, before the
   gateway sends it, F7 raises the quote's price (2a) or F6 revokes the approval (2b).
   B0 meets the fault as the approval reaches the agent. dev-002 is dev-001 with the
   auto-approval threshold below the order's amount, so the approval is a human's.
3. "Outcome unknown" (dev-001, C0): F2 drops the order's committed answer, and no
   resolver attests what happened.

Every run gets its own database, recreated from scratch and migrated, as role `ff_*`
connections need (0008). The admin connection comes from `FIVEFOLD_ADMIN_DSN`, as for
the tests. A demo needs no API key: the model is scripted (0009).

The demo reads the gateway's records only to show what the gateway did and told the
agent; the verdict comes from the evaluator, which never reads them to decide (0004).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import psycopg

from fivefold.domain.contracts import Contract
from fivefold.domain.scenario import Scenario, load_scenario_file
from fivefold.evaluator import RunResult as Evaluation
from fivefold.harness.migrations import (
    DEFAULT_ADMIN_DSN,
    bootstrap,
    migrate,
    recreate_database,
)
from fivefold.harness.run import Config, InprocRun, RunSpec

ROOT = Path(__file__).resolve().parents[3]
DEV_001 = ROOT / "scenarios" / "dev" / "dev-001-single-po.yaml"
DEV_002 = ROOT / "scenarios" / "dev" / "dev-002-human-approval.yaml"
DB_DIR = ROOT / "db"

ORDER = "submit_purchase_order"


def _lost_reply(config: Config) -> dict[str, Any]:
    """F2 on the purchase order's committed answer, on the stack's hop to the simulator."""
    hop = "gw_sim" if config == "G2" else "agent_sim"
    return {
        "fault": "F2",
        "at": {
            "point": f"net.{hop}.answer",
            "match": {"api": ORDER, "answer": "committed"},
            "occurrence": 1,
        },
    }


def _before_the_send(config: Config) -> tuple[str, dict[str, str]]:
    """Where demo 2's faults fire: at the gateway's dispatch check in G2, and in B0 as the
    approval answer reaches the agent (0006, F6 and F7 early)."""
    if config == "G2":
        return "gw.dispatch.before_check", {}
    return "net.agent_sim.answer", {"api": "get_approval_status"}


def demo_1_plan(config: Config) -> list[dict[str, Any]]:
    """Demo 1's faults (0006): F2 on the order's answer, F11 after the order call."""
    return [
        _lost_reply(config),
        {
            "fault": "F11",
            "at": {
                "point": "agent.after_tool_call",
                "match": {"tool": ORDER},
                "occurrence": 1,
            },
            "params": {"restart_delay_ms": 10_000},
        },
    ]


def demo_2a_plan(config: Config) -> list[dict[str, Any]]:
    """Demo 2a's fault (0006): F7 early, `price_up`."""
    point, match = _before_the_send(config)
    return [
        {
            "fault": "F7",
            "variant": "price_up",
            "at": {"point": point, "match": match, "occurrence": 1},
        }
    ]


def demo_2b_plan(config: Config) -> list[dict[str, Any]]:
    """Demo 2b's fault (0006): F6 early."""
    point, match = _before_the_send(config)
    return [
        {
            "fault": "F6",
            "variant": "early",
            "at": {"point": point, "match": match, "occurrence": 1},
        }
    ]


def demo_3_plan(config: Config) -> list[dict[str, Any]]:
    """Demo 3's fault (0006): F2, with no resolver."""
    return [_lost_reply(config)]


@dataclass(frozen=True)
class Demo:
    """One demo of record 0006: what it shows, and how each configuration runs it."""

    key: str
    title: str
    scenario: Path
    contract: Contract
    faults: str
    plan: Callable[[Config], list[dict[str, Any]]]

    def spec(self, config: Config, seed: int, scenario: Scenario | None = None) -> RunSpec:
        """The demo's run in one configuration."""
        loaded = scenario if scenario is not None else load_scenario_file(self.scenario)
        return RunSpec(loaded, config, self.contract, seed, plan=self.plan(config))


DEMOS: tuple[Demo, ...] = (
    Demo(
        "1",
        "Order placed, reply lost",
        DEV_001,
        Contract.C2,
        "F2 drops the order's committed answer; F11 kills the agent after the order call.",
        demo_1_plan,
    ),
    Demo(
        "2a",
        "Approval goes stale: quote",
        DEV_002,
        Contract.C2,
        "A human approves the order; then F7 raises the quote's price before it is sent.",
        demo_2a_plan,
    ),
    Demo(
        "2b",
        "Approval goes stale: revoked",
        DEV_002,
        Contract.C2,
        "A human approves the order; then F6 revokes the approval before it is sent.",
        demo_2b_plan,
    ),
    Demo(
        "3",
        "Outcome unknown",
        DEV_001,
        Contract.C0,
        "F2 drops the order's committed answer; C0 offers no lookup, and no resolver attests.",
        demo_3_plan,
    ),
)
"""The demos of record 0006, in order."""

DEMO_BY_KEY = {demo.key: demo for demo in DEMOS}


def demo_1_spec(config: Config, seed: int, scenario: Scenario | None = None) -> RunSpec:
    """Demo 1 in one configuration."""
    return DEMO_BY_KEY["1"].spec(config, seed, scenario)


@dataclass(frozen=True)
class Summary:
    """What a run did, read back from its database."""

    config: str
    effects: list[tuple[str, str]]
    budget: dict[str, int]
    claims: list[dict[str, Any]]
    faults: list[tuple[str, str, int]]
    messages: int
    digest: str
    ended: str
    verdict: str = ""
    gateway: tuple[str, ...] = ()
    """What the gateway did with each action, and what it told the agent to reconcile."""


def summarize(dsn: str, config: str, ended: str) -> Summary:
    """Read a finished run's ledger, report, faults and message log."""
    with psycopg.connect(dsn) as conn:
        effects = [
            (str(effect_id), str(approval_id))
            for effect_id, approval_id in conn.execute(
                "SELECT effect_id, approval_id FROM sim.effect ORDER BY commit_seq"
            )
        ]
        budget_row = conn.execute(
            "SELECT value FROM sim.fact_version WHERE key = 'budget/CC-OPS'"
            " ORDER BY version DESC LIMIT 1"
        ).fetchone()
        report = conn.execute(
            "SELECT report FROM harness.final_report WHERE valid ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        faults = [
            (str(fault), str(point), int(vt))
            for fault, point, vt in conn.execute(
                "SELECT fault, point, vt FROM harness.fault_log ORDER BY seq"
            )
        ]
        log = conn.execute(
            "SELECT seq, hop, kind, sender, receiver, api, correlation, intent_id,"
            " idempotency_key, answer_class, body, sent_vt, fate, drop_reason,"
            " delivered_seq, delivered_vt FROM harness.message_log ORDER BY seq"
        ).fetchall()
        gateway = _gateway_lines(conn) if config != "B0" else ()
    text = json.dumps([list(row) for row in log], sort_keys=True, default=str)
    return Summary(
        config=config,
        effects=effects,
        budget=dict(budget_row[0]) if budget_row else {},
        claims=list(report[0]["claims"]) if report else [],
        faults=faults,
        messages=len(log),
        digest=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        ended=ended,
        gateway=gateway,
    )


def _gateway_lines(conn: psycopg.Connection[Any]) -> tuple[str, ...]:
    """One line per action: its state, why, the stale facts it named, and its approval."""
    lines: list[str] = []
    actions = conn.execute(
        "SELECT a.action_id, a.state, g.approval_id FROM gw.action a"
        ' LEFT JOIN gw."grant" g USING (action_id) ORDER BY a.action_id'
    ).fetchall()
    for action_id, state, approval_id in actions:
        event = conn.execute(
            "SELECT reason, details FROM gw.action_event WHERE action_id = %s"
            " AND to_state IS NOT NULL ORDER BY seq DESC LIMIT 1",
            (action_id,),
        ).fetchone()
        reason, details = (str(event[0]), event[1]) if event else ("", None)
        line = f"{action_id} {state} ({reason}), approval {approval_id}"
        stale = details.get("stale") if isinstance(details, dict) else None
        for fact in stale or ():
            changed = ", ".join(fact.get("changed_fields") or ())
            line += (
                f"; stale: {fact['key']} version {fact['cited_version']} to"
                f" {fact['current_version']} ({changed})"
            )
        lines.append(line)
    told = conn.execute(
        "SELECT result->'reconcile' FROM gw.tool_call"
        " WHERE result->'reconcile' IS NOT NULL AND result->'reconcile' <> 'null'::jsonb"
        " ORDER BY call_no DESC LIMIT 1"
    ).fetchone()
    if told is not None:
        reconcile = told[0]
        lines.append(
            f"told the agent to reconcile: {reconcile['how']} ({reconcile['escalation_id']}):"
            f" {reconcile['what']}"
        )
    return tuple(lines)


def run_demo(admin_dsn: str, demo: Demo, config: Config, seed: int) -> Summary:
    """Run one demo in one configuration, in a fresh database, and summarize it."""
    dsn = recreate_database(admin_dsn, f"fivefold_demo_{demo.key}_{config.lower()}")
    with psycopg.connect(dsn, autocommit=True) as conn:
        migrate(conn, DB_DIR / "migrations")
    run = InprocRun(demo.spec(config, seed), dsn)
    result = asyncio.run(run.execute())
    ended = ", ".join(f"{worker}: {end[0]}" for worker, end in sorted(result.ends.items()))
    summary = summarize(dsn, config, ended)
    return replace(summary, verdict=verdict_line(result.evaluation))


def run_demo_1(admin_dsn: str, config: Config, seed: int) -> Summary:
    """Run demo 1 in a fresh database and summarize it."""
    return run_demo(admin_dsn, DEMO_BY_KEY["1"], config, seed)


def verdict_line(evaluation: Evaluation | None) -> str:
    """The verdict and each failing property's reasons, for the properties evaluated."""
    if evaluation is None:
        return "not evaluated"
    parts = [str(evaluation.verdict)]
    for result in evaluation.properties:
        if str(result.verdict) == "fail":
            parts.append(f"property {result.property} fails ({', '.join(result.reason_codes)})")
    evaluated = ", ".join(str(number) for number in evaluation.evaluated)
    return "; ".join(parts) + f"  [properties evaluated: {evaluated}]"


def render(summary: Summary) -> str:
    """A few lines a person can read."""
    budget = summary.budget
    left = budget.get("limit_minor", 0) - budget.get("committed_minor", 0)
    lines = [
        f"{summary.config}:",
        f"  orders in the ledger: {len(summary.effects)}"
        + "".join(
            f"{', ' if i else ' '}{e} (approval {a})" for i, (e, a) in enumerate(summary.effects)
        ),
        f"  budget left: {left / 100:.2f} USD" + ("  OVERDRAWN" if left < 0 else ""),
        "  report: "
        + "; ".join(
            f"{c['intent_id']} {c['status']} {c.get('effect_ids') or ''}".strip()
            for c in summary.claims
        ),
        "  faults: " + ", ".join(f"{f} at {p} (vt {vt})" for f, p, vt in summary.faults),
        *(f"  gateway: {line}" for line in summary.gateway),
        f"  agent: {summary.ended}",
        f"  verdict: {summary.verdict}",
        f"  message log: {summary.messages} messages, sha256 {summary.digest[:16]}",
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the chosen demos for B0 and G2 and print them side by side."""
    parser = argparse.ArgumentParser(
        prog="python -m fivefold.harness.demo",
        description="Run the demos of record 0006 for B0 and G2 from a seed.",
    )
    parser.add_argument(
        "demos",
        nargs="*",
        metavar="DEMO",
        help=f"which demos to run: {', '.join(DEMO_BY_KEY)} (default: all)",
    )
    parser.add_argument("--seed", type=int, default=7, help="the run seed (default 7)")
    parser.add_argument(
        "--dsn",
        default=os.environ.get("FIVEFOLD_ADMIN_DSN", DEFAULT_ADMIN_DSN),
        help="admin connection string (default: FIVEFOLD_ADMIN_DSN)",
    )
    args = parser.parse_args(argv)
    unknown = [key for key in args.demos if key not in DEMO_BY_KEY]
    if unknown:
        parser.error(f"no demo {', '.join(unknown)}; choose from {', '.join(DEMO_BY_KEY)}")
    chosen = [DEMO_BY_KEY[key] for key in args.demos] or list(DEMOS)
    with psycopg.connect(args.dsn, autocommit=True) as conn:
        bootstrap(conn, DB_DIR / "bootstrap.sql")
    for demo in chosen:
        scenario = load_scenario_file(demo.scenario)
        print(f"Demo {demo.key}: {demo.title} ({scenario.id}, {demo.contract}, seed {args.seed})")
        print(f"{demo.faults}\n")
        for config in ("B0", "G2"):
            print(render(run_demo(args.dsn, demo, config, args.seed)))
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
