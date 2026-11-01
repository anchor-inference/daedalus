import { describe, expect, it } from "vitest";
import type { StaffTurn, TerminalCommand, TerminalLayout } from "../api";
import {
  assign, clampSide, commandActions, commandKind, filterActions, kindCounts, program, readSide, reopenable, restoreLayout, saveSide, SIDE_DEFAULT, SIDE_W_MAX, SIDE_W_MIN, toolKind, turnActions,
} from "./sidestate";
import { EMPTY, normalise } from "./workspacestate";

function memory(): Storage {
  const data = new Map<string, string>();
  return { getItem: (k: string) => data.get(k) ?? null, setItem: (k: string, v: string) => void data.set(k, v) } as unknown as Storage;
}

function command(n: number, text: string, exit: number | null, extra: Partial<TerminalCommand> = {}): TerminalCommand {
  return { n, command: text, cwd: "/work/shop", exit_code: exit, started_at: `2026-11-01T10:0${n}:00Z`, finished_at: exit === null ? null : `2026-11-01T10:0${n}:05Z`, duration_ms: exit === null ? null : 1200 * n, prompt_row: null, output_row: n * 3, end_row: exit === null ? null : n * 3 + 2, ...extra };
}

describe("the panel's width", () => {
  it("stays between its minimum and half the window", () => {
    expect(clampSide(100, 1600)).toBe(SIDE_W_MIN);
    expect(clampSide(900, 1600)).toBe(SIDE_W_MAX);
    expect(clampSide(700, 1200)).toBe(600);
    expect(clampSide(400, 1200)).toBe(400);
    // A window narrower than twice the minimum still gets the minimum: the grid gives way first.
    expect(clampSide(400, 500)).toBe(SIDE_W_MIN);
    expect(clampSide(Number.NaN, 1600)).toBe(360);
  });
});

describe("what the panel remembers", () => {
  it("round-trips, and falls back to closed on files when the stored value is not one", () => {
    const store = memory();
    expect(readSide(store)).toEqual(SIDE_DEFAULT);
    saveSide({ open: true, width: 420, tab: "actions" }, store);
    expect(readSide(store)).toEqual({ open: true, width: 420, tab: "actions" });
    store.setItem("daedalus.term.side", JSON.stringify({ open: "yes", width: "wide", tab: "network" }));
    expect(readSide(store)).toEqual(SIDE_DEFAULT);
    store.setItem("daedalus.term.side", "{not json");
    expect(readSide(store)).toEqual(SIDE_DEFAULT);
    expect(readSide(null)).toEqual(SIDE_DEFAULT);
  });
});

describe("the kinds of action", () => {
  it("reads the program past settings and wrappers", () => {
    expect(program("FOO=1 BAR=2 sudo /usr/bin/git status")).toBe("git");
    expect(program("  time npm test")).toBe("npm");
    expect(program("")).toBe("");
  });

  it("names a shell command's kind by its program", () => {
    expect(commandKind("git push")).toBe("git");
    expect(commandKind("cat README.md")).toBe("reads");
    expect(commandKind("rg TODO src")).toBe("search");
    expect(commandKind("sed -i s/a/b/ x")).toBe("edits");
    expect(commandKind("sed s/a/b/ x")).toBe("shell");
    expect(commandKind("curl -s localhost")).toBe("web");
    expect(commandKind("npm run build")).toBe("shell");
  });

  it("names a CLI's tool by the words each CLI uses", () => {
    expect(toolKind("Bash")).toBe("shell");
    expect(toolKind("run_terminal_cmd")).toBe("shell");
    expect(toolKind("Read")).toBe("reads");
    expect(toolKind("read_file")).toBe("reads");
    expect(toolKind("Grep")).toBe("search");
    expect(toolKind("Glob")).toBe("search");
    expect(toolKind("Edit")).toBe("edits");
    expect(toolKind("edit_file")).toBe("edits");
    expect(toolKind("WebFetch")).toBe("web");
    expect(toolKind("WebSearch")).toBe("web");
    expect(toolKind("mcp__github__create_issue")).toBe("mcp");
    expect(toolKind("Task")).toBe("agents");
    expect(toolKind("TodoWrite")).toBe("tool");
    expect(toolKind("Something")).toBe("tool");
  });
});

describe("a shell's feed", () => {
  const rows = commandActions([command(1, "git status", 0), command(2, "npm test", 1), command(3, "   ", 0), command(4, "npm run dev", null)]);

  it("is newest first, with failures and the running command marked, and blank lines dropped", () => {
    expect(rows.map((r) => r.title)).toEqual(["npm run dev", "npm test", "git status"]);
    expect(rows[0]).toMatchObject({ running: true, failed: false, exit: null, durationMs: null, n: 4 });
    expect(rows[1]).toMatchObject({ running: false, failed: true, exit: 1, durationMs: 2400, kind: "shell" });
    expect(rows[2]).toMatchObject({ failed: false, kind: "git", detail: "/work/shop" });
  });

  it("counts kinds in a fixed order and filters by kind and by failure", () => {
    expect(kindCounts(rows)).toEqual([{ kind: "shell", n: 2 }, { kind: "git", n: 1 }]);
    expect(filterActions(rows, "git", false).map((r) => r.title)).toEqual(["git status"]);
    expect(filterActions(rows, "all", true).map((r) => r.title)).toEqual(["npm test"]);
    expect(filterActions(rows, "git", true)).toEqual([]);
  });
});

describe("a staff member's feed", () => {
  it("is every tool of every turn, newest first; an unanswered tool of the open turn is running", () => {
    const turn = (index: number, tools: StaffTurn["tools"], ended = true): StaffTurn => ({ index, role: "assistant", text: "", tools, started_at: `2026-11-01T10:0${index}:00Z`, ended_at: ended ? "x" : "", usage: null });
    const rows = turnActions([
      turn(0, [{ name: "Read", summary: "src/app.py", ok: true }, { name: "Bash", summary: "npm test", ok: false }]),
      turn(1, [{ name: "Edit", summary: "src/app.py", ok: null }], false),
    ]);
    expect(rows.map((r) => [r.kind, r.title, r.failed, r.running])).toEqual([
      ["edits", "src/app.py", false, true],
      ["shell", "npm test", true, false],
      ["reads", "src/app.py", false, false],
    ]);
  });
});

describe("putting a terminal into a place", () => {
  it("lands it there, trades places with where it was, and gives that place the keyboard", () => {
    const ws = normalise({ layout: 4, panes: ["a", "b", "c", null], active: 0 });
    expect(assign(ws, 3, "x")).toMatchObject({ panes: ["a", "b", "c", "x"], active: 3 });
    expect(assign(ws, 1, "c")).toMatchObject({ panes: ["a", "c", "b", null], active: 1 });
    expect(assign(ws, 9, "x")).toBe(ws);
    expect(assign({ ...ws, zoom: 2 }, 0, "x").zoom).toBeNull();
  });
});

describe("restoring a layout", () => {
  const saved: Pick<TerminalLayout, "workspace" | "terminals"> = {
    workspace: { layout: 4, panes: ["a", "gone", null, "ended"], col: 0.3, row: 0.6, active: 1 },
    terminals: {
      a: { env: "container", cwd: "/work/a", title: "bash a", project_id: "p1", owner_kind: "project", owner_id: "p1", profile: "shell", sandbox: false },
      ended: { env: "host", cwd: "/home/someone", title: "htop", project_id: null, owner_kind: "free", owner_id: null, profile: "shell", sandbox: false },
    },
  };

  it("keeps the running terminals and the splitters, and leaves the others' places empty and named", () => {
    const { ws, missing } = restoreLayout(saved, new Set(["a"]));
    expect(ws).toEqual({ ...EMPTY, layout: 4, panes: ["a", null, null, null], col: 0.3, row: 0.6, active: 1 });
    expect(missing.map((m) => [m.slot, m.id, m.info?.title ?? null])).toEqual([[1, "gone", null], [3, "ended", "htop"]]);
  });

  it("restores a whole layout when every terminal still runs", () => {
    expect(restoreLayout(saved, new Set(["a", "gone", "ended"])).missing).toEqual([]);
  });

  it("offers to reopen a plain shell only, never a staff member's CLI", () => {
    const shell = saved.terminals.ended;
    expect(reopenable(shell)).toBe(true);
    expect(reopenable(null)).toBe(false);
    expect(reopenable({ ...shell, owner_kind: "staff" })).toBe(false);
    expect(reopenable({ ...shell, profile: "claude" })).toBe(false);
  });
});
