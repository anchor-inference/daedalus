// The conversation as the screen shows it: the flat message list grouped into turns, and the
// streaming turn kept apart from the settled ones.
//
// Two things here exist for speed, and both matter on a phone. A settled turn is built once and
// the same object is handed back on every later build, so `memo` on the view holds and a token
// arriving re-renders one turn instead of six hundred. And the live turn is merged separately, so
// the history is not rebuilt to show a word.

import type { MediaPresentation, MessageView, ModelFallback, RunOutcome } from "./api";

/** A group of tools a run loaded, as `tool_group_loaded` said: which, how many, and how. */
export type GroupLoad = { group: string; tools: number; via: string };
export type LiveTool = { id: string; name: string; args: string; result?: string; error?: boolean; startedAt?: number; endedAt?: number; groups?: GroupLoad[] };
/**
 * `ended` is the model's own full stop: the last `message_stop` of the run said `end_turn`, so the
 * answer on the screen is the whole answer. The text stays until the written copy takes its place,
 * but nothing about the turn is live any more — no cursor under it, no dots over it — however long
 * the session takes to report itself idle afterwards.
 */
export type LiveState = { runId?: string; text: string; thinking: string; tools: LiveTool[]; startedAt: number | null; lastActivityAt: number | null; ended: boolean; model: string; fallback: ModelFallback | null; groups?: GroupLoad[] };
export const EMPTY_LIVE: LiveState = { text: "", thinking: "", tools: [], startedAt: null, lastActivityAt: null, ended: false, model: "", fallback: null };

export type ToolItem = { kind: "tool"; id: string; name: string; args: Record<string, unknown>; result?: string; error?: boolean; running: boolean; length?: number; clipped?: boolean; /** How long the step took, when both ends of it are known. */ ms?: number; /** The groups a ToolSearch call loaded, while the run that made it streams. */ groups?: GroupLoad[]; /** The name the call had in the program it was imported from. */ orig?: string };
export type NoteItem = { kind: "note"; text: string; seq?: number };
export type ThinkItem = { kind: "thinking"; text: string };
export type SummaryItem = { kind: "summary"; text: string; reason: string };
/** A group the run loaded without a search of its own to show for it: a held-back tool called by name. */
export type GroupItem = { kind: "group"; group: string; tools: number };
export type Activity = ToolItem | NoteItem | ThinkItem | SummaryItem | GroupItem;

export type Turn = {
  runId?: string;
  key: string;
  user?: MessageView;
  /** The user message was not the operator's own words: a loop's wake-up, a schedule's prompt, a
   *  reminder. Drawn as a folded system note, never as a bubble. */
  note?: SystemNote;
  summary?: MessageView;
  activity: Activity[];
  answer: string;
  answerSeq?: number;
  startedAt: number;
  endedAt: number;
  pendingTools: number;
  /** Everything that fed this turn. Same signature, same rendering: the previous object is reused. */
  sig: string;
  /** The tool calls already shown here, so a streamed one is not shown a second time. */
  toolIds: string[];
  /** The model that wrote this turn's answer, and what it stood in for when it was not the configured one. */
  model?: string;
  fallback?: ModelFallback | null;
  media?: MediaPresentation[];
  /** Last meaningful stream event. Transport keepalives never update this clock. */
  lastActivityAt?: number | null;
  /** The run ended without an answer: why, and where. Drawn as the turn's closing line. */
  outcome?: RunOutcome;
  /** The program this turn was imported from, when it was: its work is that program's, not Daedalus's. */
  imported?: string;
};

export type ActivityPhase = "preparing_call" | "reading" | "editing" | "testing" | "waiting_provider" | "waiting_user" | "compacting" | "responding";
export type ActivitySummary = { phase: ActivityPhase; steps: number; lastActivityAt: number | null; staleSeconds: number };

/** A stable, typed status from lifecycle data; it never reads hidden reasoning text. */
export function activitySummary(turn: Turn, live: boolean, now: number): ActivitySummary {
  const running = [...turn.activity].reverse().find((item) => item.kind === "tool" && item.running) as ToolItem | undefined;
  let phase: ActivityPhase = "waiting_provider";
  if (running) {
    if (running.name === "Read" || running.name === "ImageView" || running.name.startsWith("Web")) phase = "reading";
    else if (["Write", "Edit", "MultiEdit", "SendFile", "AttachMedia"].includes(running.name)) phase = "editing";
    else if (running.name === "Verify" || running.name === "Exec") phase = "testing";
    else if (running.name === "AskUser") phase = "waiting_user";
    else phase = "preparing_call";
  } else if (turn.activity.some((item) => item.kind === "summary")) phase = "compacting";
  else if (turn.answer) phase = "responding";
  const lastActivityAt = turn.lastActivityAt ?? null;
  return { phase, steps: turn.activity.filter((item) => item.kind === "tool").length, lastActivityAt, staleSeconds: live && lastActivityAt ? Math.max(0, Math.floor((now - lastActivityAt) / 1000)) : 0 };
}

/**
 * The trailing retrieval headline ⟦…⟧ is for the transcript index, not for the reader; a half-streamed one is cut too.
 * The same rules as `split_headline` on the host: it must be the very last thing in the text. On its own line any
 * ⟦…⟧ counts; glued to the last sentence, which models do despite being told not to, it must carry the format's two
 * `|` separators, so a sentence that quotes the format mid-text or ends on a bracketed word is left alone.
 */
export function stripHeadline(text: string): string {
  const m = text.match(/(?:^|\n)\s*⟦[^⟦⟧]{3,2000}⟧\s*$/s) ?? text.match(/[ \t]*⟦[^⟦⟧\n]{0,2000}\|[^⟦⟧\n]{0,2000}\|[^⟦⟧\n]{0,2000}⟧\s*$/);
  if (m && m.index !== undefined) return text.slice(0, m.index).trimEnd();
  const open = text.lastIndexOf("⟦");
  if (open !== -1 && !text.slice(open).includes("⟧")) {
    const lineStart = text.lastIndexOf("\n", open) + 1;
    if (!text.slice(lineStart, open).trim()) return text.slice(0, lineStart).trimEnd();
    // Glued to the last sentence and still arriving.
    if (!text.slice(open).includes("\n")) return text.slice(0, open).trimEnd();
  }
  return text;
}

export function parseArgs(raw: string): Record<string, unknown> {
  try {
    return JSON.parse(raw || "{}");
  } catch {
    return { raw };
  }
}

/**
 * Group the flat message list into turns: a user message plus everything the agent did after it.
 *
 * `previous` is the result of the last call. A turn whose inputs did not change is returned as the
 * very same object, which is what keeps the view from reconciling the whole history.
 */
export function buildTurns(messages: MessageView[], previous: readonly Turn[] = []): Turn[] {
  const results = new Map<string, { content: string; is_error: boolean; length?: number | null; clipped?: boolean; at: number }>();
  for (const m of messages) for (const r of m.tool_results) results.set(r.id, { ...r, at: Date.parse(m.created_at) || 0 });
  const turns: Turn[] = [];
  const sigs: string[][] = [];
  let current: Turn | null = null;
  const open = (key: string, at: number): Turn => {
    const t: Turn = { key, activity: [], answer: "", startedAt: at, endedAt: at, pendingTools: 0, sig: "", toolIds: [] };
    turns.push(t);
    sigs.push([key]);
    return t;
  };
  const mark = (part: string) => sigs[sigs.length - 1].push(part);
  messages.forEach((m, i) => {
    const at = Date.parse(m.created_at) || 0;
    if (m.outcome) {
      // The closing line of a run that produced no answer belongs to that run's turn, and to the end
      // of it — never after the host's "Context summary" that may follow, where it would be missed.
      let index = -1;
      for (let k = turns.length - 1; k >= 0; k--) {
        if (!turns[k].summary && (!m.run_id || turns[k].runId === m.run_id)) {
          index = k;
          break;
        }
      }
      if (index < 0) {
        current = open(`o${m.seq ?? i}`, at);
        current.runId = m.run_id || undefined;
        index = turns.length - 1;
      }
      turns[index].outcome = m.outcome;
      sigs[index].push(`o${m.seq ?? i}:${m.outcome.cause}`);
      return;
    }
    if (m.role === "tool" || m.internal) return;
    if (m.summary) {
      if (m.compaction?.reason !== "core") {
        // The host compacted between runs (auto) or on request (manual): a block of its own after the
        // turn, so the answer that came before it stays the answer.
        turns.push({ key: `s${m.seq ?? i}`, summary: m, activity: [], answer: "", startedAt: at, endedAt: at, pendingTools: 0, sig: "", toolIds: [] });
        sigs.push([`s${m.seq ?? i}`, `${m.text.length}`]);
        current = null;
        return;
      }
      // The core compacted mid-run: a step inside the turn, where the summarised work used to be.
      if (!current) current = open(`a${m.seq ?? i}`, at);
      if (current.answer) {
        current.activity.push({ kind: "note", text: current.answer, seq: current.answerSeq });
        current.answer = "";
        current.answerSeq = undefined;
      }
      current.activity.push({ kind: "summary", text: m.text, reason: "core" });
      mark(`x${m.seq ?? i}:${m.text.length}`);
      return;
    }
    if (m.role === "user") {
      current = open(`u${m.seq ?? i}`, at);
      current.runId = m.run_id || undefined;
      current.user = m;
      if (m.imported?.harness) current.imported = m.imported.harness;
      const note = systemNote(m);
      if (note) current.note = note;
      mark(`${m.text.length}:${m.origin ?? ""}:${m.run_id ?? ""}:${m.imported?.harness ?? ""}`);
      return;
    }
    if (m.role === "system") return;
    if (!current || (m.run_id && current.runId && m.run_id !== current.runId)) current = open(`a${m.seq ?? i}`, at);
    current.runId = m.run_id || current.runId;
    current.endedAt = at;
    // The turn is named by the model of its latest assistant message: a turn that began on one model
    // and finished on another is answered by the one that finished it, which is the one the reader read.
    current.model = m.model || current.model;
    current.fallback = m.fallback ?? null;
    current.media = m.media?.length ? m.media : current.media;
    if (m.imported?.harness) current.imported = m.imported.harness;
    mark(`m${m.imported?.harness ?? ""}:${m.seq ?? i}:${m.text.length}:${m.thinking.length}:${m.model ?? ""}:${m.fallback?.reason ?? ""}:${m.run_id ?? ""}:${m.media?.map((p) => p.id).join(",") ?? ""}`);
    if (current.answer) {
      // Text that turned out not to be final becomes a note.
      current.activity.push({ kind: "note", text: current.answer, seq: current.answerSeq });
      current.answer = "";
      current.answerSeq = undefined;
    }
    if (m.thinking) current.activity.push({ kind: "thinking", text: m.thinking });
    if (m.text && m.tool_calls.length) current.activity.push({ kind: "note", text: m.text, seq: m.seq ?? undefined });
    else if (m.text) { current.answer = m.text; current.answerSeq = m.seq ?? undefined; }
    for (const c of m.tool_calls) {
      const r = results.get(c.id);
      const running = r === undefined;
      if (running) current.pendingTools++;
      current.toolIds.push(c.id);
      const ms = r && at && r.at >= at ? r.at - at : undefined;
      const orig = m.imported?.tools?.[c.id];
      current.activity.push({ kind: "tool", id: c.id, name: c.name, args: c.arguments, result: r?.content, error: r?.is_error, running, length: r?.length ?? undefined, clipped: r?.clipped, ms, ...(orig ? { orig } : {}) });
      mark(`t${c.id}:${r ? `${r.content.length}${r.is_error ? "!" : ""}` : "-"}`);
    }
  });
  const before = new Map<string, Turn>();
  for (const t of previous) before.set(t.key, t);
  return turns.map((t, i) => {
    t.sig = sigs[i].join("|");
    const old = before.get(t.key);
    return old && old.sig === t.sig ? old : t;
  });
}

/** The settled turn the streaming one continues, if there is one: a compaction block never is. */
export function liveBase(turns: readonly Turn[], runId?: string | null): Turn | null {
  const last = turns[turns.length - 1];
  return last && !last.summary && (!runId || last.runId === runId) ? last : null;
}

/** The streaming turn: the settled tail with what the event stream has said since merged into it. */
export function applyLive(base: Turn | null, live: LiveState, now: number): Turn {
  const t: Turn = base
    ? { ...base, activity: base.activity.slice(), toolIds: base.toolIds }
    : { key: "live", activity: [], answer: "", startedAt: live.startedAt ?? now, endedAt: now, pendingTools: 0, sig: "", toolIds: [] };
  if (live.tools.length && t.pendingTools > 0) {
    // The call is in the history but its result is not written yet: the streamed result fills it in.
    t.activity = t.activity.map((a) => {
      if (a.kind !== "tool" || !a.running) return a;
      const lt = live.tools.find((x) => x.id === a.id);
      if (!lt || lt.result === undefined) return a;
      t.pendingTools--;
      // The stream carries the whole result, so what came over it is never cut short.
      const ms = lt.startedAt && lt.endedAt ? lt.endedAt - lt.startedAt : a.ms;
      return { ...a, result: lt.result, error: lt.error, running: false, length: lt.result.length, clipped: false, ms, groups: lt.groups };
    });
  }
  // Only what streamed after the last step the history already shows is new. A streamed step older
  // than that and missing from the history is on a page the screen has not read; added here it went
  // to the end of the turn, below the steps and the words that came after it.
  let lastKnown = -1;
  live.tools.forEach((lt, index) => {
    if (t.toolIds.includes(lt.id)) lastKnown = index;
  });
  const fresh = live.tools.filter((lt, index) => index > lastKnown && !t.toolIds.includes(lt.id));
  const thinkingKnown = t.activity.some((a) => a.kind === "thinking" && a.text === live.thinking);
  if (t.answer && ((live.text && live.text !== t.answer) || (live.thinking && !thinkingKnown) || fresh.length)) {
    // Something newer is streaming, so the text before it was not the final answer.
    t.activity.push({ kind: "note", text: t.answer, seq: t.answerSeq });
    t.answer = "";
    t.answerSeq = undefined;
  }
  if (live.thinking && !thinkingKnown) t.activity.push({ kind: "thinking", text: live.thinking });
  for (const lt of fresh) {
    const running = lt.result === undefined;
    if (running) t.pendingTools++;
    t.activity.push({ kind: "tool", id: lt.id, name: lt.name, args: parseArgs(lt.args), result: lt.result, error: lt.error, running, length: lt.result?.length, clipped: false, ms: lt.startedAt && lt.endedAt ? lt.endedAt - lt.startedAt : undefined, groups: lt.groups });
  }
  // A search's loads are on its own step; the rest — a held-back tool called by its name — get a line.
  // The run's seed is not announced here: carried groups would repeat the line at every message.
  const annotated = new Map(live.tools.filter((lt) => lt.groups?.length).map((lt) => [lt.id, lt.groups]));
  if (annotated.size) t.activity = t.activity.map((a) => (a.kind === "tool" && !a.groups && annotated.has(a.id) ? { ...a, groups: annotated.get(a.id) } : a));
  for (const g of live.groups ?? []) t.activity.push({ kind: "group", group: g.group, tools: g.tools });
  if (live.text) t.answer = stripHeadline(live.text);
  if (live.model) {
    t.model = live.model;
    t.fallback = live.fallback;
  }
  t.endedAt = now;
  t.lastActivityAt = live.lastActivityAt;
  return t;
}

// ── what a turn is made of, read for the reader ───────────────────────────────────────────

/** A user message that is not the operator's own words, and what it is instead. */
export type SystemNote = {
  /** Who wrote it: the loop, a schedule, a reminder, the core, or a source named by its prefix. */
  kind: "loop" | "schedule" | "reminder" | "intent" | "core" | "heartbeat" | "context" | "events" | "other";
  origin: string;
  /** For a loop wake-up: the run number and the cadence, read off the host's own header line. */
  iteration?: number;
  total?: number;
  cadence?: string;
  /** The standing instruction, without the host's framing around it. */
  body: string;
};

const LOOP_HEAD_RE = /^\s*\[Loop iteration (\d+)(?: of (\d+))?\s*[—-]\s*([^.\]]+)[^\]]*\]/;
const LOOP_BODY_RE = /<loop_instruction>\s*([\s\S]*?)\s*<\/loop_instruction>/;
const CONTEXT_RE = /<(turn_context|heartbeat)>\s*([\s\S]*?)\s*<\/\1>/;

/**
 * Whether a user message is a system note, and which. The host's own marker (`origin`) decides
 * where it exists; the text's shape is the fallback for a transcript written before the marker was.
 * A message a person sent through another channel (`inbound:<source>`) is not a note: it is a
 * message, and stays a card with its source on it.
 */
export function systemNote(m: Pick<MessageView, "role" | "text" | "origin" | "internal">): SystemNote | null {
  if (m.role !== "user" || m.internal) return null;
  const origin = m.origin ?? "";
  const text = m.text ?? "";
  const loop = LOOP_HEAD_RE.exec(text);
  if (origin === "loop" || loop || text.includes("<loop_instruction>")) {
    const body = LOOP_BODY_RE.exec(text)?.[1] ?? text.replace(LOOP_HEAD_RE, "").trim();
    return { kind: "loop", origin: origin || "loop", iteration: loop ? Number(loop[1]) : undefined, total: loop?.[2] ? Number(loop[2]) : undefined, cadence: loop?.[3]?.trim(), body };
  }
  const ctx = CONTEXT_RE.exec(text);
  if (ctx) return { kind: ctx[1] === "heartbeat" ? "heartbeat" : "context", origin: origin || ctx[1], body: ctx[2] };
  if (!origin || origin === "operator" || origin.startsWith("inbound")) return null;
  const kind = origin === "schedule" || origin === "reminder" || origin === "intent" || origin === "core" || origin === "heartbeat" || origin === "events" ? origin : "other";
  return { kind, origin, body: text };
}

// ── a project's events, as its orchestrator was woken with them ────────────────────────────

/** How an event line reads to the operator: something finished, something waits for a decision,
 *  something broke, or plain news. */
export type EventTone = "ok" | "warn" | "bad" | "info";

export type EventLine = { time: string; text: string; tone: EventTone; ask: string | null };

/** A batch of the project's events: `[events · <project> · <n> since <HH:MM>]` and one `- HH:MM …`
 *  line per event, written by the host for the orchestrator (daedalus/extensions/orchestrator.py). */
export type EventBatch = { project: string; count: number; since: string; lines: EventLine[]; more: number };

// The main orchestrator is woken the same way, with its projects' reports: `[reports · <n> since <HH:MM>]`.
const EVENTS_HEAD_RE = /^\[(?:events · (.*) · |reports · )(\d+) since ([^\]]*)\]\s*$/;
const EVENT_LINE_RE = /^- (\d{1,2}:\d{2}) (.*)$/;
const MORE_RE = /^- … and (\d+) more\b/;
// What the host adds for the orchestrator's sake — the tool that reads the whole reply, whose move
// the request is — is advice to the model, not news for the operator.
const FOR_THE_MODEL_RE = / — (?:ReadStaff\("[^"]*"\) for the whole reply|yours to answer or escalate|the operator decides; you are told|tell the operator; do not prod the project yourself|nothing was added; do not ask for it again unless that reason is gone)$/;
const ASK_RE = /\[(q[0-9a-z]{4,8})\]/i;

/** The tone of one line, read from the host's own wording. The host writes these sentences in one
 *  place and in English; a line this does not recognise is plain news, never a false alarm. */
export function eventTone(text: string): EventTone {
  if (/ stopped with an error| error\b|merge failed|crashed| as blocked:| could not be added/.test(text)) return "bad";
  if (/ needs permission| asks\b|reported (?:stuck|needs_input)| has gone silent| has been quiet for/.test(text)) return "warn";
  if (/ finished a turn| reported done|answered your request| accepted\b| as done:| finished its setup/.test(text)) return "ok";
  return "info";
}

/** A batch read back out of the message it was delivered as; null for anything else. */
export function parseEvents(text: string): EventBatch | null {
  const [head, ...rest] = text.split("\n");
  const m = EVENTS_HEAD_RE.exec(head.trim());
  if (!m) return null;
  const lines: EventLine[] = [];
  let more = 0;
  for (const raw of rest) {
    const row = raw.trimEnd();
    if (!row.trim()) continue;
    const extra = MORE_RE.exec(row);
    if (extra) {
      more = Number(extra[1]);
      continue;
    }
    const line = EVENT_LINE_RE.exec(row);
    if (line) {
      const body = line[2].replace(FOR_THE_MODEL_RE, "");
      lines.push({ time: line[1], text: body, tone: eventTone(body), ask: ASK_RE.exec(body)?.[1] ?? null });
    } else if (lines.length) {
      // A line the host wrapped: it belongs to the event above it.
      lines[lines.length - 1].text += `\n${row}`;
    }
  }
  return { project: m[1] ?? "", count: Number(m[2]), since: m[3], lines, more };
}

/** The families a run's steps fall into, most frequent first, as the folded line names them. */
export type FamilyCount = { family: string; n: number };

/** Which family a tool belongs to for the folded summary: the search tools share one, everything else is itself. */
export function familyOf(name: string): string {
  if (name === "Find" || name === "Search" || name === "WebSearch" || name === "HistorySearch") return "search";
  // Calls imported from another program under their own names count with the native steps they amount to.
  if (name === "Task" || name === "Agent") return "SubAgent";
  if (name === "apply_patch") return "Edit";
  if (name === "TodoWrite" || name === "todowrite" || name === "update_plan") return "plan";
  return name;
}

/**
 * The steps of a turn counted by family, most frequent first and, at a tie, in the order they
 * happened. `cap` families are named; whatever is left is one number ("and 4 more").
 */
export function familyCounts(items: readonly Activity[], cap = 3): { named: FamilyCount[]; more: number } {
  const counts = new Map<string, number>();
  for (const a of items) {
    if (a.kind !== "tool") continue;
    const f = familyOf(a.name);
    counts.set(f, (counts.get(f) ?? 0) + 1);
  }
  const all = [...counts.entries()].map(([family, n]) => ({ family, n }));
  all.sort((a, b) => b.n - a.n);
  const named = all.slice(0, cap);
  const more = all.slice(cap).reduce((sum, f) => sum + f.n, 0);
  return { named, more };
}

/** A file the agent produced in a turn: written into the workspace, or handed over. */
export type Artifact = {
  /** The tool call that made it, which is what the card is keyed and served by. */
  callId: string;
  path: string;
  name: string;
  /** Written by the turn, sent by it, or a file kept by handle that the text names (`att:…`). */
  how: "wrote" | "sent" | "kept";
  caption: string;
  /** The size as the tool's answer reported it, when it did. */
  size: string | null;
};

const SIZE_RE = /\(([\d.,]+\s?[KMG]?B)\)/i;

/**
 * The files a turn produced, once each: every settled `Write` and `SendFile`, the last mention of
 * a path winning, so a file written twice in one turn is one card.
 */
export function producedFiles(items: readonly Activity[]): Artifact[] {
  const out = new Map<string, Artifact>();
  for (const a of items) {
    if (a.kind !== "tool" || a.running || a.error) continue;
    if (a.name !== "Write" && a.name !== "SendFile") continue;
    const path = typeof a.args.path === "string" ? a.args.path : "";
    if (!path) continue;
    const name = path.split("/").filter(Boolean).pop() ?? path;
    const caption = typeof a.args.caption === "string" ? a.args.caption : "";
    const size = SIZE_RE.exec(a.result ?? "")?.[1] ?? null;
    out.delete(path);
    out.set(path, { callId: a.id, path, name, how: a.name === "Write" ? "wrote" : "sent", caption, size });
  }
  return [...out.values()];
}

// ── reconciling what the API says with what the screen already holds ──────────────────────

export type Merge = {
  messages: MessageView[];
  /** The tail did not overlap what is on screen (or the history was rewritten): re-read it whole. */
  gap: boolean;
};

/** A message's identity before it has a row: the parts the host's transcript key is made of. */
const identity = (m: MessageView): string => `${m.role}|${m.created_at}|${m.tool_calls[0]?.id ?? m.tool_results[0]?.id ?? ""}`;

const sameMessage = (a: MessageView, b: MessageView): boolean =>
  a.run_id === b.run_id && a.internal === b.internal && a.text === b.text && a.thinking === b.thinking && a.tool_calls.length === b.tool_calls.length && a.tool_results.length === b.tool_results.length && !!a.summary === !!b.summary;

/**
 * Fold a freshly read tail into the messages already on screen, matching by `seq`.
 *
 * The tail is what the run has just added; everything older is already here. When the two do not
 * overlap — the screen has been away long enough to miss messages — or the history got shorter
 * (a revert, a cleared session), there is nothing to fold into and the caller re-reads it whole.
 * When nothing in the tail is new, the array that came in is handed back unchanged, so the turn
 * objects keep their identity and nothing re-renders.
 *
 * A running session ends in messages the engine holds and the transcript has not written yet. Their
 * `seq` is the one the row is going to get, and they say so (`live`), because until the row exists
 * the number is a promise rather than a fact: the tail is the only authority on them, so whatever
 * the screen holds for them is dropped and what came in takes its place. They are never a gap —
 * treating them as one costs a re-read of the whole session on every event of a run.
 */
export function reconcile(known: readonly MessageView[], tail: readonly MessageView[]): Merge {
  if (!known.length) return { messages: tail.slice(), gap: false };
  if (!tail.length) return { messages: known.slice(), gap: false };
  const settled = known.filter((m) => !m.live);
  const fresh = tail.filter((m) => !m.live);
  const live = tail.filter((m) => m.live);
  // Nothing settled on either side to match on: the tail is all there is to go by.
  if (!settled.length) return { messages: tail.slice(), gap: false };
  if (settled.some((m) => m.seq == null) || fresh.some((m) => m.seq == null)) return { messages: known.slice(), gap: true };
  const maxKnown = settled[settled.length - 1].seq!;
  if (fresh.length) {
    if (fresh[fresh.length - 1].seq! < maxKnown) return { messages: known.slice(), gap: true };
    if (fresh[0].seq! > maxKnown + 1) return { messages: known.slice(), gap: true };
  }
  const bySeq = new Map<number, MessageView>();
  for (const m of settled) bySeq.set(m.seq!, m);
  let changed = false;
  for (const m of fresh) {
    const old = bySeq.get(m.seq!);
    if (old && sameMessage(old, m)) continue;
    changed = true;
    bySeq.set(m.seq!, m);
  }
  const base = changed ? [...bySeq.values()].sort((a, b) => a.seq! - b.seq!) : settled;
  // The live tail always trails what is settled, so what the screen holds for it sits at the end.
  const heldLive = known.slice(settled.length);
  // The transcript is written when the run ends, so every message of a run is live until then, and a
  // run of more than a dozen steps is longer than the tail an event asks for. The tail used to stand
  // for the whole live part: the steps that fell off its front left the screen, the stream's copy of
  // them came back in after the newer ones, and a run's earlier steps jumped below its interim text.
  // What the screen holds and the tail no longer reaches is kept, in front of the tail. It is matched
  // by identity as well as by number: a message queued during the run takes a row, and every live
  // number after it moves up by one, so a number alone can name the wrong message; and a message the
  // tail does hold is never kept twice, whatever its number says.
  const inTail = new Set(tail.map(identity));
  const written = new Set(base.slice(-(heldLive.length + tail.length)).map(identity));
  const firstLive = live[0]?.seq;
  const keptLive = firstLive != null ? heldLive.filter((m) => m.seq != null && m.seq < firstLive && !inTail.has(identity(m)) && !written.has(identity(m))) : [];
  if (live.length && !fresh.length && !heldLive.some((m) => inTail.has(identity(m)))) {
    // A live tail that starts past everything the screen holds has a hole in front of it.
    const reach = base.length ? base[base.length - 1].seq! : 0;
    if (live[0].seq != null && live[0].seq > reach + 1 + keptLive.length) return { messages: known.slice(), gap: true };
  }
  const nextLive = [...keptLive, ...live];
  const sameLive = nextLive.length === heldLive.length && nextLive.every((m, i) => sameMessage(m, heldLive[i]));
  if (!changed && sameLive) return { messages: known as MessageView[], gap: false };
  return { messages: nextLive.length ? [...base, ...nextLive] : base, gap: false };
}

/** An older page, put in front of what is on screen. Anything not actually older is dropped. */
export function prepend(known: readonly MessageView[], older: readonly MessageView[]): MessageView[] {
  if (!known.length) return older.slice();
  const first = known[0].seq;
  const head = first == null ? [] : older.filter((m) => m.seq != null && m.seq < first);
  return head.length ? [...head, ...known] : (known as MessageView[]);
}

/** Whether an answer to `before=<seq>` really is an older page: an API that ignores the cursor repeats the tail. */
export function isOlderPage(older: readonly MessageView[], oldestKnown: number | null | undefined): boolean {
  if (oldestKnown == null || !older.length) return false;
  return older.every((m) => m.seq != null && m.seq < oldestKnown);
}

/**
 * The streaming turn's state after one event off the wire. Pure, so what the screen shows during a
 * run is decided in one place and can be read back without a browser.
 *
 * The turn ends on the model's own full stop — `message_stop` with `end_turn` — and on nothing else.
 * The session reports itself idle later, after it has written the answer down, snapshotted the files
 * and told the other fronts, and waiting for that is what used to leave a cursor blinking under a
 * finished answer. The text is kept: the written copy takes its place when the read lands.
 */
export function liveAfter(state: LiveState, event: string, p: Record<string, any>): LiveState {
  const meaningful = Date.now();
  if (p.run_id && p.run_id !== state.runId) {
    // Housekeeping from a finished run can arrive after the next run has started.
    if (state.runId && event !== "message_start" && event !== "model_changed") return state;
    state = { ...EMPTY_LIVE, runId: p.run_id };
  }
  if (event === "queue_update" && p.placed?.length) return { ...EMPTY_LIVE, runId: state.runId, model: state.model, fallback: state.fallback };
  if (event === "message_start") return { ...state, text: "", thinking: "", ended: false, startedAt: state.startedAt ?? meaningful, lastActivityAt: meaningful };
  // Which model is speaking, said before the message it belongs to. `fallback: false` is the run coming
  // back to the model it was configured with, and it takes the note away rather than leaving it standing.
  if (event === "model_changed") return { ...state, model: String(p.to ?? p.model_name ?? ""), fallback: p.fallback ? { from: String(p.configured ?? p.from ?? ""), to: String(p.to ?? ""), reason: String(p.reason ?? "") } : null, lastActivityAt: meaningful };
  if (event === "content_block_delta") {
    const d = p.delta ?? {};
    if (d.type === "text_delta") return { ...state, text: state.text + (d.text ?? ""), lastActivityAt: meaningful };
    if (d.type === "thinking_delta") return { ...state, thinking: state.thinking + (d.text ?? ""), lastActivityAt: meaningful };
    return state;
  }
  if (event === "tool_use_start") return { ...state, tools: [...state.tools, { id: p.tool_call_id, name: p.tool_name, args: "", startedAt: meaningful }], lastActivityAt: meaningful };
  if (event === "tool_use_stop") return { ...state, tools: state.tools.map((t) => (t.id === p.tool_call_id ? { ...t, args: JSON.stringify(p.final_input ?? {}) } : t)), lastActivityAt: meaningful };
  if (event === "tool_group_loaded") {
    const load: GroupLoad = { group: String(p.group ?? ""), tools: Array.isArray(p.tools) ? p.tools.length : 0, via: String(p.via ?? "") };
    if (!load.group || load.via === "seed") return state;
    if (load.via === "direct_call") return { ...state, groups: [...(state.groups ?? []), load], lastActivityAt: meaningful };
    // Loaded by a ToolSearch call: the event follows that call's result, so it is the newest search.
    const at = state.tools.map((t) => t.name).lastIndexOf("ToolSearch");
    if (at < 0) return { ...state, groups: [...(state.groups ?? []), load], lastActivityAt: meaningful };
    const tools = state.tools.map((t, i) => (i === at ? { ...t, groups: [...(t.groups ?? []).filter((g) => g.group !== load.group), load] } : t));
    return { ...state, tools, lastActivityAt: meaningful };
  }
  if (event === "tool_result") return { ...state, tools: state.tools.map((t) => (t.id === p.tool_call_id ? { ...t, result: String(p.content ?? p.output ?? ""), error: !!p.is_error, endedAt: meaningful } : t)), lastActivityAt: meaningful };
  // A message that ended to make a tool call is not the end of the turn: the run goes on.
  if (event === "message_stop" && (p.stop_reason === "end_turn" || p.stop_reason === "max_tokens")) return { ...state, ended: true, lastActivityAt: meaningful };
  return state;
}

// ── the streaming turn's state, outside React ─────────────────────────────────────────────

export type LiveStore = {
  get: () => LiveState;
  subscribe: (fn: () => void) => () => void;
  update: (fn: (s: LiveState) => LiveState) => void;
  reset: () => void;
};

/**
 * Tokens arrive faster than a phone repaints. They are folded into one object here and the
 * subscribers are told once a frame, so the screen does as much work per frame as it can show —
 * and only the components that read this one re-render, not the whole conversation.
 */
export function createLiveStore(): LiveStore {
  let state = EMPTY_LIVE;
  const subs = new Set<() => void>();
  let frame = 0;
  const raf = typeof requestAnimationFrame === "function" ? requestAnimationFrame : (fn: FrameRequestCallback) => setTimeout(() => fn(0), 16) as unknown as number;
  const cancel = typeof cancelAnimationFrame === "function" ? cancelAnimationFrame : (h: number) => clearTimeout(h);
  const flush = () => {
    frame = 0;
    for (const fn of [...subs]) fn();
  };
  return {
    get: () => state,
    subscribe(fn) {
      subs.add(fn);
      return () => {
        subs.delete(fn);
      };
    },
    update(fn) {
      const next = fn(state);
      // An event the streaming turn has nothing to do with — a state change, a hook, the end of the
      // run — leaves the state as it was, and repainting for it would cost a frame per event during
      // a run for nothing on the screen.
      if (next === state) return;
      state = next;
      if (!frame) frame = raf(flush);
    },
    reset() {
      if (frame) cancel(frame);
      frame = 0;
      if (state === EMPTY_LIVE) return;
      state = EMPTY_LIVE;
      for (const fn of [...subs]) fn();
    },
  };
}
