"""The demos of record 0006, run from a seed: `python -m fivefold.harness.demo` (`make demo`).

Each demo runs B0 and G2 on the same scenario and seed and shows them side by side
(0006, Demos). This build has demo 1, "Order placed, reply lost", at C2: F2 drops the
purchase order's committed answer, and F11 kills the agent right after it sends that
call, resuming it from its checkpoint 10,000 virtual ms later.

Every run gets its own database, recreated from scratch and migrated, as role `ff_*`
connections need (0008). The admin connection comes from `FIVEFOLD_ADMIN_DSN`, as for
the tests. A demo needs no API key: the model is scripted (0009).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg

from fivefold.domain.contracts import Contract
from fivefold.domain.scenario import Scenario, load_scenario_file
from fivefold.harness.migrations import (
    DEFAULT_ADMIN_DSN,
    bootstrap,
    migrate,
    recreate_database,
)
from fivefold.harness.run import Config, InprocRun, RunSpec

ROOT = Path(__file__).resolve().parents[3]
DEV_001 = ROOT / "scenarios" / "dev" / "dev-001-single-po.yaml"
DB_DIR = ROOT / "db"


def demo_1_plan(config: Config) -> list[dict[str, Any]]:
    """Demo 1's faults (0006): F2 on the order's answer, F11 after the order call."""
    hop = "gw_sim" if config == "G2" else "agent_sim"
    return [
        {
            "fault": "F2",
            "at": {
                "point": f"net.{hop}.answer",
                "match": {"api": "submit_purchase_order", "answer": "committed"},
                "occurrence": 1,
            },
        },
        {
            "fault": "F11",
            "at": {
                "point": "agent.after_tool_call",
                "match": {"tool": "submit_purchase_order"},
                "occurrence": 1,
            },
            "params": {"restart_delay_ms": 10_000},
        },
    ]


def demo_1_spec(config: Config, seed: int, scenario: Scenario | None = None) -> RunSpec:
    """Demo 1 in one configuration."""
    loaded = scenario if scenario is not None else load_scenario_file(DEV_001)
    return RunSpec(loaded, config, Contract.C2, seed, plan=demo_1_plan(config))


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
    )


def run_demo_1(admin_dsn: str, config: Config, seed: int) -> Summary:
    """Run demo 1 in a fresh database and summarize it."""
    dsn = recreate_database(admin_dsn, f"fivefold_demo_1_{config.lower()}")
    with psycopg.connect(dsn, autocommit=True) as conn:
        migrate(conn, DB_DIR / "migrations")
    run = InprocRun(demo_1_spec(config, seed), dsn)
    result = asyncio.run(run.execute())
    ended = ", ".join(f"{worker}: {end[0]}" for worker, end in sorted(result.ends.items()))
    return summarize(dsn, config, ended)


def render(summary: Summary) -> str:
    """A few lines a person can read."""
    budget = summary.budget
    left = budget.get("limit_minor", 0) - budget.get("committed_minor", 0)
    lines = [
        f"{summary.config}:",
        f"  orders in the ledger: {len(summary.effects)} "
        + ", ".join(f"{e} (approval {a})" for e, a in summary.effects),
        f"  budget left: {left / 100:.2f} USD" + ("  OVERDRAWN" if left < 0 else ""),
        "  report: "
        + "; ".join(
            f"{c['intent_id']} {c['status']} {c.get('effect_ids') or ''}".strip()
            for c in summary.claims
        ),
        "  faults: " + ", ".join(f"{f} at {p} (vt {vt})" for f, p, vt in summary.faults),
        f"  agent: {summary.ended}",
        f"  message log: {summary.messages} messages, sha256 {summary.digest[:16]}",
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """Run demo 1 for B0 and G2 and print them side by side."""
    parser = argparse.ArgumentParser(
        prog="python -m fivefold.harness.demo",
        description="Run demo 1, 'Order placed, reply lost', for B0 and G2 from a seed.",
    )
    parser.add_argument("--seed", type=int, default=7, help="the run seed (default 7)")
    parser.add_argument(
        "--dsn",
        default=os.environ.get("FIVEFOLD_ADMIN_DSN", DEFAULT_ADMIN_DSN),
        help="admin connection string (default: FIVEFOLD_ADMIN_DSN)",
    )
    args = parser.parse_args(argv)
    with psycopg.connect(args.dsn, autocommit=True) as conn:
        bootstrap(conn, DB_DIR / "bootstrap.sql")
    print(f"Demo 1: order placed, reply lost (dev-001, C2, seed {args.seed})")
    print("F2 drops the order's committed answer; F11 kills the agent after the order call.\n")
    for config in ("B0", "G2"):
        print(render(run_demo_1(args.dsn, config, args.seed)))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
