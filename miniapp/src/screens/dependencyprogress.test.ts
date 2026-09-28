import { describe, expect, it } from "vitest";
import { clock } from "./dependencyprogress";

describe("the installation stopwatch", () => {
  it("reads minutes and seconds under an hour", () => {
    expect(clock(0)).toBe("0:00");
    expect(clock(247)).toBe("4:07");
    expect(clock(3599)).toBe("59:59");
  });
  it("gives an hour or more its own field rather than counting minutes past sixty", () => {
    expect(clock(3600)).toBe("1:00:00");
    expect(clock(187 * 60 + 12)).toBe("3:07:12");
  });
  it("never reads a negative time when the host clock runs ahead", () => {
    expect(clock(-5)).toBe("0:00");
  });
});
