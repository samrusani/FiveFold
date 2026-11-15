// Plain words for each timeline event, ledger entry and value.

import type { Json, Message, TimelineEvent } from "./types";
import type { LedgerEntry } from "./views";

const SUMMARY_FIELDS = [
  "answer",
  "result",
  "state",
  "status",
  "reason",
  "effect_id",
  "action_id",
  "approval_id",
  "read_id",
  "key",
  "version",
];
const ARG_FIELDS = [
  "intent_id",
  "supplier_id",
  "sku",
  "quantity",
  "unit_price_minor",
  "cost_center",
  "approval_id",
  "action_id",
  "wait_ms",
];

function isObject(value: Json | undefined): value is { [key: string]: Json } {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function scalar(value: Json | undefined): string {
  if (value === undefined) return "none";
  return typeof value === "string" ? value : JSON.stringify(value);
}

function fields(value: Json | undefined, names: string[]): string {
  if (!isObject(value)) return "";
  return names
    .filter((name) => value[name] !== undefined && value[name] !== null && !isObject(value[name]))
    .map((name) => `${name} ${scalar(value[name])}`)
    .join(", ");
}

/** A tool call's arguments: MCP's `params.arguments` in G modes, the body itself in B0. */
export function argumentsOf(message: Message): Json | undefined {
  const body = message.body;
  if (isObject(body) && isObject(body.params) && "arguments" in body.params) {
    return body.params.arguments;
  }
  return body;
}

/** A tool result: MCP's structured content in G modes, the body itself in B0. */
export function resultOf(message: Message): Json | undefined {
  const body = message.body;
  if (isObject(body) && isObject(body.result) && "structuredContent" in body.result) {
    return body.result.structuredContent;
  }
  if (isObject(body) && isObject(body.error)) return body.error;
  return body;
}

export function fate(message: Message): string {
  if (message.fate === "delivered") return `delivered at ${message.delivered_vt}`;
  if (message.fate === "dropped") return `dropped: ${message.drop_reason}`;
  return "in flight";
}

export interface Described {
  title: string;
  detail: string;
  tone: "plain" | "fault" | "dropped" | "report" | "gateway" | "human";
}

export function describeMessage(message: Message): Described {
  const route = `${message.sender} → ${message.receiver}`;
  const api = message.api ?? message.kind;
  const copy = message.copy_of === null ? "" : `, copy of #${message.copy_of}`;
  const tone = message.fate === "dropped" ? "dropped" : "plain";
  if (message.kind === "answer") {
    const outcome = message.answer_class === null ? "" : `: ${message.answer_class}`;
    return {
      title: `${route} · ${api} answer${outcome}`,
      detail: [fields(resultOf(message), SUMMARY_FIELDS), fate(message)].filter(Boolean).join("; "),
      tone,
    };
  }
  const kind = message.kind === "notification" ? " notification" : "";
  return {
    title: `${route} · ${api}${kind}${copy}`,
    detail: [fields(argumentsOf(message), ARG_FIELDS), fate(message)].filter(Boolean).join("; "),
    tone,
  };
}

function claims(report: Json): string {
  if (!isObject(report) || !Array.isArray(report.claims)) return "no claims";
  return report.claims
    .map((claim) => {
      if (!isObject(claim)) return "?";
      const effects = Array.isArray(claim.effect_ids) ? claim.effect_ids.map(scalar).join(" ") : "";
      return `${scalar(claim.intent_id)} ${scalar(claim.status)}${effects ? ` ${effects}` : ""}`;
    })
    .join("; ");
}

/** The record an event carries: its message, fault, action, report or action event. */
export function recordOf(event: TimelineEvent): unknown {
  switch (event.kind) {
    case "message":
      return event.message;
    case "fault":
      return event.fault;
    case "actor":
      return event.actor;
    case "report":
      return event.report;
    case "action_event":
      return event.action_event;
  }
}

export function describe(event: TimelineEvent): Described {
  switch (event.kind) {
    case "message":
      return describeMessage(event.message);
    case "fault": {
      const f = event.fault;
      const variant = f.variant === null ? "" : ` (${f.variant})`;
      const on = f.component === null ? "" : ` ${f.component}`;
      const target = f.target_seq === null ? "" : `message #${f.target_seq}`;
      const voided = f.voided.length === 0 ? "" : `voids #${f.voided.join(", #")}`;
      return {
        title: `${f.fault}${variant} at ${f.point}: ${f.action}${on}`,
        detail: [target, voided, fields(f.params, Object.keys(isObject(f.params) ? f.params : {}))]
          .filter(Boolean)
          .join("; "),
        tone: "fault",
      };
    }
    case "actor":
      return {
        title: `${event.actor.actor}: ${event.actor.action} ${event.actor.subject}`,
        detail: fields(event.actor.details, Object.keys(isObject(event.actor.details) ? event.actor.details : {})),
        tone: "human",
      };
    case "report": {
      const r = event.report;
      return {
        title: `${r.worker} final report: ${claims(r.report)}`,
        detail: `${r.valid ? "valid" : "invalid"}, taken by the ${r.accepted_by === "sink" ? "report sink" : "gateway"}`,
        tone: "report",
      };
    }
    case "action_event": {
      const a = event.action_event;
      const move = a.kind === "note" ? `note: ${a.reason}` : `${a.from_state ?? "new"} → ${a.to_state} (${a.reason})`;
      return {
        title: `${a.action_id} ${move}`,
        detail: fields(a.details, Object.keys(isObject(a.details) ? a.details : {})),
        tone: "gateway",
      };
    }
  }
}

export function describeLedger(entry: LedgerEntry): Described {
  if (entry.kind === "effect") {
    const e = entry.effect;
    return {
      title: `${e.effect_id} committed for ${e.intent_id}`,
      detail: [fields(e.args, ARG_FIELDS), `approval ${e.approval_id}`].join("; "),
      tone: "plain",
    };
  }
  const f = entry.fact;
  const by = f.cause === "effect" ? `by ${f.cause_effect_id}` : `by ${f.cause_actor ?? f.cause}`;
  return {
    title: `${f.key} v${f.version} ${by}`,
    detail: entry.changes.map((c) => `${c.field} ${scalar(c.from)} → ${scalar(c.to)}`).join(", "),
    tone: "plain",
  };
}
