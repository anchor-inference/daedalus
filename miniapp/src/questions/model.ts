// What the Questions tab decides without React: what a half-answered card holds, when it is ready to
// send, what it sends, how the list is ordered and grouped, and what a card shows after the host
// answered the send. Kept pure so the rules the operator relies on — a draft survives a reload, a
// note is only a note beside an option, a permission's "No, because…" needs its because — are
// tested without a browser.

import type { WaitingQuestion, QuestionAnswer, QuestionOutcome } from "../api";

/** A permission's answers. "because" is a refusal with the operator's reason, which the member reads;
 *  "server" is "always" for every tool of the asked tool's MCP server, offered only where the host
 *  names one (`always_server`). */
export type PermissionChoice = "allow" | "always" | "server" | "deny" | "because";

/** Every answer a stored draft may hold, in the order the card offers them. */
export const PERMISSION_CHOICES: PermissionChoice[] = ["allow", "always", "server", "deny", "because"];

/** The answers this card offers: "Always" where the CLI has a standing grant, and the server-wide one
 *  only for a tool of an MCP server whose tools the CLI can allow together. */
export function permissionChoices(q: Pick<WaitingQuestion, "always" | "always_server">): PermissionChoice[] {
  return PERMISSION_CHOICES.filter((c) => (c !== "always" || q.always) && (c !== "server" || (q.always && !!q.always_server)));
}

/** What the operator has done to one card so far. `text` is the free answer while no option is chosen
 *  and the note once one is — the same field, read by what else is chosen. */
export type Draft = { selected: string[]; text: string; choice?: PermissionChoice; at: number; project?: string | null };

export type Drafts = Record<string, Draft>;

export const EMPTY: Draft = { selected: [], text: "", at: 0 };

/** Whether a card's kind takes a permission's four answers rather than options. */
export function isPermission(q: Pick<WaitingQuestion, "kind">): boolean {
  return q.kind === "permission";
}

/** A folder or a project is added or not: one of its options, and never words. */
export function optionsOnly(q: Pick<WaitingQuestion, "kind">): boolean {
  return q.kind === "folder" || q.kind === "project";
}

/** The field is shown for every question, always (the answer in the operator's own words, or a note
 *  beside an option), and for a permission refused with a reason. A question once came with its field
 *  switched off by the orchestrator, and the operator had no way to say that none of the options fit. */
export function takesText(q: WaitingQuestion, d: Draft): boolean {
  if (isPermission(q)) return d.choice === "because";
  return !optionsOnly(q);
}

/** Whether the field is a note beside a chosen option, or the answer itself. */
export function textRole(q: WaitingQuestion, d: Draft): "answer" | "note" | "reason" {
  if (isPermission(q)) return "reason";
  return d.selected.length > 0 ? "note" : "answer";
}

/** Choose or un-choose an option: one at a time, or several where the question allows. Choosing the
 *  chosen option again clears it, so a slip is undone where it was made. */
export function toggleOption(q: WaitingQuestion, d: Draft, option: string, now = Date.now()): Draft {
  if (!q.options.includes(option)) return d;
  const has = d.selected.includes(option);
  const selected = q.multi ? (has ? d.selected.filter((o) => o !== option) : q.options.filter((o) => o === option || d.selected.includes(o))) : has ? [] : [option];
  return { ...d, selected, at: now };
}

export function choosePermission(d: Draft, choice: PermissionChoice, now = Date.now()): Draft {
  return { ...d, choice: d.choice === choice ? undefined : choice, at: now };
}

export function setText(d: Draft, text: string, now = Date.now()): Draft {
  return { ...d, text, at: now };
}

/** Nothing chosen and nothing written: no draft at all. */
export function isBlank(d: Draft | undefined): boolean {
  return !d || (d.selected.length === 0 && !d.text.trim() && !d.choice);
}

/** Whether a card's draft is an answer the host will take. A note alone, or "No, because…" without the
 *  because, is not; words where only an option will do are not. */
export function isReady(q: WaitingQuestion, d: Draft | undefined): boolean {
  if (!d) return false;
  if (isPermission(q)) return !!d.choice && (d.choice !== "because" || !!d.text.trim());
  if (q.kind === "folder" || q.kind === "project") return d.selected.length === 1;
  if (d.selected.length > 0) return true;
  return !!d.text.trim();
}

/** What one ready card sends, in the shape of `POST …/asks/answer`. */
export function answerOf(q: WaitingQuestion, d: Draft): QuestionAnswer {
  const words = d.text.trim();
  if (isPermission(q)) {
    const allow = d.choice === "allow" || d.choice === "always" || d.choice === "server";
    const always = d.choice === "always" || d.choice === "server";
    return { ask_id: q.id, allow, ...(always ? { always: true } : {}), ...(d.choice === "server" ? { server: true } : {}), ...(d.choice === "because" && words ? { note: words } : {}) };
  }
  if (d.selected.length > 0) return { ask_id: q.id, selected: [...d.selected], ...(words && q.kind === "question" ? { note: words } : {}) };
  return { ask_id: q.id, text: words };
}

/** The answers a press of Send carries: every ready draft of a card still on the list, in list order. */
export function batchOf(questions: WaitingQuestion[], drafts: Drafts): QuestionAnswer[] {
  return questions.filter((q) => isReady(q, drafts[q.id])).map((q) => answerOf(q, drafts[q.id]));
}

/** Staff waiting on a permission or an escalated question come first: a member is stopped until the
 *  answer comes, and the orchestrator's own questions can wait a moment longer. Oldest first within each. */
export function sections(questions: WaitingQuestion[]): { key: "requests" | "questions"; items: WaitingQuestion[] }[] {
  const byAge = [...questions].sort((a, b) => a.created_at.localeCompare(b.created_at));
  return (["requests", "questions"] as const)
    .map((key) => ({ key, items: byAge.filter((q) => (q.section ?? (q.origin === "staff" ? "requests" : "questions")) === key) }))
    .filter((s) => s.items.length > 0);
}

/** The main chat's list: one group per project, the group waiting longest first. The main
 *  orchestrator's own confirmation of a project that does not exist yet is a group of its own. */
export function projectGroups(questions: WaitingQuestion[]): { key: string; projectId: string | null; name: string; items: WaitingQuestion[] }[] {
  const groups = new Map<string, { key: string; projectId: string | null; name: string; items: WaitingQuestion[] }>();
  for (const q of [...questions].sort((a, b) => a.created_at.localeCompare(b.created_at))) {
    const key = q.project_id ?? `new:${q.id}`;
    const group = groups.get(key) ?? { key, projectId: q.project_id, name: q.project_name, items: [] };
    group.items.push(q);
    groups.set(key, group);
  }
  return [...groups.values()];
}

/** When the orchestrator last reworded a question, and how many times: null for one never changed. A
 *  reworded question keeps its id, so the card, its place and the operator's draft all stay; this is
 *  what tells the operator the words under that draft are not the ones it was begun against. */
export function revisionOf(q: Pick<WaitingQuestion, "detail">): { revision: number; at: string } | null {
  const revision = Number(q.detail?.revision ?? 0);
  if (!Number.isFinite(revision) || revision < 1) return null;
  const at = typeof q.detail.updated_at === "string" ? q.detail.updated_at : "";
  return { revision, at };
}

/** A draft held against the question as it now stands. A rewording may drop an option the operator
 *  had chosen; sending that choice would answer with something the question no longer offers, so the
 *  choice goes and the words written beside it stay. The draft is returned unchanged when nothing
 *  it chose is gone, so a list that did not change re-renders nothing. */
export function fitDraft(q: Pick<WaitingQuestion, "kind" | "options">, d: Draft): Draft {
  if (isPermission(q) || d.selected.length === 0) return d;
  const selected = d.selected.filter((o) => q.options.includes(o));
  return selected.length === d.selected.length ? d : { ...d, selected };
}

/** Every draft of the listed questions fitted to them, the same object back when none changed. */
export function fitDrafts(drafts: Drafts, questions: WaitingQuestion[]): Drafts {
  let out: Drafts | null = null;
  for (const q of questions) {
    const d = drafts[q.id];
    if (!d) continue;
    const fitted = fitDraft(q, d);
    if (fitted !== d) (out ??= { ...drafts })[q.id] = fitted;
  }
  return out ?? drafts;
}

// ── the host's answer ────────────────────────────────────────────────────────────────────────

/** What a card shows after a send, or after the list lost it: gone with a word, or held with a reason. */
export type CardFate =
  | { kind: "sent"; failed?: string }
  | { kind: "stale" }
  | { kind: "conflict"; by: string; line: string }
  | { kind: "withdrawn"; reason: string; hadDraft: boolean }
  | { kind: "elsewhere" }
  | { kind: "refused"; error: string };

/** How a card leaves or stays once the host answered its item. */
export function fateOf(outcome: QuestionOutcome, line: (ask: NonNullable<QuestionOutcome["ask"]>) => string): CardFate {
  if (outcome.state === "answered" && outcome.applied === false) return { kind: "stale" };
  if (outcome.state === "answered") return outcome.delivered === false && outcome.error ? { kind: "sent", failed: outcome.error } : { kind: "sent" };
  if (outcome.state === "conflict") {
    if (outcome.withdrawn) return { kind: "withdrawn", reason: String(outcome.ask?.resolution?.closed ?? ""), hadDraft: true };
    return { kind: "conflict", by: outcome.answered_by ?? "", line: outcome.ask ? line(outcome.ask) : "" };
  }
  if (outcome.state === "missing") return { kind: "elsewhere" };
  return { kind: "refused", error: outcome.error ?? "" };
}

/** Whether a fate takes the card off the list after its moment on screen. A conflict and a refusal
 *  stay: the one says who answered first and with what, the other what to fix. */
export function leaves(fate: CardFate | undefined): boolean {
  return !!fate && (fate.kind === "sent" || fate.kind === "stale" || fate.kind === "withdrawn" || fate.kind === "elsewhere");
}

/** How long a leaving card stays: long enough to read its word, longer when a draft went with it. */
export function leaveMs(fate: CardFate): number {
  if (fate.kind === "withdrawn") return fate.hadDraft ? 5200 : 2600;
  if (fate.kind === "sent" && fate.failed) return 5200;
  if (fate.kind === "stale") return 5200;
  return 1600;
}

// ── drafts on this device ────────────────────────────────────────────────────────────────────

const DRAFTS_KEY = "daedalus.questions.drafts";
/** A draft of a question nobody asks any more is dropped; one this old is dropped as well. */
export const DRAFT_KEEP_MS = 14 * 24 * 3600 * 1000;

type Store = Pick<Storage, "getItem" | "setItem">;

function safeStorage(): Storage | null {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    return null;
  }
}

/** The drafts this device keeps. A malformed entry is skipped rather than failing the list. */
export function loadDrafts(storage: Store | null = safeStorage(), now = Date.now()): Drafts {
  try {
    const raw = storage?.getItem(DRAFTS_KEY);
    const parsed = raw ? (JSON.parse(raw) as unknown) : null;
    if (!parsed || typeof parsed !== "object") return {};
    const out: Drafts = {};
    for (const [id, value] of Object.entries(parsed as Record<string, unknown>)) {
      const d = value as Partial<Draft> | null;
      if (!d || !Array.isArray(d.selected) || typeof d.text !== "string") continue;
      const at = typeof d.at === "number" ? d.at : 0;
      if (now - at > DRAFT_KEEP_MS) continue;
      const choice = (PERMISSION_CHOICES as string[]).includes(String(d.choice)) ? (d.choice as PermissionChoice) : undefined;
      const project = typeof d.project === "string" ? d.project : null;
      out[id] = { selected: d.selected.filter((s): s is string => typeof s === "string"), text: d.text, at, project, ...(choice ? { choice } : {}) };
    }
    return out;
  } catch {
    return {};
  }
}

export function saveDrafts(drafts: Drafts, storage: Store | null = safeStorage()): void {
  const kept = Object.fromEntries(Object.entries(drafts).filter(([, d]) => !isBlank(d)));
  try {
    storage?.setItem(DRAFTS_KEY, JSON.stringify(kept));
  } catch {
    /* private mode: the drafts last for the visit */
  }
}

/** The drafts of the questions still waiting. A list prunes only the drafts that are its own: a
 *  project's tab would otherwise drop the drafts of every other project the main chat's list holds. */
export function pruneDrafts(drafts: Drafts, waiting: Iterable<string>, mine: (d: Draft) => boolean): Drafts {
  const live = new Set(waiting);
  let changed = false;
  const out: Drafts = {};
  for (const [id, d] of Object.entries(drafts)) {
    if (live.has(id) || !mine(d)) out[id] = d;
    else changed = true;
  }
  return changed ? out : drafts;
}

// ── reading ──────────────────────────────────────────────────────────────────────────────────

/** Text longer than this many lines is folded, with "Show more". */
export const FOLD_LINES = 6;

/** Whether a text is long enough to fold: its own lines, or one long paragraph that wraps into as many. */
export function folds(text: string, charsPerLine = 64): boolean {
  const lines = text.split("\n").reduce((n, line) => n + Math.max(1, Math.ceil(line.length / charsPerLine)), 0);
  return lines > FOLD_LINES;
}

/** The option an arrow key moves to: round the list, as a radio group does. */
export function stepOption(key: string, index: number, count: number): number | null {
  if (count === 0) return null;
  if (key === "ArrowRight" || key === "ArrowDown") return (index + 1) % count;
  if (key === "ArrowLeft" || key === "ArrowUp") return (index - 1 + count) % count;
  if (key === "Home") return 0;
  if (key === "End") return count - 1;
  return null;
}

/** Ctrl+Enter, or ⌘+Enter on a Mac: Send. */
export function isSendKey(e: Pick<KeyboardEvent, "key" | "ctrlKey" | "metaKey" | "altKey" | "shiftKey" | "isComposing" | "keyCode">): boolean {
  // An IME uses Enter to finish a character; it must not send the whole answer batch.
  if (e.isComposing || e.keyCode === 229) return false;
  return e.key === "Enter" && (e.ctrlKey || e.metaKey) && !e.altKey;
}
