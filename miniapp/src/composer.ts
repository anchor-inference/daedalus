// The composer as a value: what its one circle means right now, what a key press asks for, the
// draft that survives leaving the session, and the queue of steers the host is holding. Nothing in
// here touches the screen, so every state the pill can be in can be read back without a browser.

import type { Question } from "./api";

export type ComposerStatus = "idle" | "running" | "waiting" | "failed" | "done" | "compacting";

/** What the primary circle does when pressed, and how it is drawn. */
export type Primary = "send" | "stop" | "queue" | "reply";

export type PrimaryInput = {
  status: ComposerStatus;
  hasDraft: boolean;
  hasFiles: boolean;
  /** The agent asked something and the dock above the pill holds the answer. */
  asking: boolean;
  sending: boolean;
};

/**
 * One circle, four meanings. While a run is on, an empty pill stops it and a written one queues
 * a steer; while the agent waits for an answer the circle is Reply; otherwise it sends. The
 * second value says whether pressing it does anything at all.
 */
export function primaryAction(i: PrimaryInput): { action: Primary; enabled: boolean } {
  const filled = i.hasDraft || i.hasFiles;
  if (i.status === "running") {
    if (filled) return { action: "queue", enabled: !i.sending };
    return { action: "stop", enabled: true };
  }
  if (i.status === "waiting" && i.asking && !filled) return { action: "reply", enabled: true };
  return { action: "send", enabled: filled && !i.sending };
}

/** The placeholder, by state: what typing here would do. */
export function placeholderKey(status: ComposerStatus, asking: boolean): string {
  if (status === "running") return "session.composer.running";
  if (status === "waiting" || asking) return "session.composer.waiting";
  return "session.composer.idle";
}

// ── keys ─────────────────────────────────────────────────────────────────────────────────

export type KeyIntent = "send" | "newline" | "complete" | "escape" | "model" | "stop" | null;

type KeyLike = Pick<KeyboardEvent, "key" | "code" | "metaKey" | "ctrlKey" | "altKey" | "shiftKey">;

/**
 * What a key press in the field asks for. `Enter` and the letter shortcuts use physical position (`code`), with an Enter
 * name fallback for embedded browsers that omit it, so
 * ⌘M opens the model list whichever alphabet the keyboard is on.
 */
export function composerKey(e: KeyLike, opts: { enterSends: boolean; paletteOpen: boolean }): KeyIntent {
  const mod = e.metaKey || e.ctrlKey;
  if (mod && !e.altKey && e.shiftKey && e.code === "KeyS") return "stop";
  if (mod && !e.altKey && !e.shiftKey && e.code === "KeyM") return "model";
  if (e.key === "Tab" && opts.paletteOpen && !e.shiftKey) return "complete";
  if (e.key === "Escape") return "escape";
  if (e.code !== "Enter" && e.code !== "NumpadEnter" && e.key !== "Enter") return null;
  if (mod) return "send";
  if (e.shiftKey || e.altKey) return "newline";
  return opts.enterSends ? "send" : "newline";
}

/** A key on the approval dock: `y` allows, `n` refuses — plain letters, so a text field never sees them. */
export function dockKey(e: KeyLike, typing: boolean): "approve" | "deny" | null {
  if (typing || e.metaKey || e.ctrlKey || e.altKey) return null;
  if (e.code === "KeyY") return "approve";
  if (e.code === "KeyN") return "deny";
  return null;
}

// ── growth ───────────────────────────────────────────────────────────────────────────────

/** How many lines the field grows to before it scrolls. */
export const MAX_ROWS = 8;

/** Reserve the minimum rows, grow with content, then scroll at the supplied row limit. */
export function fieldHeight(scrollHeight: number, lineHeight: number, padding: number, minRows = 1, maxRows = MAX_ROWS): number {
  const floor = Math.ceil(lineHeight * minRows + padding);
  const cap = Math.round(lineHeight * maxRows + padding);
  return Math.max(floor, Math.min(scrollHeight, cap));
}

// ── the draft, kept per session ──────────────────────────────────────────────────────────

const DRAFT_PREFIX = "daedalus.draft.";
/** How long after the last keystroke the draft is written down. */
export const DRAFT_DEBOUNCE_MS = 300;

type StorageLike = Pick<Storage, "getItem" | "setItem" | "removeItem">;

function safeStorage(): StorageLike | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

export function draftKey(sessionId: string): string {
  return `${DRAFT_PREFIX}${sessionId}`;
}

export function readDraft(sessionId: string, storage: StorageLike | null = safeStorage()): string {
  try {
    return storage?.getItem(draftKey(sessionId)) ?? "";
  } catch {
    return "";
  }
}

/** An empty draft is removed rather than stored as an empty string: nothing to restore is nothing. */
export function writeDraft(sessionId: string, text: string, storage: StorageLike | null = safeStorage()): void {
  try {
    if (text.trim()) storage?.setItem(draftKey(sessionId), text);
    else storage?.removeItem(draftKey(sessionId));
  } catch {
    /* private mode, full storage: the draft lives in the field only */
  }
}

export function clearDraft(sessionId: string, storage: StorageLike | null = safeStorage()): void {
  writeDraft(sessionId, "", storage);
}

/** The last custom model the operator typed, so the field opens with it. */
const CUSTOM_MODEL_KEY = "daedalus.model.custom";

export function readCustomModel(storage: StorageLike | null = safeStorage()): string {
  try {
    return storage?.getItem(CUSTOM_MODEL_KEY) ?? "";
  } catch {
    return "";
  }
}

export function rememberCustomModel(value: string, storage: StorageLike | null = safeStorage()): void {
  try {
    if (value.trim()) storage?.setItem(CUSTOM_MODEL_KEY, value.trim());
  } catch {
    /* ignore */
  }
}

// ── the steer queue ──────────────────────────────────────────────────────────────────────

/** A message the host is holding for the next model call. */
export type QueuedSteer = { id: string; text: string; queued_at: string | null };

/** What the stream says about the queue; the whole queue rides in every event. */
export type SteerChange = { reason?: string; count?: number; queued?: unknown };

/** The queue after a `steer_changed` event: the payload's list, whatever the reason, or nothing. */
export function steersAfter(_current: QueuedSteer[], payload: SteerChange): QueuedSteer[] {
  return readSteers(payload.queued);
}

/** The queue as the API answers it, guarded: a route that is not there answers something else. */
export function readSteers(raw: unknown): QueuedSteer[] {
  if (!Array.isArray(raw)) return [];
  const out: QueuedSteer[] = [];
  for (const it of raw) {
    if (!it || typeof it !== "object") continue;
    const r = it as Record<string, unknown>;
    const id = typeof r.id === "string" ? r.id : "";
    const text = typeof r.text === "string" ? r.text : "";
    if (!id || !text.trim()) continue;
    out.push({ id, text, queued_at: typeof r.queued_at === "string" ? r.queued_at : null });
  }
  return out;
}

// ── the approval dock ────────────────────────────────────────────────────────────────────

/** A tool call the policy refused, with the key that lets it through once. */
export type Approval = { key: string; callId: string; tool: string; detail: string };

const APPROVAL_RE = /Approval key: ([0-9a-f]{12})/;

type CallLike = { id: string; name: string; arguments: Record<string, unknown> };
type MessageLike = { tool_calls: CallLike[]; tool_results: { id: string; content: string; is_error: boolean }[] };

/**
 * The most recent refusal still waiting on the operator: the last error result that names an
 * approval key, unless that key was already spent or dismissed. Read off the end of the history,
 * where the refusal that matters is.
 */
export function pendingApproval(messages: readonly MessageLike[], seen: ReadonlySet<string>, lastMessages = 12): Approval | null {
  const calls = new Map<string, CallLike>();
  const tail = messages.slice(-lastMessages);
  for (const m of messages) for (const c of m.tool_calls) calls.set(c.id, c);
  for (let i = tail.length - 1; i >= 0; i--) {
    for (const r of [...tail[i].tool_results].reverse()) {
      if (!r.is_error) continue;
      const key = APPROVAL_RE.exec(r.content)?.[1];
      if (!key || seen.has(key)) continue;
      const call = calls.get(r.id);
      const a = call?.arguments ?? {};
      const detail = typeof a.command === "string" ? a.command : typeof a.path === "string" ? a.path : typeof a.url === "string" ? a.url : "";
      return { key, callId: r.id, tool: call?.name ?? "", detail: detail.split("\n")[0].slice(0, 160) };
    }
  }
  return null;
}

/** Whether the answers to the agent's questions are complete enough to send: one for each question. */
export function answersComplete(questions: readonly Question[], answers: readonly { selected: string[]; custom: string }[]): boolean {
  return questions.length > 0 && answers.length === questions.length && answers.every((a) => a.selected.length > 0 || a.custom.trim().length > 0);
}

/** What makes the agent's questions the same questions, whatever object carries them. The session is
 *  read again every few seconds, and every read carries the pending questions as a new array: keyed on
 *  that array, the answers the operator had started were wiped by a refresh between picking an option
 *  and pressing Reply, and Reply then found nothing chosen and did nothing. */
export function questionsKey(questions: readonly Question[] | null | undefined): string {
  return JSON.stringify((questions ?? []).map((q) => [q.question, q.header ?? "", (q.options ?? []).map((o) => o.label), !!q.multiSelect, !!q.allow_custom]));
}

/** Names only: context should orient the operator without repeating full filesystem paths. */
export type ComposerPlace = { project?: string; workspace?: string; system?: boolean };
export function composerContext(place?: ComposerPlace): { kind: "project" | "workspace"; name: string }[] {
  if (!place || place.system) return [];
  const short = (value = "") => value.trim().replace(/[\\/]+$/, "").split(/[\\/]/).at(-1) ?? "";
  const project = short(place.project);
  const workspace = short(place.workspace);
  const chips: { kind: "project" | "workspace"; name: string }[] = [];
  if (project) chips.push({ kind: "project", name: project });
  if (workspace && workspace !== "." && workspace.toLocaleLowerCase() !== project.toLocaleLowerCase() && !/^[a-f0-9-]{12,}$/i.test(workspace)) {
    chips.push({ kind: "workspace", name: workspace });
  }
  return chips;
}
