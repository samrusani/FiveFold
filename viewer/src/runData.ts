// Everything one run's pages show: its summary and scorecard, its timeline and its ledger.

import { api } from "./api";
import type { Ledger, RunSummary, Timeline } from "./types";

export interface RunData {
  summary: RunSummary;
  timeline: Timeline;
  ledger: Ledger;
}

export async function loadRun(database: string, signal: AbortSignal): Promise<RunData> {
  const [summary, timeline, ledger] = await Promise.all([
    api.summary(database, signal),
    api.timeline(database, signal),
    api.ledger(database, signal),
  ]);
  return { summary, timeline, ledger };
}

/** Two runs, named by a key of their two database names on separate lines. */
export async function loadPair(key: string, signal: AbortSignal): Promise<[RunData, RunData]> {
  const [left = "", right = ""] = key.split("\n");
  return Promise.all([loadRun(left, signal), loadRun(right, signal)]);
}

export function pairKey(left: string, right: string): string {
  return `${left}\n${right}`;
}
