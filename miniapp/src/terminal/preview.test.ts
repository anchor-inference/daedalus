import { describe, expect, it } from "vitest";
import type { TerminalView } from "../api";
import { setLang } from "../i18n";
import { cardStatus, groupTerminals, headerCounts, idleUnwatched, matches, ownerLine, ownerPath, previewRows, runColor, runStyle, span, staleFor } from "./preview";

setLang("en");

const NOW = Date.parse("2026-09-24T12:00:00Z");
const ago = (minutes: number) => new Date(NOW - minutes * 60_000).toISOString();

function row(id: string, fields: Partial<TerminalView> = {}): TerminalView {
  return {
    id, env: "container", title: id, owner: { kind: "free", id: null }, project_id: null, profile: "shell", sandbox: false,
    cwd: "/work", status: "running", exit_code: null, exit_signal: null, created_at: ago(38), exited_at: null,
    last_output_at: null, last_input_at: null, cols: 80, rows: 24,
    live: { clients: 1, busy: false, keyboard: { owner: "auto", until: null }, size_owner: "human", alt_screen: false },
    ...fields,
  };
}

describe("a preview's colours", () => {
  it("leave the default colour to the card and read the palette as the daemon writes it (index plus one)", () => {
    expect(runColor(0)).toBeUndefined();
    expect(runColor(undefined)).toBeUndefined();
    // 1 is palette 0; 2 is red, which must be the scheme's token so the card follows light and dark.
    expect(runColor(1)).toBe("var(--ansi-0)");
    expect(runColor(2)).toBe("var(--ansi-1)");
    expect(runColor(16)).toBe("var(--ansi-15)");
  });

  it("draws the 256-colour cube, the grey ramp and true colour", () => {
    expect(runColor(17)).toBe("rgb(0, 0, 0)"); // 16: the cube's corner
    expect(runColor(197)).toBe("rgb(255, 0, 0)"); // 196: the cube's red
    expect(runColor(233)).toBe("rgb(8, 8, 8)"); // 232: the ramp's first grey
    expect(runColor(256)).toBe("rgb(238, 238, 238)"); // 255: its last
    expect(runColor(0x1000000 | 0x12ab34)).toBe("#12ab34");
    expect(runColor(300)).toBeUndefined();
  });

  it("swaps the colours of an inverse run, defaults included", () => {
    expect(runStyle({ t: "x", fg: 2, inv: true })).toEqual({ color: "var(--term-bg)", background: "var(--ansi-1)" });
    expect(runStyle({ t: "x", inv: true })).toEqual({ color: "var(--term-bg)", background: "var(--fg)" });
    expect(runStyle({ t: "x" })).toBeUndefined();
    expect(runStyle({ t: "x", b: true, u: true })).toEqual({ fontWeight: 700, textDecoration: "underline" });
  });

  it("shows the last six rows that hold anything", () => {
    const rows = Array.from({ length: 10 }, (_, i) => [{ t: `line ${i}` }]);
    const blank = [{ t: "   " }];
    const shown = previewRows([...rows, blank, blank]);
    expect(shown.map((r) => r[0].t)).toEqual(["line 4", "line 5", "line 6", "line 7", "line 8", "line 9"]);
    expect(previewRows(undefined)).toEqual([]);
  });
});

describe("filters, counts and groups", () => {
  const rows = [
    row("a"),
    row("b", { env: "host" }),
    row("c", { owner: { kind: "staff", id: "s1", label: "Ira" }, project_id: "p1" }),
    row("d", { status: "exited", exit_code: 1, exited_at: ago(2), project_id: "p1" }),
    row("e", { env: "host", status: "exited", exit_code: 0, exited_at: ago(20) }),
  ];

  it("count the running terminals only in the header", () => {
    expect(headerCounts(rows)).toEqual({ open: 3, host: 1, staff: 1 });
  });

  it("put a finished terminal under All and Finished, and nowhere else", () => {
    const under = (f: Parameters<typeof matches>[1]) => rows.filter((r) => matches(r, f)).map((r) => r.id);
    expect(under("all")).toEqual(["a", "b", "c", "d", "e"]);
    expect(under("container")).toEqual(["a", "c"]);
    expect(under("host")).toEqual(["b"]);
    expect(under("staff")).toEqual(["c"]);
    expect(under("finished")).toEqual(["d", "e"]);
  });

  it("group by project in the projects' order, then the rest, running first", () => {
    const groups = groupTerminals(rows, [{ id: "p2", name: "Empty" }, { id: "p1", name: "Bakery" }]);
    expect(groups.map((g) => [g.name, g.rows.map((r) => r.id)])).toEqual([
      ["Bakery", ["c", "d"]],
      [null, ["a", "b", "e"]],
    ]);
  });

  it("send a terminal of a project the listing no longer knows to No project", () => {
    const groups = groupTerminals([row("x", { project_id: "gone" })], []);
    expect(groups).toEqual([{ key: "", name: null, rows: [expect.objectContaining({ id: "x" })] }]);
  });
});

describe("a card's status line", () => {
  it("says how a finished terminal ended and when", () => {
    expect(cardStatus(row("d", { status: "exited", exit_code: 1, exited_at: ago(2) }), NOW)).toEqual({ text: "code 1 · 2m ago", level: "bad" });
    expect(cardStatus(row("d", { status: "exited", exit_code: 0, exited_at: ago(20) }), NOW)).toEqual({ text: "code 0 · 20m ago", level: "off" });
    expect(cardStatus(row("d", { status: "exited", exit_signal: "SIGHUP", exited_at: ago(1) }), NOW).text).toBe("signal SIGHUP · 1m ago");
    expect(cardStatus(row("d", { status: "lost", exited_at: ago(90) }), NOW).text).toBe("lost · 1h ago");
  });

  it("says how long a watched terminal has run, and how long an unwatched one has been left", () => {
    expect(cardStatus(row("a"), NOW)).toEqual({ text: "running 38m", level: "ok" });
    const left = row("a", { live: { clients: 0, busy: false, keyboard: { owner: "auto", until: null }, size_owner: "", alt_screen: false }, last_input_at: ago(60) });
    expect(cardStatus(left, NOW)).toEqual({ text: "nobody watching · 1h", level: "idle" });
  });

  it("lets another part of the app say what the terminal is doing", () => {
    const waiting = row("c", { activity: { label: "waiting for permission · 1m", level: "warn", action: { label: "Answer", path: "/app/inbox" } } });
    expect(cardStatus(waiting, NOW)).toEqual({ text: "waiting for permission · 1m", level: "warn" });
  });

  it("counts seconds, minutes, hours and days", () => {
    expect(span(new Date(NOW - 40_000).toISOString(), NOW)).toBe("40s");
    expect(span(ago(60 * 50), NOW)).toBe("2d");
    expect(span(null, NOW)).toBe("");
  });
});

describe("owners", () => {
  it("name the owner and link to where it lives", () => {
    const session = row("a", { owner: { kind: "session", id: "s1", label: "Checkout page" } });
    const staff = row("c", { owner: { kind: "staff", id: "st1", label: "Ira" }, project_id: "p1" });
    expect(ownerLine(session, null)).toBe("session «Checkout page»");
    expect(ownerLine(staff, "Bakery")).toBe("staff Ira · project «Bakery»");
    expect(ownerLine(row("f", { cwd: "~/work" }), null)).toBe("free · ~/work");
    const paths = (r: TerminalView) => ownerPath(r, (id) => `/s/${id}`, (id) => `/p/${id}`);
    expect(paths(session)).toBe("/s/s1");
    expect(paths(staff)).toBe("/p/p1");
    expect(paths(row("f"))).toBeNull();
  });
});

describe("clean-up", () => {
  const unwatched = { clients: 0, busy: false, keyboard: { owner: "auto" as const, until: null }, size_owner: "", alt_screen: false };

  it("takes only shells at their prompt that nobody watches and nobody works in as idle", () => {
    expect(idleUnwatched(row("a", { live: unwatched, at_prompt: true }))).toBe(true);
    expect(idleUnwatched(row("b", { at_prompt: true }))).toBe(false);
    expect(idleUnwatched(row("c", { live: unwatched, at_prompt: false, running: "npm run dev" }))).toBe(false);
    expect(idleUnwatched(row("d", { live: unwatched }))).toBe(false);
    expect(idleUnwatched(row("e", { live: unwatched, at_prompt: true, owner: { kind: "staff", id: "st", label: "ira" } }))).toBe(false);
    expect(idleUnwatched(row("f", { live: unwatched, at_prompt: true, status: "exited" }))).toBe(false);
  });

  it("marks an unwatched terminal untouched for a day, by its newest input or output", () => {
    expect(staleFor(row("a", { live: unwatched, created_at: ago(60 * 24 * 7) }), NOW)).toBe("7d");
    expect(staleFor(row("b", { live: unwatched, created_at: ago(60 * 24 * 7), last_output_at: ago(5) }), NOW)).toBe("");
    expect(staleFor(row("c", { created_at: ago(60 * 24 * 7) }), NOW)).toBe("");
    expect(staleFor(row("d", { live: unwatched, created_at: ago(60 * 24 * 3), owner: { kind: "staff", id: "st", label: "ira" } }), NOW)).toBe("");
  });
});
