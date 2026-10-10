import { beforeEach, describe, expect, it } from "vitest";
import { canTick, defaultSelection, fillPercent, reasonKey, selectedBytes, sizeText, tone, withoutTracked, type DiskEntry } from "./diskmodel";
import { setLang } from "./i18n";

function entry(path: string, over: Partial<DiskEntry> = {}): DiskEntry {
  return { path, bytes: 1024, files: 3, newest: null, throwaway: false, tracked: false, protected: false, children: [], ...over };
}
const GB = 1024 ** 3;
const entries = [
  entry("_scratch", { bytes: 15 * GB, throwaway: true }),
  entry(".uv-cache", { bytes: 3 * GB, throwaway: true }),
  entry("src", { bytes: 2 * GB, tracked: true }),
  entry(".checkpoints", { bytes: GB, protected: true, throwaway: true }),
];

beforeEach(() => setLang("en"));

describe("disk model", () => {
  it("preselects only untracked, unprotected throwaway folders", () => {
    expect([...defaultSelection(entries)]).toEqual(["_scratch", ".uv-cache"]);
  });
  it("lets a tracked folder be ticked only after the extra consent, and never a protected one", () => {
    expect(canTick(entries[2], false)).toBe(false);
    expect(canTick(entries[2], true)).toBe(true);
    expect(canTick(entries[3], true)).toBe(false);
    expect(canTick(entries[0], false)).toBe(true);
  });
  it("drops tracked folders when the consent is withdrawn", () => {
    expect([...withoutTracked(new Set(["_scratch", "src"]), entries)]).toEqual(["_scratch"]);
  });
  it("adds up what is ticked", () => {
    expect(selectedBytes(new Set(["_scratch", ".uv-cache"]), entries)).toBe(18 * GB);
  });
  it("reads a truncated size as a floor", () => {
    expect(sizeText(23 * GB, false)).toBe("23.0 GB");
    expect(sizeText(23 * GB, true)).toBe("at least 23.0 GB");
  });
  it("fills the bar against the limit and clamps it", () => {
    expect(fillPercent(10 * GB, 20 * GB)).toBe(50);
    expect(fillPercent(50 * GB, 20 * GB)).toBe(100);
    expect(fillPercent(10 * GB, 0)).toBe(0);
  });
  it("maps the server's verdict to a tone and a refusal to a word", () => {
    expect([tone(0), tone(1), tone(2)]).toEqual(["", "attn", "bad"]);
    expect(reasonKey("tracked")).toBe("disk.refused.tracked");
    expect(reasonKey("surprise")).toBe("disk.refused.other");
  });
});
