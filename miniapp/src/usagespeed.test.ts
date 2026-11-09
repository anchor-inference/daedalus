// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { fmtRate } from "./format";

describe("a provider's output rate", () => {
  it("reads in whole tokens past ten a second and keeps a decimal below", () => {
    expect(fmtRate(87.44)).toBe("87");
    expect(fmtRate(10)).toBe("10");
    expect(fmtRate(6.25)).toBe("6.3");
  });
});
