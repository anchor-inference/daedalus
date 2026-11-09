// Opening and ending a terminal for the operator, wherever it is asked from: the dock, the Terminals
// screen and its full-screen view, the palette. The host answers 409 `over_cap` when the machine already runs as many terminals as the cap
// allows; for an agent that means waiting in line, for a person it is a question — a human choice is
// confirmed, never refused — so the same request goes again with `confirm: true` once they agree.

import { createElement, type ReactNode } from "react";
import { api, ApiError, TerminalCreate, TerminalView } from "../api";
import { confirmDialog } from "../ui/dialogs";
import { plural, t } from "../i18n";
import { errorText } from "../ui";

/** Whether an error is the host's "the cap is reached" rather than a real failure. */
export function overCap(error: unknown): error is ApiError {
  return error instanceof ApiError && error.status === 409 && error.data?.code === "over_cap";
}

/**
 * The new terminal, or null when the operator declined to go past the cap. Any other failure is
 * thrown for the caller to show, as is a failure of the confirmed retry.
 */
export async function createTerminalConfirmed(body: TerminalCreate): Promise<TerminalView | null> {
  try {
    return await api.createTerminal(body);
  } catch (error) {
    if (!overCap(error)) throw error;
    const ok = await confirmDialog({
      title: t("term.cap.title"),
      body: t("term.cap.body", { running: Number(error.data.running ?? 0), cap: Number(error.data.cap ?? 0) }),
      action: t("term.cap.action"),
    });
    if (!ok) return null;
    return await api.createTerminal({ ...body, confirm: true });
  }
}

/** The fields ending reads: who owns the terminal, and what the host says runs in it. */
export type Endable = Pick<TerminalView, "owner" | "title" | "status" | "at_prompt" | "running">;

/** What ending a terminal asks first, or null to end it at once.
 *
 *  Only a shell the host is sure sits at its prompt ends without a question (`at_prompt`, which the
 *  host sets from the shell's marks or the terminal's foreground process group, and leaves false
 *  whenever it cannot tell). Everything else asks and names what stops: the command line when the
 *  host knows it. A staff member's terminal always asks in the member's words: its program is the
 *  member's session, and ending it stops the member mid-task whether or not a command happens to be
 *  running at that moment. */
export function endQuestion(row: Endable, shownTitle?: string): { title: string; body: string } | null {
  const title = shownTitle || row.title || t("term.untitled");
  const running = row.running ? t("term.end.running", { command: row.running }) : "";
  if (row.owner.kind === "staff") return { title: t("term.end.staff.title"), body: [t("term.end.staff", { name: row.owner.label || t("term.card.unnamed") }), running].filter(Boolean).join(" ") };
  if (row.at_prompt) return null;
  return { title: t("term.end.title"), body: running || t("term.end.unsure", { title }) };
}

/**
 * End a terminal's process, after the question `endQuestion` asks, if any. The host is asked afresh
 * rather than trusting a listing that may be seconds old: a command started since must still be
 * asked about. A terminal that could not be read is asked about too. Resolves true when the terminal
 * is no longer running (ended here, or already), false when the operator declined or it failed.
 */
export async function endTerminal(id: string, shownTitle: string | undefined, toast: (text: string) => void): Promise<boolean> {
  let row: TerminalView | null = null;
  try {
    row = await api.terminal(id);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) return true;
  }
  if (row && row.status !== "running") return true;
  const question = row ? endQuestion(row, shownTitle) : { title: t("term.end.title"), body: t("term.end.unsure", { title: shownTitle || t("term.untitled") }) };
  if (question && !(await confirmDialog({ ...question, action: t("term.end"), danger: true }))) return false;
  try {
    await api.killTerminal(id);
    return true;
  } catch (error) {
    toast(errorText(error));
    return false;
  }
}

/** One line of a question about several terminals: its title, and what stops in it when anything does. */
export function bulkLine(row: Endable): { title: string; note: string } {
  const title = row.title || t("term.untitled");
  if (row.owner.kind === "staff") return { title, note: t("term.bulk.staff", { name: row.owner.label || t("term.card.unnamed") }) };
  if (row.at_prompt) return { title, note: "" };
  return { title, note: row.running ? row.running : t("term.bulk.unsure") };
}

/** How many requests a bulk action keeps in flight: enough to be quick, few enough not to flood the
 *  daemon, whose kill waits out a grace period per terminal. */
const BULK_PARALLEL = 4;

async function each<T>(items: T[], work: (item: T) => Promise<boolean>): Promise<number> {
  let next = 0;
  let done = 0;
  const lane = async () => {
    while (next < items.length) {
      const item = items[next++];
      if (await work(item)) done += 1;
    }
  };
  await Promise.all(Array.from({ length: Math.min(BULK_PARALLEL, items.length) }, lane));
  return done;
}

/**
 * End several terminals after one question that lists every one of them, and what stops in each.
 * A list always asks, even of idle shells: the operator chose a group, not each terminal, and the
 * list is how they see what the group holds. Resolves the number ended, or null when declined.
 */
export async function endTerminals(rows: (Endable & Pick<TerminalView, "id">)[], toast: (text: string) => void, title?: string): Promise<number | null> {
  const live = rows.filter((r) => r.status === "running");
  if (!live.length) return 0;
  const ok = await confirmDialog({ title: title ?? plural("term.bulk.end.title", live.length), body: bulkBody(live), action: t("term.end"), danger: true });
  if (!ok) return null;
  let failed = "";
  const done = await each(live, async (row) => {
    try {
      await api.killTerminal(row.id);
      return true;
    } catch (error) {
      failed = errorText(error);
      return false;
    }
  });
  if (failed) toast(failed);
  return done;
}

/** Forget several ended terminals; resolves the number forgotten. Nothing runs in them, so nothing asks. */
export async function removeTerminals(ids: string[], toast: (text: string) => void): Promise<number> {
  let failed = "";
  const done = await each(ids, async (id) => {
    try {
      await api.removeTerminal(id);
      return true;
    } catch (error) {
      failed = errorText(error);
      return false;
    }
  });
  if (failed) toast(failed);
  return done;
}

function bulkBody(rows: Endable[]): ReactNode {
  return createElement("ul", { className: "term-bulk-list" }, ...rows.map((row, i) => {
    const line = bulkLine(row);
    return createElement("li", { key: i }, createElement("span", { className: "term-bulk-title" }, line.title), line.note ? createElement("code", { className: "term-bulk-note" }, line.note) : null);
  }));
}
