import { describe, expect, it } from "vitest";
import type { Project, SessionSummary } from "../api";
import { setLang } from "../i18n";
import { acceleratorFrom, showAccelerator } from "./accelerator";
import {
  appshotError,
  composeMessage,
  DEFAULT_TARGET,
  computerGrant,
  computerSession,
  delivery,
  deskStatus,
  lastSession,
  mentionChoices,
  projectForNew,
  removeTrigger,
  skillChoices,
  trigger,
  type Companion,
} from "./quick";

function session(id: string, over: Partial<SessionSummary> = {}): SessionSummary {
  return { id, title: id, status: "idle", created_at: "2026-10-01T00:00:00Z", last_message_at: "2026-10-01T00:00:00Z", run_id: null, project_id: "p", project: "P", ...over };
}

const companion: Companion = { session_id: "c1", name: "Daedalus", avatar: { shape: "round", color: "teal", eyes: "dots", accessory: "" }, paused: false, state: "idle", running_agents: 0 };

describe("where Quick Chat sends", () => {
  it("calls the newest conversation the operator talks to the last one", () => {
    const list = [
      session("old", { last_message_at: "2026-10-10T00:00:00Z" }),
      session("helper", { last_message_at: "2026-10-13T00:00:00Z", metadata: { subagent_of: "old" } }),
      session("away", { last_message_at: "2026-10-13T00:00:00Z", archived: true }),
      session("c1", { last_message_at: "2026-10-13T09:00:00Z" }),
      session("new", { last_message_at: "2026-10-12T00:00:00Z" }),
    ];
    expect(lastSession(list, "c1")?.id).toBe("new");
    expect(lastSession([], "")).toBeNull();
  });

  it("defaults to a new chat, whether or not there is a companion or a last chat", () => {
    expect(DEFAULT_TARGET).toBe("new");
  });

  it("starts a Computer request in a new chat of kind computer, granted in the host's terms", () => {
    expect(computerSession("Fill in the table", [])).toEqual({ autotitle: true, title: "Fill in the table", kind: "computer" });
    expect(computerSession("x".repeat(100), [{ kind: "project", id: "p1", name: "Report" }])).toEqual({ autotitle: true, title: "x".repeat(80), kind: "computer", project_id: "p1" });
    expect(computerSession("", []).title).toBe("New computer chat");
    expect(computerGrant("s9", { kind: "display", display: "2" }, 30)).toEqual({ session_id: "s9", scope: { kind: "display", display: "2" }, minutes: 30 });
  });

  it("sends words to the companion's endpoint and a picture through its session's upload", () => {
    expect(delivery("companion", 0, companion, null)).toEqual({ kind: "companion" });
    expect(delivery("companion", 1, companion, null)).toEqual({ kind: "upload", session: "c1" });
    expect(delivery("companion", 0, null, null)).toBeNull();
    expect(delivery("last", 0, null, session("a"))).toEqual({ kind: "message", session: "a" });
    expect(delivery("last", 2, null, session("a"))).toEqual({ kind: "upload", session: "a" });
    expect(delivery("last", 0, null, null)).toBeNull();
    expect(delivery("new", 1, null, null)).toEqual({ kind: "new" });
  });
});

describe("@ and $", () => {
  it("open a list only at the start of a word", () => {
    expect(trigger("look at @rep", 12)).toEqual({ char: "@", query: "rep", start: 8 });
    expect(trigger("$", 1)).toEqual({ char: "$", query: "", start: 0 });
    expect(trigger("mail someone@example", 20)).toBeNull();
    expect(trigger("costs 5$", 8)).toBeNull();
    expect(trigger("@done and more", 14)).toBeNull();
    // The caret decides, not the end of the text.
    expect(trigger("see @pro here", 8)).toEqual({ char: "@", query: "pro", start: 4 });
  });

  it("take the completed word out and leave one space", () => {
    const found = trigger("look at @rep now", 12)!;
    expect(removeTrigger("look at @rep now", found, 12)).toEqual({ text: "look at now", caret: 8 });
    expect(removeTrigger("$web", trigger("$web", 4)!, 4)).toEqual({ text: "", caret: 0 });
  });

  it("offer conversations, projects and files, the best match first", () => {
    const projects = [{ id: "p1", name: "Website", created_at: "", settings: { snapshots: false } }, { id: "p2", name: "Old", created_at: "", settings: { snapshots: false, archived: true } }] as Project[];
    const sessions = [session("s1", { title: "Fix the web page" }), session("s2", { title: "Website copy" }), session("s3", { title: "web helper", metadata: { subagent_of: "s1" } })];
    const files = [{ path: "site/web.css", session: "s2", sessionTitle: "Website copy" }];
    const found = mentionChoices("web", sessions, projects, files);
    expect(found.map((item) => item.kind === "file" ? item.path : item.id)).toEqual(["s2", "s1", "p1", "site/web.css"]);
    expect(mentionChoices("", sessions, projects, [], 1).map((item) => item.kind)).toEqual(["session", "project"]);
  });

  it("match skills by name or description", () => {
    const skills = [{ id: "a", name: "web-design-reviewer", description: "Review a page" }, { id: "b", name: "pdf", description: "Read and write PDF files" }];
    expect(skillChoices("pdf", skills).map((s) => s.id)).toEqual(["b"]);
    expect(skillChoices("review", skills).map((s) => s.id)).toEqual(["a"]);
    expect(skillChoices("", skills)).toHaveLength(2);
  });
});

describe("the message the agent reads", () => {
  it("asks for the skill in words and lists the context by id", () => {
    setLang("en");
    const text = composeMessage("  why does it fail?  ", { id: "a", name: "debugger", description: "" }, [
      { kind: "session", id: "s1", title: "Deploy" },
      { kind: "project", id: "p1", name: "Website" },
      { kind: "file", path: "src/app.ts", session: "s2", sessionTitle: "Site" },
    ]);
    expect(text).toBe([
      "Use the debugger skill: why does it fail?",
      "",
      "Context:",
      "- the conversation “Deploy” (session s1)",
      "- the project “Website” (project p1)",
      "- the file src/app.ts in the conversation “Site” (session s2)",
    ].join("\n"));
    expect(composeMessage("hi", null, [])).toBe("hi");
  });

  it("starts a new conversation in the project @ named, when it named one", () => {
    expect(projectForNew([{ kind: "project", id: "p1", name: "W" }, { kind: "session", id: "s", title: "x" }])).toBe("p1");
    expect(projectForNew([{ kind: "project", id: "p1", name: "W" }, { kind: "project", id: "p2", name: "V" }])).toBeUndefined();
    expect(projectForNew([])).toBeUndefined();
  });

  it("says why an appshot was not taken in the reader's language", () => {
    setLang("ru");
    expect(appshotError("cancelled")).toBe("Экран не предоставили.");
    expect(appshotError("weird", "no display")).toBe("Не удалось сделать снимок: no display");
    setLang("en");
    expect(appshotError("permission")).toMatch(/Screen Recording/);
  });
});

describe("the tray's status", () => {
  it("counts running conversations and what needs the operator", () => {
    const list = [session("a", { status: "running" }), session("b", { status: "compacting" }), session("c", { status: "waiting" }), session("d")];
    expect(deskStatus(list, { unseen: 4, needs_you: 2 }, null)).toEqual({ running: 2, waiting: 2, companion: null });
    expect(deskStatus(undefined, undefined, { ...companion, paused: true })).toEqual({ running: 0, waiting: 0, companion: { paused: true } });
  });
});

describe("shortcuts", () => {
  const press = (code: string, mods: Partial<{ alt: boolean; ctrl: boolean; meta: boolean; shift: boolean }> = {}) =>
    ({ code, altKey: !!mods.alt, ctrlKey: !!mods.ctrl, metaKey: !!mods.meta, shiftKey: !!mods.shift });

  it("are recorded from the physical key, whatever the layout types", () => {
    expect(acceleratorFrom(press("Space", { alt: true }), false)).toBe("Alt+Space");
    expect(acceleratorFrom(press("KeyK", { ctrl: true, shift: true }), false)).toBe("Ctrl+Shift+K");
    expect(acceleratorFrom(press("KeyK", { meta: true, alt: true }), true)).toBe("Command+Alt+K");
    expect(acceleratorFrom(press("KeyK", { meta: true }), false)).toBe("Super+K");
    expect(acceleratorFrom(press("Slash", { ctrl: true }), false)).toBe("Ctrl+/");
    expect(acceleratorFrom(press("F9"), false)).toBe("F9");
  });

  it("refuse a plain key and a modifier alone", () => {
    expect(acceleratorFrom(press("KeyK"), false)).toBeNull();
    expect(acceleratorFrom(press("AltLeft", { alt: true }), false)).toBeNull();
    expect(acceleratorFrom(press("IntlBackslash", { alt: true }), false)).toBeNull();
  });

  it("are shown the way each system writes them", () => {
    expect(showAccelerator("Alt+Space", true)).toBe("⌥ Space");
    expect(showAccelerator("Command+Shift+K", true)).toBe("⌘ ⇧ K");
    expect(showAccelerator("Alt+Shift+Space", false)).toBe("Alt+Shift+Space");
    expect(showAccelerator("CommandOrControl+K", false)).toBe("Ctrl+K");
    expect(showAccelerator("", false)).toBe("");
  });
});
