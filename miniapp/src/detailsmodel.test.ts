import { describe, expect, it } from "vitest";
import { contextSegments, contextTone, originalNote, quotaTone } from "./detailsmodel";

describe("quotaTone", () => {
  it("is green with room, amber from 80 and red once spent", () => {
    expect([0, 79.9, 80, 99, 100, 140].map(quotaTone)).toEqual(["ok", "ok", "attn", "attn", "bad", "bad"]);
  });
});

describe("contextTone", () => {
  it("stays neutral while the window fills, amber from 60 and red from 90", () => {
    expect([0, 59, 60, 89, 90].map(contextTone)).toEqual([null, null, "attn", "attn", "bad"]);
  });
});

describe("contextSegments", () => {
  it("measures the parts against the window, leaving the room that is left", () => {
    const parts = contextSegments({ instructions: 100, tools: 100, conversation: 200, attachments: 0, reserved_response: 100 }, 1000);
    expect(parts.map((p) => p.key)).toEqual(["instructions", "tools", "conversation", "attachments", "reserved_response"]);
    expect(parts.map((p) => p.percent)).toEqual([10, 10, 20, 0, 10]);
  });

  it("measures against the parts' sum when the estimate overshoots the window, so the bar never passes 100", () => {
    const parts = contextSegments({ instructions: 600, conversation: 600 }, 1000);
    expect(parts.reduce((sum, p) => sum + p.percent, 0)).toBeCloseTo(100);
    expect(parts.find((p) => p.key === "instructions")?.percent).toBeCloseTo(50);
  });

  it("lists all five parts with no width when nothing is known or the window is unreported", () => {
    expect(contextSegments(null, 0).map((p) => [p.tokens, p.percent])).toEqual([[0, 0], [0, 0], [0, 0], [0, 0], [0, 0]]);
    expect(contextSegments({ tools: 50, conversation: 150 }, 0).map((p) => p.percent)).toEqual([0, 25, 75, 0, 0]);
  });
});

describe("originalNote", () => {
  it("reads both sizes from the host's sentence", () => {
    expect(originalNote("the original is 188057562 bytes, over the 67108864 kept")).toEqual({ size: 188057562, limit: 67108864 });
  });

  it("reads the limit alone when the host only learned the size was over it mid-read", () => {
    expect(originalNote("the original is over the 67108864 bytes kept")).toEqual({ size: null, limit: 67108864 });
  });

  it("leaves a reason that is not about size to be shown as written", () => {
    expect(originalNote("the host terminal daemon did not send the original")).toBeNull();
    expect(originalNote("")).toBeNull();
    expect(originalNote(null)).toBeNull();
  });
});
