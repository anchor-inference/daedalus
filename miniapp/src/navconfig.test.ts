// The resolver that places every destination. The first claim is the one a fresh installation rests
// on: with nothing configured the sidebar's pinned rows and the menu are exactly the
// lists the app ships, written out here rather than imported so a change to the defaults is a change to this
// file too.

import { describe, expect, it } from "vitest";
import { type NavConfig, type PageInfo, catalogue, desktopLayout, iconPath, launcherLayout, moveItem, pagePath, resolveNav } from "./navconfig";

const ids = (items: { id: string }[]) => items.map((it) => it.id);
const DOSING: PageInfo = { slug: "dosing", title: { en: "Dosing", ru: "Дозировка" }, icon: null, icon_path: "M4 12h16M12 4v16", path: "/app/p/dosing", version: 3, updated_at: "2026-10-11T08:00:00Z" };

describe("with nothing configured", () => {
  const nav = resolveNav({ config: null, pages: null, selfdev: "server" });

  it("pins the five daily destinations under New chat", () => {
    expect(ids(nav.pinned)).toEqual(["inbox", "board", "calendar", "changes", "terminals"]);
  });

  it("puts the rest under More, in their groups", () => {
    expect(nav.more.map((g) => [g.key, ids(g.items)])).toEqual([
      ["work", ["companion", "canvas", "harnesses"]],
      ["autonomy", ["schedules", "services"]],
      ["knowledge", ["memory"]],
      ["observe", ["usage", "health"]],
    ]);
  });

  it("is the same answer for an empty section as for a missing one", () => {
    expect(resolveNav({ config: { pinned: [], more: [], hidden: [], icons: {} }, selfdev: "server" })).toEqual(nav);
  });

  it("names every item and links it to its screen", () => {
    const inbox = nav.pinned[0];
    expect(inbox).toMatchObject({ id: "inbox", kind: "screen", screen: "inbox", title: "Inbox", icon: "inbox", iconPath: null, path: "/app/inbox" });
  });
});

describe("what the installation has", () => {
  it("leaves Changes out where the agent cannot change its own code", () => {
    const nav = resolveNav({ selfdev: "off" });
    expect(ids(nav.pinned)).not.toContain("changes");
    expect(nav.catalogue.some((it) => it.id === "changes")).toBe(false);
  });

  it("carries the counts as badges", () => {
    const nav = resolveNav({ selfdev: "server", counts: { inbox: 4, changes: 2 } });
    expect(nav.pinned.find((it) => it.id === "inbox")?.badge).toBe(4);
    expect(nav.pinned.find((it) => it.id === "changes")?.badge).toBe(2);
    expect(nav.pinned.find((it) => it.id === "board")?.badge).toBe(0);
  });
});

describe("the operator's layout", () => {
  it("orders the pinned rows as listed, and an unlisted shipped place keeps its spot after them", () => {
    const nav = resolveNav({ config: { pinned: ["terminals", "inbox", "memory"] }, selfdev: "server" });
    expect(ids(nav.pinned)).toEqual(["terminals", "inbox", "memory", "board", "calendar", "changes"]);
    expect(nav.more.find((g) => g.key === "knowledge")).toBeUndefined();
  });

  it("moves a shipped pinned place under More, into its group, in the listed order", () => {
    const nav = resolveNav({ config: { more: ["health", "board", "usage"] }, selfdev: "server" });
    expect(ids(nav.pinned)).not.toContain("board");
    expect(ids(nav.more.find((g) => g.key === "work")!.items)).toEqual(["board", "companion", "canvas", "harnesses"]);
    expect(ids(nav.more.find((g) => g.key === "observe")!.items)).toEqual(["health", "usage"]);
  });

  it("hides an item everywhere, over every other list", () => {
    const nav = resolveNav({ config: { pinned: ["companion"], hidden: ["companion", "usage"] }, selfdev: "server" });
    for (const list of [nav.pinned, nav.more.flatMap((g) => g.items), nav.shown]) {
      expect(ids(list)).not.toContain("companion");
      expect(ids(list)).not.toContain("usage");
    }
    expect(nav.catalogue.some((it) => it.id === "companion")).toBe(true);
  });

  it("ignores ids it does not know", () => {
    const nav = resolveNav({ config: { pinned: ["page:gone", "nonsense", "inbox"], hidden: ["ghost"], icons: { ghost: "bolt", inbox: "not-an-icon" } }, selfdev: "server" });
    expect(ids(nav.pinned)[0]).toBe("inbox");
    expect(nav.pinned[0].icon).toBe("inbox");
  });

  it("draws an icon chosen in place of the item's own", () => {
    const nav = resolveNav({ config: { icons: { terminals: "bolt" } }, selfdev: "server" });
    expect(nav.pinned.find((it) => it.id === "terminals")?.icon).toBe("bolt");
  });
});

describe("pages", () => {
  it("join the menu under their own heading, and stay unpinned until placed", () => {
    const nav = resolveNav({ pages: [DOSING], selfdev: "server" });
    const page = nav.more.find((g) => g.key === "pages")!.items[0];
    expect(page).toMatchObject({ id: "page:dosing", kind: "page", screen: null, title: "Dosing", icon: "file", iconPath: "M4 12h16M12 4v16", path: "/app/p/dosing", badge: 0 });
    expect(ids(nav.pinned)).not.toContain("page:dosing");
  });

  it("go where they are placed, like any screen", () => {
    const nav = resolveNav({ config: { pinned: ["page:dosing", "inbox"] }, pages: [DOSING], selfdev: "server" });
    expect(ids(nav.pinned).slice(0, 2)).toEqual(["page:dosing", "inbox"]);
    expect(nav.more.find((g) => g.key === "pages")).toBeUndefined();
  });

  it("take an icon name over their own glyph, and fall back to their slug for a title", () => {
    const named = resolveNav({ config: { icons: { "page:dosing": "flag" } }, pages: [DOSING], selfdev: "server" });
    expect(named.shown.find((it) => it.id === "page:dosing")).toMatchObject({ icon: "flag", iconPath: null });
    const [bare] = catalogue([{ slug: "cad", title: null, icon: "pen", icon_path: "<script>" }], "server").filter((it) => it.kind === "page");
    expect(bare).toMatchObject({ title: "cad", icon: "pen", iconPath: null, path: pagePath("cad") });
  });

  it("are listed once, and a broken entry is skipped", () => {
    const pages = [DOSING, DOSING, { slug: "" }, null as unknown as PageInfo];
    expect(catalogue(pages, "server").filter((it) => it.kind === "page")).toHaveLength(1);
  });
});

describe("a page's own glyph", () => {
  it("is one path on the 24 grid", () => {
    expect(iconPath("M4 12h16M12 4v16")).toBe("M4 12h16M12 4v16");
    expect(iconPath("  m12 3a9 9 0 1 0 0 18\n9 9 0 0 0 0-18z ")).toBe("m12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z");
    expect(iconPath("M-.5 1.5l3-3")).toBe("M-.5 1.5l3-3");
  });

  it("refuses anything that is not path commands and numbers", () => {
    for (const bad of ['M4 4"/><script>', "M4 4 url(x)", "<path d='M1 1'/>", "4 4h16", "M", "", null, 42, "M4 4e5"]) {
      expect(iconPath(bad)).toBeNull();
    }
  });

  it("refuses a coordinate off the grid, and a path too long to be a glyph", () => {
    expect(iconPath("M100 100h800")).toBeNull();
    expect(iconPath("M1 1" + "l1 1".repeat(400))).toBeNull();
  });
});

describe("what Settings writes", () => {
  it("splits its order into the three lists and keeps what it could not show", () => {
    const previous: NavConfig = { pinned: ["page:away", "inbox"], hidden: ["page:gone"] };
    const known = new Set(["inbox", "board", "companion"]);
    expect(desktopLayout(previous, ["board", "inbox", "companion"], { board: "pinned", inbox: "pinned", companion: "hidden" }, known)).toEqual({
      pinned: ["board", "inbox", "page:away"],
      more: [],
      hidden: ["companion", "page:gone"],
    });
  });

  it("moves one item and closes the gap", () => {
    expect(moveItem(["a", "b", "c", "d"], 0, 2)).toEqual(["b", "c", "a", "d"]);
    expect(moveItem(["a", "b", "c"], 2, 0)).toEqual(["c", "a", "b"]);
    expect(moveItem(["a", "b"], 5, 0)).toEqual(["a", "b"]);
  });
});

describe("the More launcher", () => {
  const nav = resolveNav({ config: { hidden: ["health"] }, pages: [DOSING], selfdev: "server" });

  it("marks what the surface already shows, and never offers what is hidden", () => {
    const desk = launcherLayout(nav, nav.pinned);
    expect([...desk.placed]).toEqual(ids(nav.pinned));
    expect(ids(nav.shown)).toEqual(["companion", "inbox", "board", "calendar", "canvas", "terminals", "harnesses", "changes", "schedules", "services", "memory", "usage", "page:dosing"]);
    expect(desk.fallback).not.toContain("health");
  });

  it("offers what the surface does not show while Frequent has learned nothing", () => {
    expect(launcherLayout(nav, nav.pinned).fallback).toEqual(["companion", "canvas", "harnesses", "schedules", "services", "memory", "usage", "page:dosing"]);
    expect(launcherLayout(nav, nav.pinned).fallback).not.toContain("inbox");
  });
});
