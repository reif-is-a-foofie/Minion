import { describe, expect, it } from "vitest";
import { apiPortFromBase } from "./net_util";

describe("apiPortFromBase", () => {
  it("parses explicit port", () => {
    expect(apiPortFromBase("http://127.0.0.1:9876")).toBe(9876);
  });

  it("uses fallback when missing", () => {
    expect(apiPortFromBase("http://example.test", 3030)).toBe(3030);
  });
});
