// The sidebar's fold is remembered per browser; until the operator has chosen, the window decides.

import { afterEach, describe, expect, it, vi } from "vitest";
import { SIDEBAR_COLUMN_MIN, readSidebar, rememberSidebar, sidebarCollapsed } from "./layout";

describe("whether the sidebar is a strip", () => {
  it("follows the window until the operator has chosen", () => {
    expect(sidebarCollapsed(null, 1024)).toBe(true);
    expect(sidebarCollapsed(null, SIDEBAR_COLUMN_MIN - 1)).toBe(true);
    expect(sidebarCollapsed(null, SIDEBAR_COLUMN_MIN)).toBe(false);
    expect(sidebarCollapsed(null, 2560)).toBe(false);
  });

  it("keeps the operator's choice whatever the window", () => {
    expect(sidebarCollapsed("collapsed", 2560)).toBe(true);
    expect(sidebarCollapsed("open", 1100)).toBe(false);
  });

  it("treats a value it does not know as no choice", () => {
    expect(sidebarCollapsed("sideways", 1440)).toBe(false);
    expect(sidebarCollapsed("sideways", 1000)).toBe(true);
  });
});

describe("remembering it", () => {
  const kept = new Map<string, string>();
  afterEach(() => {
    vi.unstubAllGlobals();
    kept.clear();
  });

  it("writes the choice and reads it back", () => {
    vi.stubGlobal("localStorage", { getItem: (k: string) => kept.get(k) ?? null, setItem: (k: string, v: string) => void kept.set(k, v) });
    expect(readSidebar(1440)).toBe(false);
    rememberSidebar(true);
    expect(kept.get("daedalus.sidebar")).toBe("collapsed");
    expect(readSidebar(2560)).toBe(true);
    rememberSidebar(false);
    expect(readSidebar(1100)).toBe(false);
  });

  it("survives a browser with no storage", () => {
    vi.stubGlobal("localStorage", { getItem: () => { throw new Error("private"); }, setItem: () => { throw new Error("private"); } });
    expect(() => rememberSidebar(true)).not.toThrow();
    expect(readSidebar(1440)).toBe(false);
    expect(readSidebar(1024)).toBe(true);
  });
});
