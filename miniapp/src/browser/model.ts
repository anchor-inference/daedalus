// The browser's state as the app draws it, in plain functions: who drives, what the last actions were
// in words, when the corner preview shows and where it sits. Nothing here touches the page or the
// network, so each rule is a test rather than a screenshot.

import type { BrowserActionRow, BrowserGroup, BrowserStep } from "../api";
import { t } from "../i18n";
import type { ActionEvent, ViewControl } from "./protocol";

/** The agent's name as the browser shows it: a staff member's own, otherwise the app's. */
export function agentName(group: Pick<BrowserGroup, "owner">): string {
  return group.owner.kind === "staff" && group.owner.label ? group.owner.label : t("browser.agent");
}

/** What the panel's banner, the preview's dot and the header button say, in one word. */
export type DriveState = "acting" | "idle" | "you" | "other" | "paused" | "needs" | "closed";

/**
 * Who drives, from the most specific fact: a person's hand beats a request (the request is what the
 * hand is answering), a request beats a pause (a handoff pauses the agent with a reason), and the agent
 * acting or idle is what is left. `holder` comes from the live socket when there is one; the listing
 * says only that a human holds it, which is someone else unless this window's socket says "you".
 */
export function driveState(group: Pick<BrowserGroup, "status" | "control" | "acting"> & { needs_you: object | null }, live: ViewControl | null = null): DriveState {
  if (group.status === "closed" || group.status === "lost") return "closed";
  const control = live ?? group.control;
  if (control.owner === "human") return control.holder === "you" ? "you" : "other";
  if (group.needs_you) return "needs";
  if (control.owner === "paused") return "paused";
  return group.acting ? "acting" : "idle";
}

/**
 * What the agent asked the operator for, as far as this window knows: the live socket's own request
 * first; else the listing's, but only while the socket (when there is one) still says the agent is
 * paused — a request is a pause with a reason, and control going back to the agent or to a person
 * has answered it before the listing is read again.
 */
export function needsOf<N>(listed: N | null, live: { needs: N | null; control: ViewControl | null; clientId: string | null }): N | null {
  if (live.needs) return live.needs;
  if (live.clientId && live.control && live.control.owner !== "paused") return null;
  return listed;
}

/** The reasons a request can carry, the agent's own and the daemon's (`by: "daemon"`). */
export const NEED_REASONS = ["login", "captcha", "two_factor", "payment", "confirm", "field_forbidden", "basic_auth", "other"] as const;

/**
 * What a request asks for, in words: the agent's own sentence, or — for one the daemon raised by
 * itself (a password field the agent tried to type into, a CAPTCHA, a sign-in prompt), which may come
 * with no sentence — the reason's.
 */
export function needWords(need: { reason: string; what?: string } | null): string {
  if (!need) return "";
  if (need.what) return need.what;
  return (NEED_REASONS as readonly string[]).includes(need.reason) ? t(`browser.reason.${need.reason}`) : t("browser.needs");
}

/** The host part of an address, without `www.`; the address itself when it has none (about:blank). */
export function domainOf(url: string): string {
  try {
    const u = new URL(url);
    if (!u.hostname) return url;
    return u.hostname.replace(/^www\./, "") + (u.port ? `:${u.port}` : "");
  } catch {
    return url;
  }
}

/** Whether an address is served over a secure channel, for the lock beside it. */
export function secure(url: string): boolean {
  return /^https:\/\//i.test(url) || /^(about|chrome):/i.test(url);
}

/** A row of the action log in words: an i18n key and what fills it. */
export function actionWords(row: Pick<BrowserActionRow, "kind" | "element" | "name" | "text" | "text_len" | "keys" | "url" | "needs" | "download"> & { ok?: boolean; actor?: string }): { key: string; vars: Record<string, string | number> } {
  const what = row.name || row.element || "";
  switch (row.kind) {
    case "click":
    case "double_click":
    case "right_click":
    case "hover":
    case "check":
    case "uncheck":
    case "scroll":
    case "select":
    case "drag":
    case "upload":
      return { key: `browser.act.${row.kind}`, vars: { what } };
    case "type":
      return row.text ? { key: "browser.act.typed", vars: { what, text: row.text } } : { key: "browser.act.typedn", vars: { what, n: row.text_len ?? 0 } };
    case "press":
      return { key: "browser.act.press", vars: { keys: row.keys ?? "" } };
    case "navigate":
    case "open":
      return { key: "browser.act.open", vars: { where: domainOf(row.url ?? "") || what } };
    case "back":
    case "forward":
    case "reload":
      return { key: `browser.act.${row.kind}`, vars: {} };
    case "handoff":
      return { key: "browser.act.handoff", vars: { what: row.needs?.what ?? what } };
    case "dialog":
      // The page's own alert, which the browser accepted so it never stopped the agent: its words are
      // the row's element. Otherwise the agent answered a confirm or a prompt.
      return row.actor === "page" ? { key: "browser.act.dialog_auto", vars: { what } } : { key: "browser.act.dialog", vars: {} };
    case "download":
      return { key: "browser.act.download", vars: { name: row.download?.name ?? what } };
    case "take":
    case "give":
    case "pause":
      return { key: `browser.act.${row.kind}`, vars: {} };
    case "close":
      return { key: "browser.act.close", vars: {} };
    case "blocked":
      return { key: "browser.act.blocked", vars: { where: domainOf(row.url ?? "") || row.url || "" } };
    case "watch":
      return { key: "browser.act.watch", vars: { where: domainOf(row.url ?? "") || row.url || "" } };
    case "monitor":
      return { key: row.ok === false ? "browser.act.monitor.hit" : "browser.act.monitor.clean", vars: { where: domainOf(row.url ?? "") || row.url || "" } };
    default:
      return { key: "browser.act.other", vars: { kind: row.kind, what } };
  }
}

/** A live `action` event as a row of the log, so the drawer moves before the listing is read again. */
/**
 * One step of the operator's recording as the panel says it: the i18n key and its words. The element is
 * named as the page names it, and a secret step says only whose it is — nothing was kept of it.
 */
export function stepWords(step: BrowserStep): { key: string; vars: Record<string, string | number> } {
  const what = step.element?.name || step.element?.role || "";
  switch (step.action) {
    case "navigate":
      return step.go ? { key: `browser.step.go.${step.go}`, vars: {} } : { key: "browser.step.navigate", vars: { where: domainOf(step.to ?? "") || step.to || "" } };
    case "arrive":
      return { key: "browser.step.arrive", vars: { where: step.title || domainOf(step.to ?? "") || step.to || "" } };
    case "click":
    case "double_click":
    case "right_click":
    case "check":
    case "uncheck":
      return step.element ? { key: `browser.step.${step.action}`, vars: { what } } : { key: "browser.step.point", vars: {} };
    case "type":
      if (step.value !== undefined) return { key: step.submit ? "browser.step.typed.submit" : "browser.step.typed", vars: { what, text: step.value } };
      return { key: step.submit ? "browser.step.blank.submit" : "browser.step.blank", vars: { what, slot: step.slot ?? "" } };
    case "select":
      return step.option ? { key: "browser.step.select", vars: { what, option: step.option } } : { key: "browser.step.select.blank", vars: { what, slot: step.slot ?? "" } };
    case "press":
      return { key: (step.count ?? 1) > 1 ? "browser.step.press.n" : "browser.step.press", vars: { keys: step.keys ?? "", n: step.count ?? 1 } };
    case "scroll":
      return { key: `browser.step.scroll.${step.direction === "up" || step.direction === "left" || step.direction === "right" ? step.direction : "down"}`, vars: {} };
    case "handoff":
      return { key: `browser.step.handoff.${step.reason === "two_factor" || step.reason === "payment" ? step.reason : "login"}`, vars: {} };
    case "expect":
      return { key: "browser.step.expect", vars: { text: step.text || step.title || "" } };
    case "dialog":
      return { key: step.accept ? "browser.step.dialog.accept" : "browser.step.dialog.dismiss", vars: { text: step.text ?? "" } };
    case "download":
      return { key: "browser.step.download", vars: { name: step.text ?? "" } };
    case "tab":
      return { key: "browser.step.tab", vars: { where: step.title || domainOf(step.to ?? "") } };
  }
  return { key: "browser.step.other", vars: { what: String(step.action) } };
}

/** The steps a procedure is made of: the ones that do something, not where the page went or scrolled. */
export function doingSteps(steps: BrowserStep[]): number {
  return steps.filter((s) => s.action !== "arrive" && s.action !== "scroll" && s.action !== "expect").length;
}

export function rowOfEvent(e: ActionEvent): BrowserActionRow {
  return {
    id: e.id,
    action_id: e.id,
    at: new Date(e.at).toISOString(),
    actor: e.actor,
    kind: e.kind,
    element: e.element,
    name: e.name,
    tab: e.tab,
    point: e.point ?? null,
    box: e.box ?? null,
    ...(e.text_len !== undefined ? { text_len: e.text_len } : {}),
    ...(e.keys ? { keys: e.keys } : {}),
  };
}

/** The listing's rows and the live ones merged by id, newest first, the listing winning on detail. */
export function mergeActions(listed: BrowserActionRow[], live: BrowserActionRow[], max = 200): BrowserActionRow[] {
  // The host's rows are numbered by its audit and name the daemon's action they were; a live row is
  // the daemon's action itself. One action is one row, whichever came first.
  const byId = new Map<string, BrowserActionRow>();
  const listedActions = new Set(listed.map((r) => r.action_id).filter(Boolean));
  for (const r of live) if (!listedActions.has(r.id)) byId.set(r.id, r);
  for (const r of listed) byId.set(r.id, { ...byId.get(r.id), ...r });
  return [...byId.values()].sort((a, b) => (a.at < b.at ? 1 : a.at > b.at ? -1 : 0)).slice(0, max);
}

// ── the corner preview ──────────────────────────────────────────────────────────────────────

/**
 * Below this width of the conversation the card becomes a pill, so it never covers what is being read.
 * Not the 720 first proposed: at 1440 px with the sidebar and the panel open the column is 648 px, and
 * the preview would have been a pill in the layout it is most often seen in. Under 520 (a dual view's
 * pane, a narrow window) the card would cover half the text.
 */
export const PIP_PILL_BELOW = 520;

/**
 * The same threshold on a touch screen with no hover. A landscape tablet with the panel open leaves
 * the conversation about 650 px, where the card sat over the run summary and cut a sentence in half;
 * a finger cannot hover to reveal the card's buttons either, so dragging it aside was the only way
 * out. On touch the card waits for a column this wide, which a tablet reaches only with the panel shut.
 */
export const PIP_PILL_BELOW_TOUCH = 960;

export type Corner = "tr" | "tl" | "br" | "bl";
export const CORNERS: Corner[] = ["tr", "tl", "br", "bl"];

/** The corner nearest to where a drag let go, by the card's centre against the column's. */
export function nearestCorner(cx: number, cy: number, columnW: number, columnH: number): Corner {
  const right = cx >= columnW / 2;
  const bottom = cy >= columnH / 2;
  return `${bottom ? "b" : "t"}${right ? "r" : "l"}` as Corner;
}

const CORNER_KEY = "daedalus.browser.pip.corner";

export function readCorner(storage: Pick<Storage, "getItem"> | null = safeStorage()): Corner {
  try {
    const v = storage?.getItem(CORNER_KEY);
    return CORNERS.includes(v as Corner) ? (v as Corner) : "tr";
  } catch {
    return "tr";
  }
}

export function rememberCorner(corner: Corner, storage: Pick<Storage, "setItem"> | null = safeStorage()): void {
  try {
    storage?.setItem(CORNER_KEY, corner);
  } catch {
    /* a webview without site data: the corner lasts the visit */
  }
}

/**
 * The group the corner preview shows for a session: one that needs the operator first, then the one
 * a person drives, then the most recently active. A closed browser shows nothing.
 */
export function pipGroup(groups: BrowserGroup[]): BrowserGroup | null {
  const open = groups.filter((g) => g.status !== "closed" && g.status !== "lost");
  if (!open.length) return null;
  const rank = (g: BrowserGroup) => (g.needs_you ? 3 : g.control.owner === "human" ? 2 : g.acting ? 1 : 0);
  return [...open].sort((a, b) => rank(b) - rank(a) || (a.last_activity_at < b.last_activity_at ? 1 : -1))[0];
}

/**
 * A card hidden with ✕ stays hidden for its group until the group does something new — a newer
 * action than the one on screen when it was hidden — or asks for the operator. `hidden` maps a
 * group to the activity time it was hidden at; the visit forgets it.
 */
export function pipHidden(group: Pick<BrowserGroup, "id" | "needs_you" | "last_activity_at">, hidden: ReadonlyMap<string, string>): boolean {
  const at = hidden.get(group.id);
  if (at === undefined) return false;
  if (group.needs_you) return false;
  return group.last_activity_at <= at;
}

/** How many other tabs or groups the card stands for, for its "+2" chip. */
export function extraCount(groups: BrowserGroup[], shown: BrowserGroup): number {
  const open = groups.filter((g) => g.status !== "closed" && g.status !== "lost");
  const tabs = shown.tabs.length;
  return Math.max(0, open.length - 1) + Math.max(0, tabs - 1);
}

/** Data saving, read from the connection the browser reports, and always inside Telegram on a phone. */
export function savingData(nav: { connection?: { saveData?: boolean; effectiveType?: string } } | undefined, telegramPhone: boolean): boolean {
  if (telegramPhone) return true;
  const c = nav?.connection;
  return !!c && (c.saveData === true || c.effectiveType === "2g" || c.effectiveType === "slow-2g" || c.effectiveType === "3g");
}

function safeStorage(): Storage | null {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    return null;
  }
}
