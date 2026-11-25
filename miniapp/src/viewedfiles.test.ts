// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { readMode, readViewed, writeMode, writeViewed } from "./viewedfiles";

afterEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
});

describe("what a reader marked in a change set", () => {
  it("keeps the viewed files per change set and forgets them when none is left", () => {
    writeViewed("p1", new Set(["a.py", "b.py"]));
    writeViewed("p2", new Set(["c.py"]));
    expect([...readViewed("p1")]).toEqual(["a.py", "b.py"]);
    writeViewed("p1", new Set());
    expect(readViewed("p1").size).toBe(0);
    expect(localStorage.getItem("daedalus.changes.viewed.p1")).toBeNull();
    expect([...readViewed("p2")]).toEqual(["c.py"]);
  });

  it("starts empty from storage that holds something else", () => {
    localStorage.setItem("daedalus.changes.viewed.p1", "{not json");
    expect(readViewed("p1").size).toBe(0);
    localStorage.setItem("daedalus.changes.viewed.p1", JSON.stringify([1, "a.py", null]));
    expect([...readViewed("p1")]).toEqual(["a.py"]);
  });

  it("remembers the layout and falls back to the unified one", () => {
    expect(readMode()).toBe("unified");
    writeMode("split");
    expect(readMode()).toBe("split");
  });

  it("works without storage: reads come back empty and writes do not throw", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("blocked"); });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("blocked"); });
    expect(readViewed("p1").size).toBe(0);
    expect(readMode()).toBe("unified");
    expect(() => writeViewed("p1", new Set(["a.py"]))).not.toThrow();
    expect(() => writeMode("split")).not.toThrow();
  });
});
