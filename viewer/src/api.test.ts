import { describe, expect, it, vi } from "vitest";

import { api } from "./api";

describe("viewer API responses", () => {
  it.each(["<html></html>", "null", "{}", "[]"])("rejects an invalid 200 body %s", async (text) => {
    vi.stubGlobal("fetch", async () => new Response(text));
    await expect(api.runs()).rejects.toMatchObject({ status: 200, code: "invalid_response" });
  });

  it("propagates an abort during JSON decoding", async () => {
    const controller = new AbortController();
    vi.stubGlobal("fetch", async () => ({
      ok: true,
      status: 200,
      json: () => {
        controller.abort();
        return Promise.reject(controller.signal.reason);
      },
    }));
    await expect(api.runs(controller.signal)).rejects.toMatchObject({ name: "AbortError" });
  });
});
