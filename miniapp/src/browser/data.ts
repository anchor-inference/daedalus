// What the app reads about browsers from the host, and the few things it asks of it. The listing is
// the truth about which groups an owner has and who drives them; the host's event stream says when to
// read it again (`browser.opened`, `browser.needs_you`, `browser.returned`, `browser.closed`, and the
// lighter `browser.activity` and `browser.control`), and the poll is the safety net while the stream
// is down, as for every other list.

import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { api, telegram, type BrowserActionRow, type BrowserControl, type BrowserDraft, type BrowserGroup, type BrowserList, type BrowserRecording, type BrowserSiteNote, type BrowserStep, type BrowserWorkflow, type BrowserWorkflows } from "../api";
import { useEvent, useStreamUp } from "../events";
import { invalidate, useQuery } from "../store";
import { LiveView, type LiveSnapshot } from "./live";
import type { Tier } from "./protocol";
import { savingData } from "./model";

const enc = encodeURIComponent;
export const BROWSERS_KEY = "/api/browsers";

/** Whose groups: an agent session's (including Daedalus staff and the orchestrators), or a CLI staff member's. */
export type BrowserOwner = { session: string } | { staff: string };

export function browsersKey(owner: BrowserOwner): string {
  return "session" in owner ? `${BROWSERS_KEY}?session=${enc(owner.session)}` : `${BROWSERS_KEY}?staff=${enc(owner.staff)}`;
}

/** The owner's groups, newest activity first, or an empty list while nothing is known. */
export function useBrowsers(owner: BrowserOwner | null): { groups: BrowserGroup[]; available: boolean; loaded: boolean } {
  const live = useStreamUp();
  const key = owner ? browsersKey(owner) : null;
  const query = useQuery<BrowserList>(key, { pollMs: live ? 60000 : 8000, staleMs: 1500 });
  const id = owner ? ("session" in owner ? owner.session : owner.staff) : "";
  useEvent(["browser."], (event) => {
    if (!owner) return;
    const mine = "session" in owner ? event.session_id === id || event.payload?.session_id === id : event.staff_id === id || event.payload?.staff_id === id;
    if (mine) invalidate(key!);
  }, [key]);
  const data = query.data;
  const groups = data && Array.isArray(data.groups) ? data.groups : [];
  return { groups, available: !!data?.available, loaded: data !== undefined };
}

/** One group on its own, for the full-page viewer. */
export function useBrowserGroup(id: string | null): BrowserGroup | null {
  const live = useStreamUp();
  const key = id ? `${BROWSERS_KEY}/${enc(id)}` : null;
  const query = useQuery<BrowserGroup>(key, { pollMs: live ? 60000 : 8000, staleMs: 1500 });
  useEvent(["browser."], (event) => {
    if (id && (event.payload?.group_id === id || !event.payload?.group_id)) invalidate(key!);
  }, [key]);
  return query.data && typeof query.data === "object" && "id" in query.data ? query.data : null;
}

/** The group's action log, re-read when the host says it moved. */
export function useActions(group: string | null, limit = 100): BrowserActionRow[] {
  const key = group ? `${BROWSERS_KEY}/${enc(group)}/actions?limit=${limit}` : null;
  const query = useQuery<{ actions: BrowserActionRow[] }>(key, { pollMs: 30000, staleMs: 2000 });
  useEvent(["browser."], (event) => {
    if (group && event.payload?.group_id === group) invalidate(key!);
  }, [key]);
  return query.data && Array.isArray(query.data.actions) ? query.data.actions : [];
}

/** The group's recording: whether it is on, and its keyframes, oldest first. */
export function useRecording(group: string | null): BrowserRecording {
  const key = group ? `${BROWSERS_KEY}/${enc(group)}/recording` : null;
  const query = useQuery<BrowserRecording>(key, { pollMs: 15000, staleMs: 2000 });
  useEvent(["browser."], (event) => {
    if (group && event.payload?.group_id === group) invalidate(key!);
  }, [key]);
  const data = query.data;
  return { recording: data?.recording ?? { frames: false, human: false }, frames: data && Array.isArray(data.frames) ? data.frames : [] };
}

/** Read the recording again soon: an action was just done, and its keyframe is taken once the page settles. */
export function recordingMoved(group: string): void {
  window.setTimeout(() => invalidate(`${BROWSERS_KEY}/${enc(group)}/recording`), 900);
}

/** Switch the recording of keyframes; only the operator can. */
export async function setRecording(group: string, frames: boolean): Promise<void> {
  await api.post(`${BROWSERS_KEY}/${enc(group)}/recording`, { frames });
  invalidate(`${BROWSERS_KEY}/${enc(group)}/recording`);
}

export async function deleteRecording(group: string): Promise<void> {
  await api.delete(`${BROWSERS_KEY}/${enc(group)}/recording`);
  invalidate(`${BROWSERS_KEY}/${enc(group)}/recording`);
}

/** The group's recording of the operator's steps: the one on now, and its finished ones. */
export function useWorkflows(group: string | null): BrowserWorkflows & { loaded: boolean } {
  const key = group ? `${BROWSERS_KEY}/${enc(group)}/workflow` : null;
  const query = useQuery<BrowserWorkflows>(key, { pollMs: 30000, staleMs: 1500 });
  useEvent(["browser."], (event) => {
    if (group && event.payload?.group_id === group) invalidate(key!);
  }, [key]);
  const data = query.data;
  return { workflow: data?.workflow ?? null, recent: data && Array.isArray(data.recent) ? data.recent : [], loaded: data !== undefined };
}

/** Read the group's recordings again: one just started or stopped, as the live view said. */
export function workflowsMoved(group: string): void {
  invalidate(`${BROWSERS_KEY}/${enc(group)}/workflow`);
}

/** Start recording the operator's own steps; the browser must be theirs. */
export async function startWorkflow(group: string, values: BrowserWorkflow["values"]): Promise<BrowserWorkflow> {
  const out = await api.post<{ workflow: BrowserWorkflow }>(`${BROWSERS_KEY}/${enc(group)}/workflow`, { values });
  workflowsMoved(group);
  return out.workflow;
}

export async function stopWorkflow(group: string): Promise<BrowserWorkflow> {
  const out = await api.post<{ workflow: BrowserWorkflow }>(`${BROWSERS_KEY}/${enc(group)}/workflow/stop`, {});
  workflowsMoved(group);
  return out.workflow;
}

/** "This is what done looks like": the words selected on the page, else its heading. */
export async function markWorkflow(group: string, tab: string | null): Promise<BrowserStep> {
  const out = await api.post<{ step: BrowserStep }>(`${BROWSERS_KEY}/${enc(group)}/workflow/mark`, tab ? { tab_id: tab } : {});
  return out.step;
}

export async function draftWorkflow(group: string, id: string, goal: string): Promise<BrowserDraft> {
  const out = await api.post<BrowserDraft>(`${BROWSERS_KEY}/workflows/${enc(id)}/draft`, { goal });
  workflowsMoved(group);
  invalidate(NOTES_KEY);
  return out;
}

export async function discardWorkflow(group: string, id: string): Promise<void> {
  await api.delete(`${BROWSERS_KEY}/workflows/${enc(id)}`);
  workflowsMoved(group);
}

export const NOTES_KEY = "/api/browsers/notes";

/** The operator's own words for a note or a procedure. */
export async function editNote(id: string, text: string, title?: string): Promise<BrowserSiteNote> {
  const out = await api.patch<{ note: BrowserSiteNote }>(`${NOTES_KEY}/${enc(id)}`, title === undefined ? { text } : { text, title });
  invalidate(NOTES_KEY);
  return out.note;
}

export async function approveNote(id: string): Promise<BrowserSiteNote> {
  const out = await api.post<{ note: BrowserSiteNote }>(`${NOTES_KEY}/${enc(id)}/approve`, {});
  invalidate(NOTES_KEY);
  return out.note;
}

export async function deleteNote(id: string): Promise<void> {
  await api.delete(`${NOTES_KEY}/${enc(id)}`);
  invalidate(NOTES_KEY);
}

function refreshLists(): void {
  invalidate(BROWSERS_KEY);
}

/**
 * Take control, give it back, pause or resume. Taking names this window's live client, which is the
 * one whose input the daemon then accepts; giving back may carry a note the agent is woken with.
 */
/**
 * Ask the page to become this many CSS pixels. The picture is that page drawn into the pane; left
 * at the size it was opened, a taller pane has an empty band under it. A failure leaves the band:
 * an older daemon, or a group that has since closed, must not take the tab down with it.
 */
export async function resizeViewport(group: string, w: number, h: number): Promise<void> {
  await api.post(`${BROWSERS_KEY}/${enc(group)}/viewport`, { w, h });
}

export async function setControl(group: string, body: { owner: BrowserControl["owner"]; client_id?: string | null; note?: string; reason?: string }): Promise<BrowserControl> {
  const out = await api.post<BrowserControl>(`${BROWSERS_KEY}/${enc(group)}/control`, Object.fromEntries(Object.entries(body).filter(([, v]) => v !== undefined && v !== null && v !== "")));
  refreshLists();
  return out;
}

export async function answerDialog(group: string, tab: string | null, accept: boolean, text?: string): Promise<void> {
  await api.post(`${BROWSERS_KEY}/${enc(group)}/dialog`, { accept, ...(tab ? { tab_id: tab } : {}), ...(text !== undefined ? { text } : {}) });
}

export async function closeBrowser(group: string): Promise<void> {
  await api.post(`${BROWSERS_KEY}/${enc(group)}/close`, {});
  refreshLists();
}

/**
 * A live view for as long as the component that holds it is mounted. The view is made in an effect
 * and closed in its cleanup, so a component that remounts (a panel reopened, React's development
 * double mount) never leaves a socket behind; `box` is read at every attach, never captured.
 */
export function useLiveView(group: string | null, tier: Tier, opts: { readOnly?: boolean; tab?: string; box: () => { max_w: number; max_h: number; dpr?: number; quality?: number } }): LiveView | null {
  const [view, setView] = useState<LiveView | null>(null);
  const box = useRef(opts.box);
  box.current = opts.box;
  useEffect(() => {
    if (!group) return;
    const v = new LiveView({ group, tier, tab: opts.tab, readOnly: opts.readOnly, box: () => box.current() });
    setView(v);
    return () => {
      v.close();
      setView((cur) => (cur === v ? null : cur));
    };
    // The tier and the tab change on the open socket (VIEW), not by a new one.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [group, opts.readOnly]);
  return view;
}

const EMPTY: LiveSnapshot = {
  state: { kind: "connecting" }, clientId: null, readOnly: true, control: null, tabs: [], active: null, viewing: null,
  viewers: { count: 0, others: [] }, action: null, recent: [], dialog: null, needs: null, meta: null, painted: false, workflow: null,
};
const none = () => () => undefined;

export function useLiveSnapshot(view: LiveView | null): LiveSnapshot {
  return useSyncExternalStore(view ? view.subscribe : none, view ? view.get : () => EMPTY);
}

/** The pip's rectangle as it was clicked, so the panel's viewer can grow out of it. Read once. */
let handoff: { group: string; rect: DOMRect; at: number } | null = null;

export function handOff(group: string, rect: DOMRect): void {
  handoff = { group, rect, at: Date.now() };
}

export function takeHandoff(group: string): DOMRect | null {
  const h = handoff;
  handoff = null;
  return h && h.group === group && Date.now() - h.at < 1500 ? h.rect : null;
}

/** A take-control asked for on the corner card, done by the panel once its live view has a client id. */
let pendingTake: { group: string; at: number } | null = null;

export function askTake(group: string): void {
  pendingTake = { group, at: Date.now() };
}

export function consumeTake(group: string): boolean {
  const p = pendingTake;
  if (!p || p.group !== group) return false;
  pendingTake = null;
  return Date.now() - p.at < 15000;
}

/** Whether this device should save data: its connection says so, or it is Telegram on a phone. */
export function deviceSaving(phone: boolean): boolean {
  return savingData(typeof navigator === "undefined" ? undefined : (navigator as unknown as { connection?: { saveData?: boolean; effectiveType?: string } }), phone && !!telegram()?.initData);
}
