import { beforeEach, describe, expect, it } from "vitest";
import {
  canTick, defaultSelection, diskBytes, fillPercent, limitChoices, limitText, parseLimitGb, reasonKey, selectedBytes, sizeText, tone, withLimit, withoutTracked,
  type DiskDetail, type DiskEntry,
} from "./diskmodel";
import { setLang } from "./i18n";

function entry(path: string, over: Partial<DiskEntry> = {}): DiskEntry {
  return { path, bytes: 1024, files: 3, newest: null, throwaway: false, tracked: false, protected: false, children: [], ...over };
}
const GB = 1e9;
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
    expect(sizeText(23 * GB, false)).toBe("23 GB");
    expect(sizeText(23.4 * GB, true)).toBe("at least 23.4 GB");
  });
  it("counts in decimal units, so a 20 GB limit reads 20 GB", () => {
    expect(diskBytes(20 * GB)).toBe("20 GB");
    expect(diskBytes(22.2 * GB)).toBe("22.2 GB");
    expect(diskBytes(209 * GB)).toBe("209 GB");
    expect(diskBytes(2.5e12)).toBe("2.5 TB");
    expect(diskBytes(340e6)).toBe("340 MB");
    expect(diskBytes(5.8e6)).toBe("5.8 MB");
    expect(diskBytes(950)).toBe("950 B");
    expect(diskBytes(0)).toBe("0 B");
    // A value that rounds up to the next unit is said in that unit, not as "1000 MB".
    expect(diskBytes(999.7e6)).toBe("1 GB");
    expect(diskBytes(999_990)).toBe("1 MB");
    setLang("ru");
    expect(diskBytes(20 * GB)).toBe("20 ГБ");
  });
  it("says there is no limit in words", () => {
    expect(limitText(0)).toBe("no limit");
    expect(limitText(30 * GB)).toBe("30 GB");
  });
  it("offers quick limits relative to the one in force, and never the one already in force", () => {
    expect(limitChoices(20 * GB).map((c) => [c.id, c.gb, c.label])).toEqual([["plus", 30, "+10 GB"], ["double", 40, "×2"], ["g50", 50, "50 GB"], ["g100", 100, "100 GB"]]);
    // 40 + 10 is already the fixed 50: it is offered once.
    expect(limitChoices(40 * GB).map((c) => c.gb)).toEqual([50, 80, 100]);
    expect(limitChoices(50 * GB).map((c) => c.gb)).toEqual([60, 100]);
    // With no limit there is nothing to add to.
    expect(limitChoices(0).map((c) => c.id)).toEqual(["g50", "g100"]);
    expect(limitChoices(12.345 * GB)[0].gb).toBe(22.345);
  });
  it("accepts a typed limit only when it is a positive number the host takes", () => {
    expect(parseLimitGb("30")).toBe(30);
    expect(parseLimitGb(" 12,5 ")).toBe(12.5);
    expect(parseLimitGb("0.25")).toBe(0.25);
    for (const bad of ["", "  ", "0", "-5", "abc", "1e400", "2000000"]) expect(parseLimitGb(bad), bad).toBeNull();
  });
  it("redraws one workspace with its new limit and leaves the others alone", () => {
    const detail: DiskDetail = {
      default_limit_bytes: 20 * GB, free_bytes: 0, total_bytes: 0, low_disk: false, outside: [],
      workspaces: ["a", "b"].map((name) => ({ name, label: name, bytes: 22 * GB, truncated: false, limit_bytes: 20 * GB, own_limit: false, over: 1 as const, entries: [] })),
    };
    const next = withLimit(detail, { name: "a", limit_bytes: 50 * GB, own_limit: true, default_limit_bytes: 20 * GB, over: 0 });
    expect(next.workspaces.map((w) => [w.limit_bytes, w.own_limit, w.over])).toEqual([[50 * GB, true, 0], [20 * GB, false, 1]]);
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
