import { describe, expect, it } from "vitest";

import { b0Ledger, b0Timeline, g2Timeline, registryRun } from "./test/fixtures";
import { agentView, alignByVt, filterByHops, hopsOf, ledgerEntries, overdrawn, pairs } from "./views";

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
      registryRun("demo_2a_b0", "B0"),
      registryRun("demo_2a_g2", "G2"),
      registryRun("demo_2b_g2", "G2"),
    ];
    expect(pairs(runs).map((p) => [p.b0.database, p.g2.database])).toEqual([["demo_2a_b0", "demo_2a_g2"]]);
  });

  it("pairs every fitting G2 run when none is named after it", () => {
    const runs = [registryRun("study_0001", "B0"), registryRun("study_0002", "G2"), registryRun("x", "G1")];
    expect(pairs(runs).map((p) => p.g2.database)).toEqual(["study_0002"]);
    expect(pairs([registryRun("a_b0", "B0"), { ...registryRun("a_g2", "G2"), seed: 8 }])).toEqual([]);
  });
});
