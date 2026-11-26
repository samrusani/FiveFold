// Loading API data for a page, keyed so a new key starts a new load and drops the old one.

import { useEffect, useState } from "react";

export type Load<T> =
  | { state: "loading" }
  | { state: "failed"; error: Error }
  | { state: "done"; data: T };

export function useLoad<T>(key: string, load: (key: string, signal: AbortSignal) => Promise<T>): Load<T> {
  const [result, setResult] = useState<{ key: string; value: Load<T> } | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    load(key, controller.signal).then(
      (data) => {
        if (!controller.signal.aborted) setResult({ key, value: { state: "done", data } });
      },
      (error: unknown) => {
        if (controller.signal.aborted) return;
        const failure = error instanceof Error ? error : new Error(String(error));
        setResult({ key, value: { state: "failed", error: failure } });
      },
    );
    return () => controller.abort();
  }, [key, load]);
  return result !== null && result.key === key ? result.value : { state: "loading" };
}
