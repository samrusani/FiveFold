import { describe, expect, it } from "vitest";

import { b0Ledger, b0Timeline, g2Timeline, listedRun } from "./test/fixtures";
import {
  agentView,
  alignByVt,
  experiment,
  filterByHops,
  hopsOf,
  ledgerEntries,
  overdrawn,
  pairs,
} from "./views";

describe("the hop filter", () => {
  it("lists a run's hops in a fixed order", () => {
    expect(hopsOf(g2Timeline)).toEqual(["agent_gw", "gw_sim"]);
    expect(hopsOf(b0Timeline)).toEqual(["agent_sim"]);
  });

  it("hides messages on other hops and keeps every other event", () => {
    const kept = filterByHops(g2Timeline, new Set(["gw_sim"]));
    expect(kept.map((e) => `${e.source}#${e.seq}`)).toEqual([
      "harness#63",
      "gateway#4",
      "harness#66",
    ]);
  });
});

describe("the agent's view", () => {
  it("holds what the agent sent and was sent, the faults on it, and its report", () => {
    expect(agentView(b0Timeline).map((e) => e.seq)).toEqual([17, 18, 20, 21, 28]);
  });

  it("leaves out the gateway's traffic, its faults, its events and MCP handshakes", () => {
    expect(agentView(g2Timeline).map((e) => `${e.kind}#${e.seq}`)).toEqual(["message#35", "message#95"]);
    expect(agentView(g2Timeline, { handshakes: true }).map((e) => e.seq)).toEqual([1, 35, 95]);
  });
});

describe("the ledger in time", () => {
  it("has each effect and each fact change with the fields that changed", () => {
    const entries = ledgerEntries(b0Ledger);
    expect(entries.map((e) => [e.kind, e.vt, e.seq])).toEqual([
      ["effect", 5311, 8],
      ["fact", 5311, 8],
      ["effect", 15297, 10],
      ["fact", 15297, 10],
    ]);
    const last = entries[3];
    expect(last?.kind === "fact" ? last.changes : null).toEqual([
      { field: "committed_minor", from: 74000, to: 128000 },
    ]);
  });

  it("marks a budget committed above its limit", () => {
    expect(b0Ledger.facts.map(overdrawn)).toEqual([false, false, true]);
  });
});

describe("alignment by virtual time", () => {
  it("makes one row per virtual time with each column's items", () => {
    const rows = alignByVt([
      { key: "left", items: [{ vt: 1 }, { vt: 5 }, { vt: 5 }], vt: (i) => (i as { vt: number }).vt },
      { key: "right", items: [{ vt: 3 }, { vt: 5 }], vt: (i) => (i as { vt: number }).vt },
    ]);
    expect(rows.map((row) => [row.vt, row.cells.map((cell) => cell.length)])).toEqual([
      [1, [1, 0]],
      [3, [0, 1]],
      [5, [2, 1]],
    ]);
  });
});

describe("pairs of runs a demo shows side by side", () => {
  it("pairs B0 with the G2 run named after it", () => {
    const runs = [
      listedRun("demo_2a_b0", "B0"),
      listedRun("demo_2a_g2", "G2"),
      listedRun("demo_2b_g2", "G2"),
    ];
    expect(pairs(runs).map((p) => [p.b0.database, p.g2.database])).toEqual([["demo_2a_b0", "demo_2a_g2"]]);
  });

  it("pairs every fitting G2 run when none is named after it", () => {
    const runs = [listedRun("study_0001", "B0"), listedRun("study_0002", "G2"), listedRun("x", "G1")];
    expect(pairs(runs).map((p) => p.g2.database)).toEqual(["study_0002"]);
    expect(pairs([listedRun("a_b0", "B0"), { ...listedRun("a_g2", "G2"), seed: "8" }])).toEqual([]);
  });

  it("never pairs runs whose seeds differ only past 2**53, where JavaScript numbers round", () => {
    const b0 = { ...listedRun("study_0001", "B0"), seed: "9223372036854775806" };
    const g2 = { ...listedRun("study_0002", "G2"), seed: "9223372036854775807" };
    expect(Number(b0.seed)).toBe(Number(g2.seed));
    expect(pairs([b0, g2])).toEqual([]);
    expect(pairs([b0, { ...g2, seed: b0.seed }]).map((p) => p.g2.database)).toEqual(["study_0002"]);
  });
});

describe("an experimental branch (record 0009)", () => {
  const parent = { run_id: "dev-001-single-po.G2.C2.7", branch_point: 4 };

  it("is labelled a new experiment of its parent at its branch point", () => {
    expect(experiment(listedRun("demo_1_g2_branch_4", "G2", parent))).toBe(
      "A new experiment: a branch of dev-001-single-po.G2.C2.7 at branch point 4",
    );
    expect(experiment(listedRun("demo_1_g2", "G2"))).toBeNull();
  });

  it("is never paired with B0, though it has its parent's scenario, contract and seed", () => {
    const branch = listedRun("study_0003", "G2", parent);
    const runs = [listedRun("study_0001", "B0"), listedRun("study_0002", "G2"), branch];
    expect(pairs(runs).map((p) => p.g2.database)).toEqual(["study_0002"]);
    expect(pairs([listedRun("study_0001", "B0"), branch])).toEqual([]);
  });
});
