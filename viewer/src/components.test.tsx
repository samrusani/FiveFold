import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import { RunHeader } from "./components/RunPage";
import { Scorecard } from "./components/Scorecard";
import { Timeline } from "./components/Timeline";
import {
  b0Ledger,
  b0Scorecard,
  b0Timeline,
  g2Ledger,
  g2Timeline,
  listedRun,
  summary,
  timeline,
} from "./test/fixtures";
import type { RunList } from "./types";

const PARENT = { run_id: "dev-001-single-po.G2.C2.7", branch_point: 4 };

afterEach(() => {
  cleanup();
  window.location.hash = "";
});

describe("the timeline", () => {
  it("shows one row per event and marks events that share a virtual time", () => {
    render(<Timeline events={g2Timeline} />);
    expect(screen.getAllByTestId("timeline-row")).toHaveLength(6);
    expect(screen.getAllByLabelText("shares its virtual time")).toHaveLength(2);
  });

  it("filters messages by hop and keeps faults and gateway events", () => {
    render(<Timeline events={g2Timeline} />);
    fireEvent.click(screen.getByRole("checkbox", { name: "agent_gw" }));
    const kinds = screen.getAllByTestId("timeline-row").map((row) => row.dataset.kind);
    expect(kinds).toEqual(["message", "action_event", "fault"]);
    expect(screen.getByText("3 of 6 events")).toBeTruthy();
  });
});

describe("the scorecard", () => {
  it("shows each property's verdict, reason codes, failures, excused intents and claims", () => {
    render(<Scorecard card={b0Scorecard} />);
    const rows = screen.getAllByTestId("property-row");
    expect(rows).toHaveLength(5);
    expect(within(rows[0]!).getAllByText("duplicate_effects")).toHaveLength(2);
    expect(rows[0]!.textContent).toContain("REQ-1 has 2 effects at the end, not at most 1");
    expect(screen.getByTestId("excused-intent").textContent).toContain("authority_withdrawn");
    expect(screen.getByText("PO-0001 PO-0002")).toBeTruthy();
  });

  it("says when the run is not scored yet", () => {
    render(<Scorecard card={null} />);
    expect(screen.getByText("The evaluator has not scored this run yet.")).toBeTruthy();
  });
});

const responses: Record<string, unknown> = {
  "/api/runs": {
    registry: "fivefold_registry",
    runs: [
      listedRun("demo_1_g2_branch_4", "G2", PARENT),
      listedRun("demo_1_g2", "G2"),
      listedRun("demo_1_b0", "B0"),
    ],
  },
  "/api/runs/demo_1_b0": summary("demo_1_b0", "B0", b0Scorecard),
  "/api/runs/demo_1_b0/timeline": timeline("demo_1_b0", b0Timeline),
  "/api/runs/demo_1_b0/ledger": b0Ledger,
  "/api/runs/demo_1_g2": summary("demo_1_g2", "G2", null),
  "/api/runs/demo_1_g2/timeline": timeline("demo_1_g2", g2Timeline),
  "/api/runs/demo_1_g2/ledger": g2Ledger,
};

describe("the pages, over a stubbed API", () => {
  const requested: string[] = [];

  beforeEach(() => {
    requested.length = 0;
    vi.stubGlobal("fetch", async (path: string, init: RequestInit) => {
      requested.push(`${init.method} ${path}`);
      const body = responses[path];
      return body === undefined
        ? { ok: false, status: 404, json: async () => ({ error: "unknown_run", detail: `no ${path}` }) }
        : { ok: true, status: 200, json: async () => body };
    });
  });

  it("names itself FiveFold viewer in the top bar", async () => {
    render(<App />);
    expect(screen.getByRole("link", { name: "FiveFold viewer" })).toBeTruthy();
    expect(await screen.findAllByTestId("run-row")).toHaveLength(3);
  });

  it("lists the runs and offers B0 beside G2", async () => {
    render(<App />);
    expect(await screen.findAllByTestId("run-row")).toHaveLength(3);
    const pair = screen.getByRole("link", { name: "dev-001-single-po, C2, seed 7: B0 beside G2" });
    expect(pair.getAttribute("href")).toBe("#/compare/demo_1_b0/demo_1_g2");
    expect(requested).toEqual(["GET /api/runs"]);
  });

  it("labels a branch in the run list as a new experiment, and never pairs it", async () => {
    render(<App />);
    const rows = await screen.findAllByTestId("run-row");
    expect(rows.map((row) => row.querySelector(".experiment")?.textContent ?? null)).toEqual([
      "A new experiment: a branch of dev-001-single-po.G2.C2.7 at branch point 4.",
      null,
      null,
    ]);
    expect(screen.getAllByRole("link", { name: /B0 beside G2/ })).toHaveLength(1);
  });

  it("aligns the agent's view and the ledger of B0 and G2 by virtual time", async () => {
    window.location.hash = "#/compare/demo_1_b0/demo_1_g2";
    render(<App />);
    const rows = await screen.findAllByTestId("compare-row");
    expect(rows.map((row) => row.querySelector("td")?.textContent)).toEqual([
      "4464",
      "4823",
      "5292",
      "5311",
      "14576",
      "15297",
      "16345",
    ]);
    const at5311 = rows[3]!.querySelectorAll("td");
    expect(at5311[1]?.textContent).toContain("F2 at net.agent_sim.answer: drop");
    expect(at5311[2]?.textContent).toContain("PO-0001 committed for REQ-1");
    expect(rows[5]!.textContent).toContain("over the limit");
    expect(requested.every((line) => line.startsWith("GET "))).toBe(true);
  });

  it("says a run with no scorecard is not scored yet, not that every property holds", async () => {
    window.location.hash = "#/compare/demo_1_b0/demo_1_g2";
    render(<App />);
    await screen.findAllByTestId("compare-row");
    expect(screen.getByText("not scored yet")).toBeTruthy();
    expect(screen.queryByText("every property holds")).toBeNull();
  });

  it("names the API's reason when a run cannot be read", async () => {
    window.location.hash = "#/runs/gone";
    render(<App />);
    expect((await screen.findByRole("alert")).textContent).toBe("unknown_run: no /api/runs/gone");
  });
});

describe("the run page of an experimental branch (record 0009)", () => {
  it("says it is a new experiment of its parent at its branch point", () => {
    const plain = summary("demo_1_g2_branch_4", "G2", null);
    const branch = {
      ...plain,
      run: {
        ...plain.run,
        run_id: "dev-001-single-po.G2.C2.7.branch-4",
        parent_run_id: PARENT.run_id,
        branch_point: PARENT.branch_point,
      },
    };
    render(<RunHeader summary={branch} />);
    expect(screen.getByText(/^A new experiment/).textContent).toBe(
      "A new experiment: a branch of dev-001-single-po.G2.C2.7 at branch point 4. It replayed" +
        " its parent's first 4 model calls, then switched model.",
    );
  });

  it("says nothing of the kind for a run that is no branch", () => {
    render(<RunHeader summary={summary("demo_1_g2", "G2", null)} />);
    expect(screen.queryByText(/new experiment/)).toBeNull();
  });
});

describe("a seed past 2**53 - 1, which a JavaScript number cannot hold", () => {
  const b0 = { ...listedRun("study_0001", "B0"), seed: "9223372036854775806" };
  const g2 = { ...listedRun("study_0002", "G2"), seed: "9223372036854775807" };

  it("shows exactly in the run list, which pairs B0 only with the G2 run of its seed", async () => {
    const twin = { ...listedRun("study_0003", "G2"), seed: b0.seed };
    const list: RunList = { registry: "fivefold_registry", runs: [b0, g2, twin] };
    vi.stubGlobal("fetch", async () => ({ ok: true, status: 200, json: async () => list }));
    render(<App />);
    const rows = await screen.findAllByTestId("run-row");
    expect(rows.map((row) => row.querySelectorAll("td")[6]?.textContent)).toEqual([
      "9223372036854775806",
      "9223372036854775807",
      "9223372036854775806",
    ]);
    const pair = screen.getByRole("link", { name: /B0 beside G2/ });
    expect(pair.textContent).toBe("dev-001-single-po, C2, seed 9223372036854775806: B0 beside G2");
    expect(pair.getAttribute("href")).toBe("#/compare/study_0001/study_0003");
  });

  it("shows exactly on the run page", () => {
    const plain = summary("study_0002", "G2", null);
    render(<RunHeader summary={{ ...plain, run: { ...plain.run, seed: g2.seed } }} />);
    expect(screen.getByText(/, seed 9223372036854775807;/)).toBeTruthy();
  });
});
