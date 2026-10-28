import { describe, expect, it } from "vitest";

import { API_BASE, apiUrl } from "./config";

describe("apiUrl", () => {
  it("prefixes the versioned same-origin API base", () => {
    expect(API_BASE).toBe("/api/v1");
    expect(apiUrl("/health/live")).toBe("/api/v1/health/live");
  });

  it("rejects paths without a leading slash", () => {
    expect(() => apiUrl("health/live")).toThrow();
  });
});
