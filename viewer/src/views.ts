// What each page shows, worked out from the API's data. Nothing here judges a run: the
// verdicts come from the evaluator (record 0004). These functions only select and align.

import type {
  Effect,
  FactVersion,
  Json,
  Ledger,
  ListedRun,
  RegistryRun,
  TimelineEvent,
} from "./types";

export const HOPS = ["agent_gw", "gw_sim", "agent_sim"] as const;

/** The hops a run's messages travel, in a fixed order. */
export function hopsOf(events: TimelineEvent[]): string[] {
  const seen = new Set(events.flatMap((e) => (e.kind === "message" ? [e.message.hop] : [])));
  const known: string[] = HOPS.filter((hop) => seen.has(hop));
  return [...known, ...[...seen].filter((hop) => !known.includes(hop)).sort()];
}

/** Messages on the chosen hops; every event that is not a message stays. */
export function filterByHops(events: TimelineEvent[], hops: ReadonlySet<string>): TimelineEvent[] {
  return events.filter((e) => e.kind !== "message" || hops.has(e.message.hop));
}

export function isAgent(component: string | null): boolean {
  return component !== null && /^agent-\d+$/.test(component);
}

/** MCP's handshake messages: protocol, not the agent's work. */
export function isHandshake(api: string | null): boolean {
  return api === "initialize" || api === "tools/list" || (api?.startsWith("notifications/") ?? false);
}

/**
 * The agent's view: what the agent sent and what was sent to it, with each message's fate,
 * the faults that hit an agent or its hop, and its final reports. The gateway's and the
 * simulator's own traffic, the scripted humans and the gateway's action events are not in it.
 */
export function agentView(events: TimelineEvent[], { handshakes = false } = {}): TimelineEvent[] {
  return events.filter((e) => {
    switch (e.kind) {
      case "message":
        return (
          (isAgent(e.message.sender) || isAgent(e.message.receiver)) &&
          (handshakes || !isHandshake(e.message.api))
        );
      case "fault":
        return (
          isAgent(e.fault.component) ||
          e.fault.point.startsWith("agent.") ||
          e.fault.point.startsWith("net.agent_")
        );
      case "report":
        return true;
      case "actor":
      case "action_event":
        return false;
    }
  });
}

export interface FieldChange {
  field: string;
  from: Json | undefined;
  to: Json;
}

export type LedgerEntry =
  | { kind: "effect"; vt: number; seq: number; effect: Effect }
  | { kind: "fact"; vt: number; seq: number; fact: FactVersion; changes: FieldChange[] };

/**
 * The ledger as entries in time: each committed effect, and each fact version after the
 * first with the fields that changed. The simulator's own counter orders entries within
 * one virtual time.
 */
export function ledgerEntries(ledger: Ledger): LedgerEntry[] {
  const latest = new Map<string, FactVersion>();
  const entries: LedgerEntry[] = ledger.effects.map((effect) => ({
    kind: "effect",
    vt: effect.committed_vt,
    seq: effect.commit_seq,
    effect,
  }));
  for (const fact of ledger.facts) {
    const before = latest.get(fact.key);
    latest.set(fact.key, fact);
    if (before === undefined) continue;
    const fields = new Set([...Object.keys(before.value), ...Object.keys(fact.value)]);
    const changes = [...fields]
      .filter((field) => JSON.stringify(before.value[field]) !== JSON.stringify(fact.value[field]))
      .sort()
      .map((field) => ({ field, from: before.value[field], to: fact.value[field] ?? null }));
    entries.push({ kind: "fact", vt: fact.valid_from_vt, seq: fact.valid_from_seq, fact, changes });
  }
  return entries.sort((a, b) =>
    a.vt - b.vt || a.seq - b.seq || Number(a.kind === "fact") - Number(b.kind === "fact"),
  );
}

/** Whether a budget's committed amount is above its limit. */
export function overdrawn(fact: FactVersion): boolean {
  const { limit_minor: limit, committed_minor: committed } = fact.value;
  return fact.key.startsWith("budget/") && typeof limit === "number" &&
    typeof committed === "number" && committed > limit;
}

export interface Column<T> {
  key: string;
  items: T[];
  vt: (item: T) => number;
}

export interface AlignedRow {
  vt: number;
  cells: unknown[][];
}

/**
 * Rows by virtual time across columns: one row per virtual time at which any column has
 * an item, and in each row, each column's items at that time, in their own order.
 */
export function alignByVt(columns: Column<unknown>[]): AlignedRow[] {
  const rows = new Map<number, AlignedRow>();
  columns.forEach((column, index) => {
    for (const item of column.items) {
      const vt = column.vt(item);
      let row = rows.get(vt);
      if (row === undefined) {
        row = { vt, cells: columns.map(() => []) };
        rows.set(vt, row);
      }
      row.cells[index]!.push(item);
    }
  });
  return [...rows.values()].sort((a, b) => a.vt - b.vt);
}

/**
 * How the viewer labels an experimental branch: a new experiment of its parent, at its
 * branch point, the number of the parent's model calls it replayed before it switched
 * model (record 0009). Null for a run that is no branch.
 */
export function experiment(run: {
  parent_run_id: string | null;
  branch_point: number | null;
}): string | null {
  if (run.parent_run_id === null || run.branch_point === null) return null;
  return `A new experiment: a branch of ${run.parent_run_id} at branch point ${run.branch_point}`;
}

export interface Pair {
  b0: RegistryRun;
  g2: RegistryRun;
}

/**
 * B0 and G2 runs of the same experiment cell. Prefer the named demo twin when it
 * also belongs to the same cell. Compare seeds as decimal text to preserve precision.
 */
export function pairs(listed: ListedRun[]): Pair[] {
  const found: Pair[] = [];
  const runs = listed.filter((run) => run.parent_run_id === null &&
    run.branch_point === null && run.cell_key);
  const g2ByCell = new Map<string, ListedRun[]>();
  for (const run of runs.filter((run) => run.config === "G2")) {
    const key = cellOf(run);
    const matches = g2ByCell.get(key);
    if (matches === undefined) g2ByCell.set(key, [run]);
    else matches.push(run);
  }
  for (const b0 of runs.filter((run) => run.config === "B0")) {
    const fits = g2ByCell.get(cellOf(b0)) ?? [];
    const twin = fits.filter((run) => run.database === b0.database.replace(/b0$/, "g2"));
    for (const g2 of twin.length > 0 ? twin : fits) found.push({ b0, g2 });
  }
  return found;
}

function cellOf(run: ListedRun): string {
  return JSON.stringify([run.scenario_id, run.contract, run.seed, run.cell_key]);
}
