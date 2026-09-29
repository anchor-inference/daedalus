import { describe, expect, it } from "vitest";
import { HARNESSES, HARNESS_BADGES, availability, branchPreview, placeExecutor, branchSlug, colourVar, defaultIsolation, foldersFor, initials, statusTone } from "./team";

describe("the executor badge", () => {
  it("gives every executor its own two letters", () => {
    expect(HARNESSES.map((h) => HARNESS_BADGES[h])).toEqual(["D", "CC", "CX", "GK", "OC", "π"]);
    expect(new Set(Object.values(HARNESS_BADGES)).size).toBe(HARNESSES.length);
  });
});

describe("which executor can be hired", () => {
  it("always offers Daedalus", () => {
    expect(availability("daedalus", null)).toBe("");
  });

  it("reads a missing catalog, or a missing entry, as not installed", () => {
    expect(availability("claude", null)).toBe("notinstalled");
    expect(availability("claude", {})).toBe("notinstalled");
    expect(availability("codex", { codex: { installed: false } })).toBe("notinstalled");
  });

  it("says why an installed agent still cannot be chosen", () => {
    expect(availability("claude", { claude: { installed: true, logged_in: false } })).toBe("loggedout");
    expect(availability("grok", { grok: { installed: false, error: "exec failed" } })).toBe("error");
    expect(availability("claude", { claude: { installed: true, logged_in: true } })).toBe("");
    // A catalog that does not say whether it is signed in is not a reason to refuse it.
    expect(availability("pi", { pi: { installed: true } })).toBe("");
  });
});

describe("where an executor is offered", () => {
  const host = { claude: { installed: true, logged_in: true }, codex: { installed: true } };
  const container = { codex: { installed: true } };

  it("offers an agent installed only on the host while the sheet shows the container", () => {
    // The defect: with Daedalus picked the grid read the container's catalog alone, and Claude Code,
    // installed on the host, stayed "not installed" until Codex was clicked.
    expect(placeExecutor("claude", "container", { container, host })).toEqual({ env: "host", why: "", waiting: false });
  });

  it("keeps the preferred environment when the agent is there", () => {
    expect(placeExecutor("codex", "container", { container, host })).toEqual({ env: "container", why: "", waiting: false });
  });

  it("says not installed only when neither environment has it, and waits while one is still loading", () => {
    expect(placeExecutor("grok", "host", { container, host })).toEqual({ env: "host", why: "notinstalled", waiting: false });
    expect(placeExecutor("claude", "container", { container, host: undefined }).waiting).toBe(true);
  });
});

describe("the branch preview", () => {
  it("is the name the worktree's branch will carry", () => {
    expect(branchPreview("Ada", "<task>")).toBe("agent/ada/<task>");
    expect(branchSlug("Menu Page Writer")).toBe("menu-page-writer");
    expect(branchSlug("  --Ada.Lovelace_2--  ")).toBe("ada.lovelace_2");
    expect(branchSlug("a/b\\c:d")).toBe("a-b-c-d");
  });

  it("stays within its length and never goes empty", () => {
    expect(branchSlug("x".repeat(80)).length).toBe(32);
    expect(branchSlug("")).toBe("staff");
    expect(branchSlug("!!!")).toBe("staff");
  });

  // The same cases the host's worktree tests pin, so the two rules cannot drift apart unnoticed.
  it("transliterates and cleans the way the host does", () => {
    expect(branchSlug("Анна")).toBe("anna");
    expect(branchSlug("Щукин Ёж")).toBe("shchukin-ezh");
    expect(branchSlug("Café Noël")).toBe("cafe-noel");
    expect(branchSlug("a..b")).toBe("a.b");
    expect(branchSlug("release.lock")).toBe("release");
    expect(branchSlug("_under_")).toBe("_under_");
    expect(branchSlug(".-dots-.")).toBe("dots");
  });
});

describe("how a member is drawn", () => {
  it("colours the status dot the way sessions are coloured", () => {
    expect(statusTone("working")).toBe("running");
    expect(statusTone("starting")).toBe("running");
    expect(statusTone("question")).toBe("waiting");
    expect(statusTone("permission")).toBe("waiting");
    expect(statusTone("error")).toBe("failed");
    expect(statusTone("turn_done_unseen")).toBe("done");
    expect(statusTone("off")).toBe("idle");
    expect(statusTone("no_signal")).toBe("idle");
  });

  it("puts initials in the avatar", () => {
    expect(initials("Ada")).toBe("AD");
    expect(initials("ada lovelace")).toBe("AL");
    expect(initials("Ян")).toBe("ЯН");
    expect(initials(" ")).toBe("?");
  });

  it("keeps to the colour tokens the stylesheet has", () => {
    expect(colourVar("teal")).toBe("var(--staff-teal)");
    expect(colourVar("#ff0000")).toBe("var(--staff-blue)");
  });
});

describe("folders and isolation", () => {
  const folders = [
    { id: "f1", path: "/work/site", label: "", env: "container" as const, is_git: true, readonly: false },
    { id: "f2", path: "/work/docs", label: "docs", env: "container" as const, is_git: false, readonly: true },
    { id: "f3", path: "/home/you/site", label: "", env: "host" as const, is_git: true, readonly: false },
  ];

  it("offers a member only the folders of the environment it runs in", () => {
    expect(foldersFor(folders, "container").map((f) => f.id)).toEqual(["f1", "f2"]);
    expect(foldersFor(folders, "host").map((f) => f.id)).toEqual(["f3"]);
  });

  it("starts with an own worktree only where one can be made", () => {
    expect(defaultIsolation(folders[0])).toBe("worktree");
    expect(defaultIsolation(folders[1])).toBe("shared");
    expect(defaultIsolation(undefined)).toBe("shared");
  });
});
