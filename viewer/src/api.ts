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

function object(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

async function getJson<T>(
  path: string,
  valid: (body: Record<string, unknown>) => boolean,
  signal?: AbortSignal,
): Promise<T> {
  const response = await fetch(path, { method: "GET", headers: { Accept: "application/json" }, signal });
  let body: Record<string, unknown> | null = null;
  try {
    body = object(await response.json());
  } catch (error) {
    signal?.throwIfAborted();
    if (error instanceof DOMException && error.name === "AbortError") throw error;
  }
  signal?.throwIfAborted();
  if (!response.ok) {
    throw new ApiFailure(
      response.status,
      typeof body?.error === "string" ? body.error : "http_error",
      typeof body?.detail === "string" ? body.detail : `${path} answered ${response.status}`,
    );
  }
  if (body === null || !valid(body)) {
    throw new ApiFailure(response.status, "invalid_response", `${path} returned invalid data`);
  }
  return body as unknown as T;
}

const runPath = (database: string) => `/api/runs/${encodeURIComponent(database)}`;

export const api = {
  runs: (signal?: AbortSignal) => getJson<RunList>(
    "/api/runs", (body) => typeof body.registry === "string" && Array.isArray(body.runs), signal,
  ),
  summary: (database: string, signal?: AbortSignal) => getJson<RunSummary>(
    runPath(database), (body) => body.database === database && object(body.registry) !== null &&
      object(body.run) !== null && (body.scorecard === null || object(body.scorecard) !== null), signal,
  ),
  timeline: (database: string, signal?: AbortSignal) =>
    getJson<Timeline>(`${runPath(database)}/timeline`, (body) =>
      body.database === database && Array.isArray(body.events), signal),
  ledger: (database: string, signal?: AbortSignal) => getJson<Ledger>(
    `${runPath(database)}/ledger`, (body) => body.database === database &&
      Array.isArray(body.effects) && Array.isArray(body.facts), signal,
  ),
};
