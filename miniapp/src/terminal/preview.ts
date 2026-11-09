// What the Terminals screen says about a terminal without opening it: the last rows of its screen in
// their own colours, the line under them, and which filter and group it falls into. Pure, so the
// rules are tested without a browser.

import type { CSSProperties } from "react";
import type { TerminalRun, TerminalView } from "../api";
import { t } from "../i18n";

/**
 * A run's colour as CSS. The daemon writes 0 for the default colour (left to the card), 1..256 for
 * palette entries 0..255 plus one, and 0x1000000 + RGB for true colour. The first sixteen are the
 * app's `--ansi-*` tokens, so a preview changes with the scheme exactly as the terminal does; the
 * rest of the 256 are the fixed cube and grey ramp every terminal agrees on.
 */
export function runColor(value: number | string | undefined): string | undefined {
  if (value === undefined || value === 0 || value === "") return undefined;
  if (typeof value === "string") return value;
  if (value >= 0x1000000) return `#${(value & 0xffffff).toString(16).padStart(6, "0")}`;
  const index = value - 1;
  if (index < 0 || index > 255) return undefined;
  if (index < 16) return `var(--ansi-${index})`;
  if (index >= 232) {
    const level = 8 + (index - 232) * 10;
    return `rgb(${level}, ${level}, ${level})`;
  }
  const cube = index - 16;
  const step = (n: number) => (n === 0 ? 0 : 55 + n * 40);
  return `rgb(${step(Math.floor(cube / 36))}, ${step(Math.floor(cube / 6) % 6)}, ${step(cube % 6)})`;
}

/** The inline style of one run: its colours (swapped when inverse), weight, slant, underline, dimness. */
export function runStyle(run: TerminalRun): CSSProperties | undefined {
  let fg = runColor(run.fg);
  let bg = runColor(run.bg);
  if (run.inv) {
    // An inverse run with default colours still has to look inverse: the card's own colours, swapped.
    [fg, bg] = [bg ?? "var(--term-bg)", fg ?? "var(--fg)"];
  }
  const style: CSSProperties = {};
  if (fg) style.color = fg;
  if (bg) style.background = bg;
  if (run.b) style.fontWeight = 700;
  if (run.i) style.fontStyle = "italic";
  if (run.u) style.textDecoration = "underline";
  if (run.d) style.opacity = 0.6;
  return Object.keys(style).length ? style : undefined;
}

/** The last `rows` rows that hold anything: a shell that has just cleared its screen shows its prompt, not blank lines. */
export function previewRows(preview: TerminalRun[][] | undefined, rows = 6): TerminalRun[][] {
  const lines = preview ?? [];
  let end = lines.length;
  while (end > 0 && !lines[end - 1].some((run) => run.t.trim())) end -= 1;
  return lines.slice(Math.max(0, end - rows), end);
}

export type TerminalFilter = "all" | "container" | "host" | "staff" | "finished";
export const FILTERS: TerminalFilter[] = ["all", "container", "host", "staff", "finished"];

export function running(row: TerminalView): boolean {
  return row.status === "running";
}

/** Whether a terminal belongs under a filter. The environment and staff filters show what runs; a finished one is under Finished (and All). */
export function matches(row: TerminalView, filter: TerminalFilter): boolean {
  if (filter === "all") return true;
  if (filter === "finished") return !running(row);
  if (!running(row)) return false;
  if (filter === "staff") return row.owner.kind === "staff";
  return row.env === filter;
}

/** "6 open · 2 on host · 2 with staff": the running terminals only. */
export function headerCounts(rows: TerminalView[]): { open: number; host: number; staff: number } {
  const live = rows.filter(running);
  return { open: live.length, host: live.filter((r) => r.env === "host").length, staff: live.filter((r) => r.owner.kind === "staff").length };
}

function stamp(row: TerminalView): number {
  const at = Date.parse(row.last_output_at || row.exited_at || row.created_at || "");
  return Number.isFinite(at) ? at : 0;
}

/**
 * The cards in their groups: one per project in the order of `projects`, then the terminals of no
 * project (or of one the listing no longer knows). Inside a group the running come first, the most
 * recently active of them first; the finished follow.
 */
export function groupTerminals(rows: TerminalView[], projects: { id: string; name: string }[]): { key: string; name: string | null; rows: TerminalView[] }[] {
  const order = (a: TerminalView, b: TerminalView) => Number(running(b)) - Number(running(a)) || stamp(b) - stamp(a);
  const known = new Map(projects.map((p) => [p.id, p.name]));
  const groups: { key: string; name: string | null; rows: TerminalView[] }[] = [];
  for (const p of projects) {
    const inside = rows.filter((r) => r.project_id === p.id);
    if (inside.length) groups.push({ key: p.id, name: p.name, rows: inside.sort(order) });
  }
  const loose = rows.filter((r) => !r.project_id || !known.has(r.project_id));
  if (loose.length) groups.push({ key: "", name: null, rows: loose.sort(order) });
  return groups;
}

/** A span of time as a card's status line writes it: "40 s", "38 min", "1 h", "3 d". */
export function span(fromIso: string | null | undefined, now = Date.now()): string {
  const at = Date.parse(fromIso ?? "");
  if (!Number.isFinite(at)) return "";
  const s = Math.max(0, Math.round((now - at) / 1000));
  if (s < 60) return t("fmt.dur.s", { n: s });
  if (s < 3600) return t("fmt.dur.m", { n: Math.floor(s / 60) });
  if (s < 86400) return t("fmt.dur.h", { n: Math.floor(s / 3600) });
  return t("fmt.dur.d", { n: Math.floor(s / 86400) });
}

export type CardStatus = { text: string; level: "ok" | "warn" | "bad" | "idle" | "off" };

/**
 * The line under a card's preview. What another part of the app says the terminal is doing wins (a
 * staff member waiting for permission); then how it ended; then whether anyone is looking at it.
 */
export function cardStatus(row: TerminalView, now = Date.now()): CardStatus {
  if (!running(row)) {
    const ago = span(row.exited_at, now);
    if (row.status === "lost") return { text: t("term.card.lost", { ago }), level: "off" };
    const how = row.exit_signal ? t("term.card.signal", { signal: row.exit_signal }) : t("term.card.code", { code: row.exit_code ?? "?" });
    return { text: ago ? t("term.card.ended", { how, ago }) : how, level: row.exit_signal || (row.exit_code ?? 0) !== 0 ? "bad" : "off" };
  }
  if (row.activity?.status) {
    const words = t(`team.status.${row.activity.status}`);
    return { text: row.activity.label ? `${words} · ${row.activity.label}` : words, level: row.activity.level ?? "ok" };
  }
  if (row.activity?.label) return { text: row.activity.label, level: row.activity.level ?? "ok" };
  if (row.live && row.live.clients === 0) return { text: t("term.card.unwatched", { t: span(row.last_input_at || row.created_at, now) }), level: "idle" };
  return { text: t("term.card.running", { t: span(row.created_at, now) }), level: "ok" };
}

/** A terminal "End idle" takes: a shell the host is sure sits at its prompt, nobody looking at it,
 *  and nobody's work — not a staff member's, not one another part of the app is waiting on. */
export function idleUnwatched(row: TerminalView): boolean {
  return running(row) && row.at_prompt === true && row.live?.clients === 0 && row.owner.kind !== "staff" && !row.activity;
}

/** How long past which an unwatched terminal is marked as one to clean up. */
export const STALE_MS = 24 * 3600_000;

/**
 * How long a running terminal nobody watches has gone untouched, when that is past `STALE_MS`; ""
 * otherwise. Typed input counts, and so does output: a server printing its log is in use. A staff
 * member's terminal is never stale, its member is the one who watches it.
 */
export function staleFor(row: TerminalView, now = Date.now()): string {
  if (!running(row) || row.live?.clients !== 0 || row.owner.kind === "staff") return "";
  const stamps = [row.last_input_at, row.last_output_at, row.created_at].map((s) => Date.parse(s ?? "")).filter(Number.isFinite);
  if (!stamps.length) return "";
  const latest = Math.max(...stamps);
  return now - latest >= STALE_MS ? span(new Date(latest).toISOString(), now) : "";
}

/** Where a terminal's owner lives in the app, when it has a page: the session, or the project's team. */
export function ownerPath(row: TerminalView, sessionPath: (id: string) => string, teamPath: (projectId: string) => string): string | null {
  if (row.owner.kind === "session" && row.owner.id) return sessionPath(row.owner.id);
  if ((row.owner.kind === "staff" || row.owner.kind === "project") && row.project_id) return teamPath(row.project_id);
  return null;
}

/** The owner line: "session «Checkout page»", "staff Ira · project «Bakery»", "free · ~/work", "project «Bakery»". */
export function ownerLine(row: TerminalView, projectName: string | null): string {
  const label = row.owner.label || "";
  if (row.owner.kind === "session") return t("term.card.session", { name: label || t("term.card.unnamed") });
  if (row.owner.kind === "staff") return projectName ? t("term.card.staff.project", { name: label || t("term.card.unnamed"), project: projectName }) : t("term.card.staff", { name: label || t("term.card.unnamed") });
  if (row.owner.kind === "project") return t("term.card.project", { name: label || projectName || t("term.card.unnamed") });
  return t("term.card.free", { path: row.cwd });
}
