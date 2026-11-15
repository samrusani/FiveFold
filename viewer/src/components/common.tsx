import type { ReactNode } from "react";

import { ApiFailure } from "../api";
import type { Load } from "../load";

const VERDICT_TEXT: Record<string, string> = {
  pass: "pass",
  pass_unresolved: "pass, unresolved",
  fail: "fail",
  not_evaluated: "not evaluated",
};

export function Verdict({ verdict }: { verdict: string | null }) {
  const value = verdict ?? "not_evaluated";
  return <span className={`verdict verdict-${value}`}>{VERDICT_TEXT[value] ?? value}</span>;
}

export function Loaded<T>({ load, children }: { load: Load<T>; children: (data: T) => ReactNode }) {
  if (load.state === "loading") return <p className="muted">Loading…</p>;
  if (load.state === "failed") {
    const code = load.error instanceof ApiFailure ? `${load.error.code}: ` : "";
    return (
      <p className="error" role="alert">
        {code}
        {load.error.message}
      </p>
    );
  }
  return <>{children(load.data)}</>;
}

export function Json({ value }: { value: unknown }) {
  return <pre className="json">{JSON.stringify(value, null, 2)}</pre>;
}
