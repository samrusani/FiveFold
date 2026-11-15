// Read-only calls to the viewer's API. Every call is a GET (record 0012).

import type { Ledger, RunList, RunSummary, Timeline } from "./types";

export class ApiFailure extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, detail: string) {
    super(detail);
    this.status = status;
    this.code = code;
  }
}

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, { method: "GET", headers: { Accept: "application/json" }, signal });
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const error = body as { error?: string; detail?: string } | null;
    throw new ApiFailure(
      response.status,
      error?.error ?? "http_error",
      error?.detail ?? `${path} answered ${response.status}`,
    );
  }
  return body as T;
}

const runPath = (database: string) => `/api/runs/${encodeURIComponent(database)}`;

export const api = {
  runs: (signal?: AbortSignal) => getJson<RunList>("/api/runs", signal),
  summary: (database: string, signal?: AbortSignal) => getJson<RunSummary>(runPath(database), signal),
  timeline: (database: string, signal?: AbortSignal) =>
    getJson<Timeline>(`${runPath(database)}/timeline`, signal),
  ledger: (database: string, signal?: AbortSignal) =>
    getJson<Ledger>(`${runPath(database)}/ledger`, signal),
};
