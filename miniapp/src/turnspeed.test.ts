// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import type { RunSpeed } from "./api";
import { mergeSpeeds, speedWords } from "./turnspeed";

const speed = (rate: number, over: Partial<RunSpeed> = {}): RunSpeed => ({ tokens: 1234, ms: 14200, rate, calls: 3, model: "claude-opus-5-5", ...over });

describe("the speed under a turn", () => {
  it("keeps the runs an earlier read brought, and the same object when a read brings nothing new", () => {
    const held = { r1: speed(87.4) };
    expect(mergeSpeeds(held, undefined)).toBe(held);
    expect(mergeSpeeds(held, {})).toBe(held);
    expect(mergeSpeeds(held, { r1: speed(87.4) })).toBe(held);
    const more = mergeSpeeds(held, { r2: speed(6.25, { tokens: 40 }) });
    expect(more).not.toBe(held);
    expect(Object.keys(more)).toEqual(["r1", "r2"]);
    // A run that went on after the first read was taken: its newer figure replaces the older one.
    expect(mergeSpeeds(more, { r1: speed(90, { tokens: 2000 }) }).r1.tokens).toBe(2000);
  });

  it("reads as Usage reads a rate, with the output, the time and the model on hover", () => {
    expect(speedWords(speed(87.44)).label).toBe("≈ 87 tok/s");
    expect(speedWords(speed(6.25)).label).toBe("≈ 6.3 tok/s");
    expect(speedWords(speed(87.44)).title).toBe("1,234 output tokens · 14s of generation · claude-opus-5-5");
  });
});
