import { describe as group, expect, it } from "vitest";

import { describe, describeLedger } from "./describe";
import { b0Ledger, b0Timeline, g2Timeline } from "./test/fixtures";
import { ledgerEntries } from "./views";

group("plain words for events", () => {
  it("says what a baseline request asked and where it went", () => {
    const text = describe(b0Timeline[0]!);
    expect(text.title).toBe("agent-1 → simulator · submit_purchase_order");
    expect(text.detail).toBe(
      "intent_id REQ-1, supplier_id S-1, sku W-100, quantity 100, approval_id APR-0001; delivered at 5312",
    );
  });

  it("shows a dropped answer as dropped, with why", () => {
    const text = describe(b0Timeline[2]!);
    expect(text.title).toBe("simulator → agent-1 · submit_purchase_order answer: committed");
    expect(text.detail).toBe("answer committed, effect_id PO-0001; dropped: fault");
    expect(text.tone).toBe("dropped");
  });

  it("reads a gateway tool's result from MCP's structured content", () => {
    expect(describe(g2Timeline[5]!).detail).toBe(
      "state confirmed, reason committed, effect_id PO-0001, action_id A-0001; delivered at 14596",
    );
    expect(describe(g2Timeline[1]!).detail).toBe("intent_id REQ-1, sku W-100; delivered at 4484");
  });

  it("names faults, the gateway's transitions and reports", () => {
    expect(describe(b0Timeline[1]!)).toMatchObject({
      title: "F11 at agent.after_tool_call: kill agent-1",
      detail: "message #17; restart_delay_ms 10000",
      tone: "fault",
    });
    expect(describe(g2Timeline[3]!)).toMatchObject({
      title: "A-0001 authorized → dispatched (dispatched)",
      tone: "gateway",
    });
    expect(describe(b0Timeline[4]!).title).toBe("agent-1 final report: REQ-1 done PO-0002");
  });

  it("names ledger entries and the fields a fact changed", () => {
    const [effect, fact] = ledgerEntries(b0Ledger);
    expect(describeLedger(effect!).title).toBe("PO-0001 committed for REQ-1");
    expect(describeLedger(fact!)).toMatchObject({
      title: "budget/CC-OPS v2 by PO-0001",
      detail: "committed_minor 20000 → 74000",
    });
  });
});
