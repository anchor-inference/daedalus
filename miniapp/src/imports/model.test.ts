import { beforeEach, describe, expect, it } from "vitest";
import { setLang } from "../i18n";
import { buildTurns } from "../turns";
import type { MessageView } from "../api";
import {
  baseName, canLand, cardSessions, crumbs, destinationLine, exchanges, firstHarness, foreignToolName, groupByFolder, harnessMeta, harnessOrder,
  importBody, importedId, isLargePreview, isLargeSession, jobStages, markQuery, offersProject, originMessages, parentOf, previewUrl, progress,
  mergeScan, scanUrl, sessionMeta, shortId, shortPath, snippetParts, splitTrivial, stageLine, suggestedMode, windowShare,
  type ForeignSession, type HarnessView, type ImportJob, type ImportPreview,
} from "./model";

const HOME = "/home/someone";

function session(over: Partial<ForeignSession> = {}): ForeignSession {
  return {
    harness: "claude", id: "a1f3c9e2-0000-4000-8000-000000004d17", cwd: `${HOME}/projects/smart-home`, title: "Rebind the lock",
    started_at: "2026-10-09T10:00:00Z", updated_at: "2026-10-09T12:00:00Z", messages: 214, bytes: 1_800_000, flags: {}, ...over,
  };
}

function preview(over: Partial<ImportPreview> = {}): ImportPreview {
  return {
    header: session(), harness_name: "Claude Code", first: [{ role: "user", text: "fix the lock" }, { role: "assistant", text: "looking" }],
    last: [{ role: "user", text: "and the automation" }, { role: "assistant", text: "done" }], complete: true,
    counts: { turns: 400, user: 60, assistant: 154, tool_calls: 171, sidechains: 2, compactions: 0 }, messages: 214, tokens: 96_000, window: 200_000,
    suggested_mode: "full", destination: { kind: "project", cwd: `${HOME}/projects/smart-home`, project: { id: "p-home", name: "Smart home", ephemeral: false } },
    model: { source: "claude-opus-5", preset: "opus", label: "Claude Opus 5", same: true, window: 200_000 },
    models: [{ id: "opus", label: "Claude Opus 5", window: 200_000 }, { id: "small", label: "Small", window: 32_000 }], ...over,
  };
}

beforeEach(() => setLang("en"));

describe("paths", () => {
  it("shortens the home to a tilde and keeps the rest", () => {
    expect(shortPath(`${HOME}/projects/x/`, HOME)).toBe("~/projects/x");
    expect(shortPath(HOME, HOME)).toBe("~");
    expect(shortPath("/srv/data", HOME)).toBe("/srv/data");
    expect(shortPath(`${HOME}x/y`, HOME)).toBe(`${HOME}x/y`);
  });

  it("builds crumbs that lead back to each folder", () => {
    expect(crumbs(`${HOME}/projects/smart-home`, HOME)).toEqual([
      { name: "~", path: HOME }, { name: "projects", path: `${HOME}/projects` }, { name: "smart-home", path: `${HOME}/projects/smart-home` },
    ]);
    expect(crumbs("/srv/a", HOME).map((c) => c.path)).toEqual(["/", "/srv", "/srv/a"]);
    expect(crumbs("C:\\Users\\you\\p").map((c) => c.name)).toEqual(["C:", "Users", "you", "p"]);
    expect(crumbs("", HOME)).toEqual([{ name: "~", path: HOME }]);
  });

  it("knows the folder above and stops at a root", () => {
    expect(parentOf("/srv/a/b")).toBe("/srv/a");
    expect(parentOf("/srv")).toBe("/");
    expect(parentOf("/")).toBeNull();
    expect(parentOf("C:/Users")).toBe("C:/");
    expect(parentOf("C:/")).toBeNull();
    expect(baseName("/srv/a/b/")).toBe("b");
  });
});

describe("the strip", () => {
  const list: HarnessView[] = [
    { id: "cursor", name: "Cursor", found: false, sessions: 0, folders: 0 },
    { id: "gemini", name: "Gemini CLI", found: true, sessions: 0, folders: 0 },
    { id: "claude", name: "Claude Code", found: true, sessions: 412, folders: 23 },
  ];

  it("puts the programs with sessions first and the missing ones last", () => {
    expect(harnessOrder(list).map((h) => h.id)).toEqual(["claude", "gemini", "cursor"]);
  });

  it("opens on the program asked for, else the first with sessions", () => {
    expect(firstHarness(list)).toBe("claude");
    expect(firstHarness(list, "gemini")).toBe("gemini");
    expect(firstHarness(list, "nope")).toBe("claude");
  });

  it("gives an unknown program its own name and a neutral tile", () => {
    expect(harnessMeta("claude").mark).toBe("CC");
    expect(harnessMeta("claude", "Claude Code 2").name).toBe("Claude Code 2");
    expect(harnessMeta("warp")).toEqual({ name: "warp", mark: "WA", color: "var(--staff-slate)" });
  });
});

describe("the list", () => {
  it("groups a search's matches by folder in the host's order", () => {
    const groups = groupByFolder([session({ id: "1", cwd: "/a" }), session({ id: "2", cwd: "/b/" }), session({ id: "3", cwd: "/a" })]);
    expect(groups.map((g) => [g.cwd, g.sessions.map((s) => s.id)])).toEqual([["/a", ["1", "3"]], ["/b", ["2"]]]);
  });

  it("folds the bookkeeping sessions away", () => {
    const { main, trivial } = splitTrivial([session({ id: "a" }), session({ id: "b", title: "/init", messages: 6 }), session({ id: "c", messages: 1 })]);
    expect(main.map((s) => s.id)).toEqual(["a"]);
    expect(trivial.map((s) => s.id)).toEqual(["b", "c"]);
  });

  it("says a session's id, branch, counts and size on one line", () => {
    expect(sessionMeta(session({ branch: "main", flags: { compacted: 3, sidechains: 2 } }))).toEqual(["a1f3c9e2", "main", "214 msgs.", "3 compactions", "2 subagents", "1.7 MB"]);
  });

  it("tells time-ordered ids apart by their end", () => {
    expect(shortId("019d1234-abcd-7f30-8000-00000000a1e4")).toBe("019d…a1e4");
    expect(shortId("ses_0123456789abcdef")).toBe("ses_01…cdef");
    expect(shortId("short")).toBe("short");
  });

  it("marks a search's matches", () => {
    expect(snippetParts("…the lock in the hall", "LOCK").filter((p) => p.hit).map((p) => p.text)).toEqual(["lock"]);
    expect(snippetParts("…the [[lock]] in the [[hall]]")).toEqual([
      { text: "…the ", hit: false }, { text: "lock", hit: true }, { text: " in the ", hit: false }, { text: "hall", hit: true },
    ]);
    expect(markQuery("Lock and LOCK", "lock").filter((p) => p.hit).map((p) => p.text)).toEqual(["Lock", "LOCK"]);
    expect(markQuery("Nothing", "")).toEqual([{ text: "Nothing", hit: false }]);
  });

  it("warns of a large session by size or by length", () => {
    expect(isLargeSession({ bytes: 38 * 1024 * 1024, messages: 10 })).toBe(true);
    expect(isLargeSession({ bytes: 1000, messages: 3412 })).toBe(true);
    expect(isLargeSession({ bytes: 1000, messages: 20 })).toBe(false);
  });
});

describe("the preview", () => {
  it("applies the host's rule against the model chosen here", () => {
    expect(isLargePreview(preview())).toBe(false);
    expect(isLargePreview(preview(), "small")).toBe(true);
    expect(isLargePreview(preview({ messages: 3412 }))).toBe(true);
    expect(windowShare(preview())).toBe(48);
    expect(windowShare(preview(), "small")).toBe(300);
    expect(windowShare(preview({ window: 0 }))).toBeNull();
    expect(suggestedMode(preview())).toBe("full");
    expect(suggestedMode(preview({ suggested_mode: "tail" }), "opus")).toBe("tail");
    expect(suggestedMode(preview(), "small")).toBe("tail");
  });

  it("quotes the operator's first request and the model's last answer", () => {
    expect(exchanges(preview())).toEqual({ first: { role: "user", text: "fix the lock" }, last: { role: "assistant", text: "done" } });
    expect(exchanges(preview({ last: [] })).last).toBeNull();
  });

  it("says where the session lands", () => {
    expect(destinationLine(preview(), false, HOME)).toEqual({ title: "Into the project «Smart home»", sub: "this folder is already a project" });
    const inside = preview({ destination: { kind: "project", cwd: `${HOME}/projects/smart-home/ha`, worktree_cwd: `${HOME}/projects/smart-home/ha`, project: { id: "p-home", name: "Smart home" } } });
    expect(destinationLine(inside, false, HOME).sub).toBe("~/projects/smart-home/ha is inside the project's folder; commands run there");
    const chat = preview({ destination: { kind: "chat", cwd: `${HOME}/x`, project: { id: "c1", name: "Lock notes", ephemeral: true }, becomes_project: true } });
    expect(destinationLine(chat, false, HOME).title).toBe("Beside the chat «Lock notes»");
    const fresh = preview({ destination: { kind: "new_chat", cwd: `${HOME}/projects/esp32-door` } });
    expect(destinationLine(fresh, false, HOME)).toEqual({ title: "A new chat on the host", sub: "~/projects/esp32-door · nothing in Daedalus here yet" });
    expect(destinationLine(fresh, true, HOME).title).toBe("A new project «esp32-door»");
    const refused = preview({ destination: { kind: "refused", cwd: "/gone", code: "missing", problem: "/gone is not on the machine any more" } });
    expect(destinationLine(refused, false, HOME)).toEqual({ title: "It cannot land here", sub: "/gone is not on the machine any more" });
    expect(canLand(refused)).toBe(false);
    expect(canLand(fresh)).toBe(true);
  });

  it("offers to make a project only where a chat of its own would start", () => {
    expect(offersProject(preview())).toBe(false);
    expect(offersProject(preview({ destination: { kind: "new_chat", cwd: "/x" } }))).toBe(true);
    expect(offersProject(preview({ destination: { kind: "chat", cwd: "/x", project: { id: "c", name: "chat", ephemeral: true } } }))).toBe(false);
  });

  it("sends what the operator chose, and only what applies", () => {
    expect(importBody(preview(), "tail", "opus", true)).toEqual({ harness: "claude", id: session().id, mode: "tail", model: "opus", project_id: "p-home" });
    expect(importBody(preview({ destination: { kind: "new_chat", cwd: "/x" } }), "full", "", true, true)).toEqual({ harness: "claude", id: session().id, mode: "full", make_project: true, again: true });
  });

  it("asks the host in the shapes it answers", () => {
    expect(scanUrl("claude", "/a b", "", true)).toBe("/api/imports/scan?harness=claude&path=%2Fa+b");
    expect(scanUrl("claude", "", " lock ", true)).toBe("/api/imports/scan?harness=claude&path=&q=lock&deep=1");
    expect(scanUrl("claude", "/a", "", false, "17")).toBe("/api/imports/scan?harness=claude&path=%2Fa&cursor=17");
    expect(previewUrl("codex", "019d")).toBe("/api/imports/preview?harness=codex&id=019d");
  });

  it("appends the page after a truncated scan, never listing a session twice", () => {
    const a = session({ id: "a" });
    const b = session({ id: "b" });
    const first = { path: "/p", here: [a], children: [{ path: "/p/x", sessions: 1 }], folders: [], truncated: true, cursor: "1" };
    const merged = mergeScan(first, { path: "/p", here: [a, b], children: [], folders: [], truncated: false, cursor: "" });
    expect(merged.here.map((s) => s.id)).toEqual(["a", "b"]);
    expect(merged.children).toEqual(first.children);
    expect(merged.truncated).toBe(false);
  });
});

describe("the progress", () => {
  const job = (over: Partial<ImportJob> = {}): ImportJob => ({ job_id: "j1", state: "running", stage: "write", counts: { turns_read: 400, turns_total: 400, messages: 214, masked: 3, written: 132 }, ...over });

  it("lists the stages before the job's as done, its own as running, the summary only for a tail", () => {
    expect(jobStages(job(), "full").map((s) => `${s.id}:${s.state}`)).toEqual(["read:done", "parse:done", "mask:done", "write:now", "index:todo", "open:todo"]);
    expect(jobStages(job(), "tail").map((s) => s.id)).toContain("summarise");
    expect(jobStages(job({ state: "done", stage: "open" }), "full").every((s) => s.state === "done")).toBe(true);
    expect(jobStages(job({ state: "failed" }), "full")[3].state).toBe("failed");
  });

  it("counts the writing stage by its own numbers", () => {
    expect(progress(job(), "full")).toBe(Math.round((100 * (3 + 132 / 214)) / 6));
    expect(progress(job({ state: "done", stage: "open" }), "full")).toBe(100);
    expect(progress(job({ stage: "read", counts: {} }), "full")).toBe(0);
  });

  it("names each stage with its numbers", () => {
    const [read, parse, mask, write] = jobStages(job(), "full");
    const counts = job().counts;
    expect(stageLine(read, counts)).toEqual({ text: "Read on the host", aside: "400 records" });
    expect(stageLine(parse, counts).text).toBe("Parsed: 214 messages");
    expect(stageLine(mask, counts).text).toBe("Secrets hidden: 3");
    expect(stageLine(write, counts).aside).toBe("132 / 214");
    expect(stageLine({ id: "open", state: "todo" }, counts, "Smart home").text).toBe("Opening in Smart home");
    setLang("ru");
    expect(stageLine(parse, counts).text).toBe("Разобрано: 214 сообщений");
  });
});

describe("the start screen's card", () => {
  const now = Date.parse("2026-10-09T12:30:00Z");

  it("offers three fresh sessions, never one imported, running or older than the project's own activity", () => {
    const found = [
      session({ id: "1", updated_at: "2026-10-09T12:00:00Z" }),
      session({ id: "2", imported_as: { session_id: "s9", title: "x" } }),
      session({ id: "3", updated_at: "2026-10-09T11:00:00Z", project: { id: "p-home", name: "Smart home", kind: "project" } }),
      session({ id: "4", flags: { live: true } }),
      session({ id: "5", updated_at: "2026-10-01T11:00:00Z" }),
      session({ id: "6", updated_at: "2026-10-09T10:00:00Z" }),
      session({ id: "7", updated_at: "2026-10-09T09:00:00Z" }),
    ];
    expect(cardSessions(found, {}, now).map((s) => s.id)).toEqual(["1", "3", "6"]);
    expect(cardSessions(found, { "p-home": "2026-10-09T11:30:00Z" }, now).map((s) => s.id)).toEqual(["1", "6", "7"]);
    expect(cardSessions(undefined)).toEqual([]);
  });

  it("knows an imported session by the scan's object or its flag", () => {
    expect(importedId(session({ imported_as: { session_id: "s1", title: "x" } }))).toBe("s1");
    expect(importedId(session({ flags: { imported_as: "s2" } }))).toBe("s2");
    expect(importedId(session())).toBeNull();
    expect(originMessages({ counts: { user: 60, assistant: 154, tool_calls: 9 } })).toBe(214);
  });
});

describe("an imported chat", () => {
  const message = (over: Partial<MessageView>): MessageView => ({ role: "assistant", text: "", thinking: "", tool_calls: [], tool_results: [], created_at: "2026-10-09T10:00:00Z", ...over });

  it("keeps a foreign call's own name and marks the turns that came over", () => {
    const messages: MessageView[] = [
      message({ role: "user", text: "fix the test", origin: "operator", seq: 1, imported: { harness: "claude" } }),
      message({ seq: 2, tool_calls: [{ id: "t1", name: "Exec", arguments: { command: "pytest -q" } }], imported: { harness: "claude", tools: { t1: "Bash" } }, created_at: "2026-10-09T10:00:01Z" }),
      message({ role: "tool", seq: 3, tool_results: [{ id: "t1", content: "1 failed", is_error: false }], created_at: "2026-10-09T10:00:02Z" }),
      message({ seq: 4, text: "Fixed.", imported: { harness: "claude" }, created_at: "2026-10-09T10:00:03Z" }),
      message({ role: "user", text: "now run it", origin: "operator", seq: 5, created_at: "2026-10-09T11:00:00Z" }),
      message({ seq: 6, text: "Done.", created_at: "2026-10-09T11:00:01Z" }),
    ];
    expect(foreignToolName(messages[1], "t1")).toBe("Bash");
    expect(foreignToolName(messages[5], "t1")).toBeNull();
    const turns = buildTurns(messages);
    expect(turns.map((turn) => turn.imported ?? "")).toEqual(["claude", ""]);
    const tool = turns[0].activity.find((a) => a.kind === "tool");
    expect(tool).toMatchObject({ name: "Exec", orig: "Bash", result: "1 failed" });
    // The same messages build the same turn objects, so the marker is part of what a turn is.
    expect(buildTurns(messages, turns)[0]).toBe(turns[0]);
  });
});
