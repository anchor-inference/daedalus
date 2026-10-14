import { describe, expect, it } from "vitest";
import type { QuestionOutcome, WaitingQuestion } from "../api";
import {
  DRAFT_KEEP_MS,
  EMPTY,
  answerOf,
  batchOf,
  choosePermission,
  draftOf,
  fateOf,
  fitDraft,
  fitDrafts,
  folds,
  isBlank,
  isReady,
  isSendKey,
  leaves,
  loadDrafts,
  permissionChoices,
  projectGroups,
  pruneDrafts,
  revisionOf,
  saveDrafts,
  sections,
  setText,
  stepOption,
  takesFiles,
  takesText,
  textRole,
  toggleOption,
  withFiles,
  type Drafts,
} from "./model";
import { answerPath, concerns, dismissPath, questionsKey } from "./routes";

function question(over: Partial<WaitingQuestion> = {}): WaitingQuestion {
  return {
    id: "ask-db", short_id: "qdb01", project_id: "p1", origin: "orchestrator", kind: "question", staff_id: null, task_id: null,
    title: "Database", heading: "Database", text: "Which database?", detail: {}, routed_to: "operator", suggestion: "",
    created_at: "2026-09-25T10:00:00Z", resolved_at: null, resolved_by: null, resolution: {},
    project_name: "Bakery", asker: "orchestrator", section: "questions", options: ["Postgres", "SQLite"], multi: false,
    host: false, always: false, urgent: false, ...over,
  };
}

const perm = question({ id: "ask-perm", kind: "permission", origin: "staff", section: "requests", title: "", heading: "Exec: npm publish", options: [], always: true });
const providers = question({ id: "ask-pay", title: "Providers", options: ["Stripe", "PayPal", "Cash"], multi: true });
const folder = question({ id: "ask-folder", kind: "folder", options: ["Add", "Don't add"] });

function memory(): Storage {
  const kept = new Map<string, string>();
  return { getItem: (k) => kept.get(k) ?? null, setItem: (k, v) => void kept.set(k, v), removeItem: (k) => void kept.delete(k), clear: () => kept.clear(), key: () => null, length: 0 };
}

describe("a draft", () => {
  it("chooses one option at a time, and the same option again clears it", () => {
    const q = question();
    let d = toggleOption(q, EMPTY, "Postgres", 1);
    expect(d.selected).toEqual(["Postgres"]);
    d = toggleOption(q, d, "SQLite", 2);
    expect(d.selected).toEqual(["SQLite"]);
    expect(toggleOption(q, d, "SQLite", 3).selected).toEqual([]);
    expect(toggleOption(q, d, "MySQL", 4)).toBe(d);
  });

  it("chooses several where the question allows, kept in the options' own order", () => {
    let d = toggleOption(providers, EMPTY, "Cash");
    d = toggleOption(providers, d, "Stripe");
    expect(d.selected).toEqual(["Stripe", "Cash"]);
    expect(toggleOption(providers, d, "Cash").selected).toEqual(["Stripe"]);
  });

  it("is an answer in words, or a note once an option is chosen", () => {
    const q = question();
    expect(textRole(q, EMPTY)).toBe("answer");
    const chosen = toggleOption(q, EMPTY, "Postgres");
    expect(textRole(q, chosen)).toBe("note");
    expect(answerOf(q, setText(chosen, "  keep SQLite for tests "))).toEqual({ ask_id: "ask-db", selected: ["Postgres"], note: "keep SQLite for tests" });
    expect(answerOf(q, setText(EMPTY, "MariaDB, we know it"))).toEqual({ ask_id: "ask-db", text: "MariaDB, we know it" });
  });

  it("is ready only when the host would take it", () => {
    const q = question();
    expect(isReady(q, undefined)).toBe(false);
    expect(isReady(q, setText(EMPTY, "   "))).toBe(false);
    expect(isReady(q, setText(EMPTY, "Postgres"))).toBe(true);
    // A question is always open to words, even one stored when an orchestrator could say "options only".
    const stored = question({ detail: { allow_free: false } });
    expect(takesText(stored, EMPTY)).toBe(true);
    expect(isReady(stored, setText(EMPTY, "Neither: MariaDB"))).toBe(true);
    expect(takesText(stored, toggleOption(stored, EMPTY, "SQLite"))).toBe(true);
    // A folder is one of its options, and never words.
    expect(takesText(folder, EMPTY)).toBe(false);
    expect(isReady(folder, toggleOption(folder, EMPTY, "Add"))).toBe(true);
    expect(answerOf(folder, setText(toggleOption(folder, EMPTY, "Add"), "please"))).toEqual({ ask_id: "ask-folder", selected: ["Add"] });
  });

  it("answers a permission with its four choices, and No, because… needs the because", () => {
    expect(takesText(perm, EMPTY)).toBe(false);
    const allow = choosePermission(EMPTY, "allow");
    expect(answerOf(perm, allow)).toEqual({ ask_id: "ask-perm", allow: true });
    expect(answerOf(perm, choosePermission(EMPTY, "always"))).toEqual({ ask_id: "ask-perm", allow: true, always: true });
    expect(answerOf(perm, choosePermission(EMPTY, "server"))).toEqual({ ask_id: "ask-perm", allow: true, always: true, server: true });
    expect(answerOf(perm, choosePermission(EMPTY, "deny"))).toEqual({ ask_id: "ask-perm", allow: false });
    const because = choosePermission(EMPTY, "because");
    expect(takesText(perm, because)).toBe(true);
    expect(isReady(perm, because)).toBe(false);
    expect(answerOf(perm, setText(because, "not before the review"))).toEqual({ ask_id: "ask-perm", allow: false, note: "not before the review" });
    expect(choosePermission(allow, "allow").choice).toBeUndefined();
  });

  it("is blank when nothing is chosen or written", () => {
    expect(isBlank(undefined)).toBe(true);
    expect(isBlank(setText(EMPTY, "  "))).toBe(true);
    expect(isBlank(choosePermission(EMPTY, "deny"))).toBe(false);
  });
});

describe("a send", () => {
  it("carries every ready draft, in list order, and nothing half-done", () => {
    const list = [perm, question(), providers, folder];
    const drafts: Drafts = {
      "ask-db": toggleOption(question(), EMPTY, "SQLite"),
      "ask-pay": setText(EMPTY, "a note with no option"),
      "ask-perm": choosePermission(EMPTY, "deny"),
      "ask-gone": setText(EMPTY, "for a question no longer listed"),
    };
    expect(batchOf(list, drafts)).toEqual([{ ask_id: "ask-perm", allow: false }, { ask_id: "ask-db", selected: ["SQLite"] }, { ask_id: "ask-pay", text: "a note with no option" }]);
  });

  it("reads the host's outcome of each item as the card's fate", () => {
    const line = () => "answered in Telegram: Friday";
    const o = (over: Partial<QuestionOutcome>): QuestionOutcome => ({ ask_id: "a", state: "answered", ...over });
    expect(fateOf(o({ delivered: true }), line)).toEqual({ kind: "sent" });
    expect(fateOf(o({ applied: false, delivered: false }), line)).toEqual({ kind: "stale" });
    expect(fateOf(o({ delivered: false, error: "already has the folder" }), line)).toEqual({ kind: "sent", failed: "already has the folder" });
    const conflict = fateOf(o({ state: "conflict", answered_by: "operator", ask: question({ resolved_by: "operator" }) }), line);
    expect(conflict).toEqual({ kind: "conflict", by: "operator", line: "answered in Telegram: Friday" });
    expect(fateOf(o({ state: "conflict", withdrawn: true, ask: question({ resolution: { closed: "not needed" } }) }), line)).toEqual({ kind: "withdrawn", reason: "not needed", hadDraft: true });
    expect(fateOf(o({ state: "refused", error: "choose one option" }), line)).toEqual({ kind: "refused", error: "choose one option" });
    expect(leaves({ kind: "sent" })).toBe(true);
    expect(leaves({ kind: "conflict", by: "operator", line: "" })).toBe(false);
    expect(leaves({ kind: "refused", error: "x" })).toBe(false);
  });
});

describe("drafts on this device", () => {
  it("survive a reload, and a malformed or stale one is skipped", () => {
    const storage = memory();
    const now = Date.parse("2026-09-25T12:00:00Z");
    saveDrafts({ "ask-db": { ...toggleOption(question(), EMPTY, "Postgres", now), text: "note", project: "p1" }, "ask-empty": EMPTY }, storage);
    expect(loadDrafts(storage, now)).toEqual({ "ask-db": { selected: ["Postgres"], text: "note", at: now, project: "p1" } });
    storage.setItem("daedalus.questions.drafts", JSON.stringify({ a: { selected: "x", text: 1 }, b: { selected: [], text: "old", at: now - DRAFT_KEEP_MS - 1 }, c: { selected: [], text: "c", at: now, choice: "maybe" } }));
    expect(loadDrafts(storage, now)).toEqual({ c: { selected: [], text: "c", at: now, project: null } });
    storage.setItem("daedalus.questions.drafts", "{not json");
    expect(loadDrafts(storage, now)).toEqual({});
    expect(loadDrafts(null, now)).toEqual({});
  });

  it("are pruned by the list they belong to, never by another project's", () => {
    const drafts: Drafts = { a: { ...setText(EMPTY, "x"), project: "p1" }, b: { ...setText(EMPTY, "y"), project: "p2" }, c: { ...setText(EMPTY, "z"), project: "p1" } };
    const pruned = pruneDrafts(drafts, ["a"], (d) => d.project === "p1");
    expect(Object.keys(pruned)).toEqual(["a", "b"]);
    expect(pruneDrafts(drafts, ["a", "b", "c"], () => true)).toBe(drafts);
  });
});

describe("the list", () => {
  it("puts staff who wait above the orchestrator's questions, oldest first", () => {
    const later = question({ id: "q2", created_at: "2026-09-25T11:00:00Z" });
    const s = sections([later, question(), perm]);
    expect(s.map((x) => [x.key, x.items.map((q) => q.id)])).toEqual([["requests", ["ask-perm"]], ["questions", ["ask-db", "q2"]]]);
  });

  it("groups the main chat's list by project, a new project's confirmation on its own", () => {
    const garden = question({ id: "g", project_id: "p2", project_name: "Garden", created_at: "2026-09-25T09:00:00Z" });
    const fresh = question({ id: "n", project_id: null, project_name: "", origin: "dispatcher", kind: "project" });
    expect(projectGroups([question(), garden, fresh]).map((g) => [g.name, g.items.map((q) => q.id)])).toEqual([["Garden", ["g"]], ["Bakery", ["ask-db"]], ["", ["n"]]]);
  });

  it("folds a long text, by its lines or by one long paragraph", () => {
    expect(folds("one\ntwo\nthree")).toBe(false);
    expect(folds("1\n2\n3\n4\n5\n6\n7")).toBe(true);
    expect(folds("x".repeat(64 * 7))).toBe(true);
  });

  it("moves between options with the arrows, round the list, and sends on Ctrl or ⌘ with Enter", () => {
    expect(stepOption("ArrowRight", 2, 3)).toBe(0);
    expect(stepOption("ArrowUp", 0, 3)).toBe(2);
    expect(stepOption("End", 0, 3)).toBe(2);
    expect(stepOption("a", 0, 3)).toBeNull();
    const key = (over: Partial<KeyboardEvent>) => ({ key: "Enter", ctrlKey: false, metaKey: false, altKey: false, shiftKey: false, isComposing: false, keyCode: 0, ...over });
    expect(isSendKey(key({ ctrlKey: true }))).toBe(true);
    expect(isSendKey(key({ metaKey: true }))).toBe(true);
    expect(isSendKey(key({}))).toBe(false);
    expect(isSendKey(key({ ctrlKey: true, altKey: true }))).toBe(false);
    expect(isSendKey(key({ ctrlKey: true, isComposing: true }))).toBe(false);
    expect(isSendKey(key({ metaKey: true, isComposing: true }))).toBe(false);
    expect(isSendKey(key({ ctrlKey: true, keyCode: 229 }))).toBe(false);
    expect(isSendKey(key({ metaKey: true, keyCode: 229 }))).toBe(false);
  });
});

describe("the answers a permission card offers", () => {
  it("adds always for a whole MCP server only where the host names one", () => {
    expect(permissionChoices({ always: true, always_server: "daedalus_browser" })).toEqual(["allow", "always", "server", "deny", "because"]);
    expect(permissionChoices({ always: true, always_server: "" })).toEqual(["allow", "always", "deny", "because"]);
    expect(permissionChoices({ always: true })).toEqual(["allow", "always", "deny", "because"]);
    expect(permissionChoices({ always: false, always_server: "daedalus_browser" })).toEqual(["allow", "deny", "because"]);
  });
});

describe("a question reworded in place", () => {
  it("is marked with its revision and time, and a question never changed is not", () => {
    expect(revisionOf(question())).toBeNull();
    expect(revisionOf(question({ detail: { revision: 0 } }))).toBeNull();
    expect(revisionOf(question({ detail: { revision: 2, updated_at: "2026-09-25T10:05:00Z" } }))).toEqual({ revision: 2, at: "2026-09-25T10:05:00Z" });
    expect(revisionOf(question({ detail: { revision: 1 } }))).toEqual({ revision: 1, at: "" });
  });

  it("keeps the draft under the same id, dropping only a chosen option the new words no longer offer", () => {
    const before = { selected: ["SQLite"], text: "small is fine", at: 1, project: "p1" };
    const reworded = question({ options: ["Postgres", "DuckDB"], detail: { revision: 1 } });
    expect(fitDraft(reworded, before)).toEqual({ ...before, selected: [] });
    const kept = question({ options: ["Postgres", "SQLite", "DuckDB"] });
    expect(fitDraft(kept, before)).toBe(before);
    const drafts: Drafts = { [reworded.id]: before, other: { selected: ["x"], text: "", at: 1 } };
    const fitted = fitDrafts(drafts, [reworded]);
    expect(fitted[reworded.id].text).toBe("small is fine");
    expect(fitted.other).toBe(drafts.other);
    expect(fitDrafts(drafts, [kept])).toBe(drafts);
  });

  it("leaves a permission's choice alone, since it has no options to lose", () => {
    const d = { selected: [], text: "", at: 1, choice: "allow" as const };
    expect(fitDraft(perm, d)).toBe(d);
  });
});

describe("side questions and files with an answer", () => {
  const side = (over: Partial<WaitingQuestion> = {}) => question({ id: "ask-port", short_id: "qport", project_id: null, origin: "session", session_id: "s1", files: true, options: [], ...over });
  const png = () => new File(["x"], "screenshot.png", { type: "image/png" });

  it("takes files only for a question an agent asked, and files alone are an answer", () => {
    expect(takesFiles(side())).toBe(true);
    expect(takesFiles(question({ kind: "permission", files: false }))).toBe(false);
    expect(takesFiles(question({ files: undefined }))).toBe(false);
    expect(isReady(side(), undefined, 1)).toBe(true);
    expect(isReady(side(), undefined, 0)).toBe(false);
    expect(isReady(question({ kind: "permission", files: true }), undefined, 1)).toBe(false);
  });

  it("sends a card with only files, and numbers each answer's files among the batch's uploads", () => {
    const shot = png();
    const doc = new File(["y"], "notes.txt", { type: "text/plain" });
    const other = side({ id: "ask-font", short_id: "qfont" });
    const drafts: Drafts = { "ask-font": { ...EMPTY, text: "Inter", at: 1 } };
    const batch = batchOf([side(), other], drafts, { "ask-port": [shot, doc] });
    expect(batch).toEqual([{ ask_id: "ask-port", text: "" }, { ask_id: "ask-font", text: "Inter" }]);
    const { items, uploads } = withFiles(batch, { "ask-port": [shot, doc], "ask-font": [png()] });
    expect(items).toEqual([{ ask_id: "ask-port", text: "", files: [0, 1] }, { ask_id: "ask-font", text: "Inter", files: [2] }]);
    expect(uploads.map((f) => f.name)).toEqual(["screenshot.png", "notes.txt", "screenshot.png"]);
    expect(withFiles(batch, {}).uploads).toEqual([]);
  });

  it("keeps each list's drafts to itself: a session's, a project's and the main chat's", () => {
    const sessionDraft = { ...EMPTY, text: "x", project: null, session: "s1" };
    const projectDraft = { ...EMPTY, text: "y", project: "p1" };
    expect(draftOf({ sessionId: "s1" }, sessionDraft)).toBe(true);
    expect(draftOf({ sessionId: "s2" }, sessionDraft)).toBe(false);
    expect(draftOf("all", sessionDraft)).toBe(false);
    expect(draftOf("all", projectDraft)).toBe(true);
    expect(draftOf({ projectId: "p1" }, projectDraft)).toBe(true);
    expect(draftOf({ projectId: "p1" }, { ...sessionDraft, project: "p1" })).toBe(false);
    const store = new Map<string, string>();
    const storage = { getItem: (k: string) => store.get(k) ?? null, setItem: (k: string, v: string) => void store.set(k, v) };
    saveDrafts({ a: { ...sessionDraft, at: Date.now() } }, storage);
    expect(loadDrafts(storage).a.session).toBe("s1");
  });

  it("reads, answers and puts aside a session's list at its own routes, and redraws only on its own events", () => {
    expect(questionsKey({ sessionId: "s 1" })).toBe("/api/questions?session=s%201");
    expect(answerPath({ sessionId: "s1" })).toBe("/api/sessions/s1/asks/answer");
    expect(dismissPath({ sessionId: "s1" }, "ask-1")).toBe("/api/sessions/s1/asks/ask-1/dismiss");
    expect(dismissPath({ projectId: "p1" }, "ask-1")).toBeNull();
    expect(concerns({ sessionId: "s1" }, { type: "ask.side", session_id: "s1" })).toBe(true);
    expect(concerns({ sessionId: "s1" }, { type: "ask.side", session_id: "s2" })).toBe(false);
    expect(concerns({ sessionId: "s1" }, { type: "ask.pending", project_id: "p1" })).toBe(false);
    expect(concerns({ projectId: "p1" }, { type: "ask.side", session_id: "s1" })).toBe(false);
    expect(concerns({ projectId: "p1" }, { type: "ask.pending", project_id: "p1" })).toBe(true);
    expect(concerns("all", { type: "ask.batch", project_id: "p2" })).toBe(true);
  });

  it("lets a card put aside leave the list like one answered elsewhere", () => {
    expect(leaves({ kind: "aside" })).toBe(true);
  });
});
