// Small runs in the API's shapes, after demo 1 (record 0006): B0 orders twice, G2 once.

import type {
  Ledger,
  ListedRun,
  Message,
  RegistryRun,
  RunSummary,
  Scorecard,
  Timeline,
  TimelineEvent,
} from "../types";

export function message(partial: Partial<Message> & Pick<Message, "seq" | "hop" | "sender" | "receiver">): Message {
  return {
    kind: "request",
    api: null,
    correlation: null,
    intent_id: null,
    idempotency_key: null,
    answer_class: null,
    body: {},
    sent_vt: 0,
    fate: "delivered",
    drop_reason: null,
    delivered_seq: partial.seq + 1,
    delivered_vt: (partial.sent_vt ?? 0) + 20,
    copy_of: null,
    ...partial,
  };
}

function messageEvent(m: Message, shares_vt = false): TimelineEvent {
  return { source: "harness", kind: "message", seq: m.seq, vt: m.sent_vt, shares_vt, message: m };
}

export const b0Order = message({
  seq: 17,
  hop: "agent_sim",
  sender: "agent-1",
  receiver: "simulator",
  api: "submit_purchase_order",
  intent_id: "REQ-1",
  body: { intent_id: "REQ-1", supplier_id: "S-1", sku: "W-100", quantity: 100, approval_id: "APR-0001" },
  sent_vt: 5292,
});

export const b0LostAnswer = message({
  seq: 20,
  hop: "agent_sim",
  kind: "answer",
  sender: "simulator",
  receiver: "agent-1",
  api: "submit_purchase_order",
  correlation: 17,
  intent_id: "REQ-1",
  answer_class: "committed",
  body: { answer: "committed", effect_id: "PO-0001", intent_id: "REQ-1" },
  sent_vt: 5311,
  fate: "dropped",
  drop_reason: "fault",
  delivered_seq: null,
  delivered_vt: null,
});

export const b0Timeline: TimelineEvent[] = [
  messageEvent(b0Order, true),
  {
    source: "harness",
    kind: "fault",
    seq: 18,
    vt: 5292,
    shares_vt: true,
    fault: {
      seq: 18,
      fault: "F11",
      variant: null,
      point: "agent.after_tool_call",
      occurrence: 1,
      action: "kill",
      target_seq: 17,
      component: "agent-1",
      voided: [],
      params: { restart_delay_ms: 10000 },
      vt: 5292,
    },
  },
  messageEvent(b0LostAnswer, true),
  {
    source: "harness",
    kind: "fault",
    seq: 21,
    vt: 5311,
    shares_vt: true,
    fault: {
      seq: 21,
      fault: "F2",
      variant: null,
      point: "net.agent_sim.answer",
      occurrence: 1,
      action: "drop",
      target_seq: 20,
      component: null,
      voided: [],
      params: {},
      vt: 5311,
    },
  },
  {
    source: "harness",
    kind: "report",
    seq: 28,
    vt: 16345,
    shares_vt: false,
    report: {
      seq: 28,
      worker: "agent-1",
      report: { claims: [{ intent_id: "REQ-1", status: "done", effect_ids: ["PO-0002"], note: "" }] },
      valid: true,
      errors: [],
      accepted_by: "sink",
      message_seq: 26,
      vt: 16345,
    },
  },
];

export const g2Timeline: TimelineEvent[] = [
  messageEvent(
    message({ seq: 1, hop: "agent_gw", sender: "agent-1", receiver: "gateway", api: "initialize", sent_vt: 0 }),
  ),
  messageEvent(
    message({
      seq: 35,
      hop: "agent_gw",
      sender: "agent-1",
      receiver: "gateway",
      api: "submit_purchase_order",
      intent_id: "REQ-1",
      body: { method: "tools/call", params: { name: "submit_purchase_order", arguments: { intent_id: "REQ-1", sku: "W-100" } } },
      sent_vt: 4464,
    }),
  ),
  messageEvent(
    message({
      seq: 63,
      hop: "gw_sim",
      sender: "gateway",
      receiver: "simulator",
      api: "submit_purchase_order",
      intent_id: "REQ-1",
      idempotency_key: "A-0001",
      sent_vt: 4785,
    }),
    true,
  ),
  {
    source: "gateway",
    kind: "action_event",
    seq: 4,
    vt: 4785,
    shares_vt: true,
    action_event: {
      seq: 4,
      action_id: "A-0001",
      kind: "transition",
      from_state: "authorized",
      to_state: "dispatched",
      reason: "dispatched",
      details: { attempt_no: 1 },
      vt: 4785,
    },
  },
  {
    source: "harness",
    kind: "fault",
    seq: 66,
    vt: 4823,
    shares_vt: false,
    fault: {
      seq: 66,
      fault: "F2",
      variant: null,
      point: "net.gw_sim.answer",
      occurrence: 1,
      action: "drop",
      target_seq: 65,
      component: null,
      voided: [],
      params: {},
      vt: 4823,
    },
  },
  messageEvent(
    message({
      seq: 95,
      hop: "agent_gw",
      kind: "answer",
      sender: "gateway",
      receiver: "agent-1",
      api: "submit_purchase_order",
      correlation: 93,
      answer_class: "action",
      body: {
        result: { structuredContent: { action_id: "A-0001", effect_id: "PO-0001", state: "confirmed", reason: "committed" } },
      },
      sent_vt: 14576,
    }),
  ),
];

export const b0Ledger: Ledger = {
  database: "fivefold_demo_1_b0",
  effects: [
    {
      effect_id: "PO-0001",
      intent_id: "REQ-1",
      operation: "submit_purchase_order",
      args: { supplier_id: "S-1", sku: "W-100", quantity: 100, unit_price_minor: 540 },
      approval_id: "APR-0001",
      basis: { "budget/CC-OPS": 1, "quote/S-1/W-100": 1 },
      idempotency_key: null,
      client_ref: null,
      received_seq: 7,
      commit_seq: 8,
      committed_vt: 5311,
    },
    {
      effect_id: "PO-0002",
      intent_id: "REQ-1",
      operation: "submit_purchase_order",
      args: { supplier_id: "S-1", sku: "W-100", quantity: 100, unit_price_minor: 540 },
      approval_id: "APR-0001",
      basis: { "budget/CC-OPS": 1, "quote/S-1/W-100": 1 },
      idempotency_key: null,
      client_ref: null,
      received_seq: 9,
      commit_seq: 10,
      committed_vt: 15297,
    },
  ],
  facts: [
    budget(1, 20000, 0, 1, "initial", null),
    budget(2, 74000, 5311, 8, "effect", "PO-0001"),
    budget(3, 128000, 15297, 10, "effect", "PO-0002"),
  ],
};

export const g2Ledger: Ledger = {
  database: "fivefold_demo_1_g2",
  effects: [{ ...b0Ledger.effects[0]!, idempotency_key: "A-0001", committed_vt: 4823, commit_seq: 13 }],
  facts: [budget(1, 20000, 0, 1, "initial", null), budget(2, 74000, 4823, 13, "effect", "PO-0001")],
};

function budget(
  version: number,
  committed: number,
  vt: number,
  seq: number,
  cause: "initial" | "effect",
  effect: string | null,
) {
  return {
    key: "budget/CC-OPS",
    version,
    value: { limit_minor: 100000, committed_minor: committed },
    valid_from_seq: seq,
    valid_from_vt: vt,
    cause,
    cause_effect_id: effect,
    cause_actor: null,
  };
}

export const b0Scorecard: Scorecard = {
  verdict: "fail",
  evaluated: [1, 2, 3, 4, 5],
  properties: [
    {
      property: 1,
      name: "exactly_once",
      verdict: "fail",
      reason_codes: ["duplicate_effects"],
      failures: [
        {
          code: "duplicate_effects",
          intent_id: "REQ-1",
          effect_ids: ["PO-0001", "PO-0002"],
          detail: "REQ-1 has 2 effects at the end, not at most 1",
        },
      ],
      metrics: { duplicates: 1 },
    },
    { property: 2, name: "authorized_at_commit", verdict: "pass", reason_codes: [], failures: [], metrics: {} },
    { property: 3, name: "inputs_still_fresh", verdict: "pass", reason_codes: [], failures: [], metrics: {} },
    { property: 4, name: "actually_committed", verdict: "pass", reason_codes: [], failures: [], metrics: {} },
    { property: 5, name: "truthfully_reported", verdict: "pass", reason_codes: [], failures: [], metrics: {} },
  ],
  excused_intents: [
    { intent_id: "REQ-2", excuse: "authority_withdrawn", approval_ids: ["APR-0002"], detail: "APR-0002 was revoked" },
  ],
  report: {
    seq: 28,
    worker: "agent-1",
    accepted_by: "sink",
    message_seq: 26,
    t_r: 27,
    claims: [
      {
        intent_id: "REQ-1",
        status: "done",
        claimed_effects: ["PO-0002"],
        knowledge: "committed",
        known_effects: ["PO-0002"],
        ledger_effects: ["PO-0001", "PO-0002"],
        passed: true,
        reason_code: null,
      },
    ],
  },
  metrics: { duplicates: 1 },
  code_version: "0.0.0",
};

export function registryRun(database: string, config: string): RegistryRun {
  return {
    database,
    run_id: `dev-001-single-po.${config}.C2.7`,
    scenario_id: "dev-001-single-po",
    config,
    contract: "C2",
    seed: 7,
    created_wall_at: "2026-11-15T09:59:32+00:00",
  };
}

/** A run as the run list shows it: no branch, unless `parent` names its parent. */
export function listedRun(
  database: string,
  config: string,
  parent?: { run_id: string; branch_point: number },
): ListedRun {
  const run = registryRun(database, config);
  return parent === undefined
    ? { ...run, parent_run_id: null, branch_point: null }
    : {
        ...run,
        run_id: `${parent.run_id}.branch-${parent.branch_point}`,
        parent_run_id: parent.run_id,
        branch_point: parent.branch_point,
      };
}

export function summary(database: string, config: string, card: Scorecard | null): RunSummary {
  return {
    database,
    registry: registryRun(database, config),
    run: {
      run_id: `dev-001-single-po.${config}.C2.7`,
      scenario_id: "dev-001-single-po",
      scenario_digest: "d",
      config,
      contract: "C2",
      seed: 7,
      topology: "inproc",
      model_adapter: "scripted",
      model: "scripted",
      cassette_digest: null,
      parent_run_id: null,
      branch_point: null,
      params: {},
      stopped_vt: 37463,
    },
    scorecard: card,
  };
}

export function timeline(database: string, events: TimelineEvent[]): Timeline {
  return { database, events };
}
