import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";

import { useLoad } from "./load";

afterEach(cleanup);

it("discards a load that completes after leaving and returning to its key", async () => {
  const finish: ((value: string) => void)[] = [];
  const load = () => new Promise<string>((resolve) => finish.push(resolve));
  const { rerender, result } = renderHook(({ key }) => useLoad(key, load), { initialProps: { key: "a" } });
  rerender({ key: "b" });
  rerender({ key: "a" });
  await act(async () => finish[2]!("current"));
  await act(async () => finish[0]!("old"));
  expect(result.current).toEqual({ state: "done", data: "current" });
});
