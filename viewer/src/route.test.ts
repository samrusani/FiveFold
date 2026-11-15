import { describe, expect, it } from "vitest";

import { href, parseRoute } from "./route";

describe("routes in the URL's fragment", () => {
  it("reads the three pages", () => {
    expect(parseRoute("")).toEqual({ page: "runs" });
    expect(parseRoute("#/")).toEqual({ page: "runs" });
    expect(parseRoute("#/runs/fivefold_demo_1_g2")).toEqual({ page: "run", database: "fivefold_demo_1_g2" });
    expect(parseRoute("#/compare/a_b0/a_g2")).toEqual({ page: "compare", left: "a_b0", right: "a_g2" });
  });

  it("names a fragment it cannot read", () => {
    expect(parseRoute("#/runs")).toEqual({ page: "missing", hash: "#/runs" });
    expect(parseRoute("#/elsewhere/x")).toEqual({ page: "missing", hash: "#/elsewhere/x" });
  });

  it("writes what it reads", () => {
    for (const route of [
      { page: "runs" } as const,
      { page: "run", database: "fivefold_demo_1_b0" } as const,
      { page: "compare", left: "x y", right: "z/w" } as const,
    ]) {
      expect(parseRoute(href(route))).toEqual(route);
    }
  });
});
