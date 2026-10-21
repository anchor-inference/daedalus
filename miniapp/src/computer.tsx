// The Computer control of a conversation: control of the operator's own desktop given to this
// conversation for a while, within one window, one app or the whole desktop, and taken back.
//
// Only the native application offers it (`GET /api/computer` says `native: true`); everywhere else
// nothing here is drawn. The rules that must hold even when the agent misbehaves are the desktop
// daemon's — the lease, its scope, the pause on the operator's own hand, Stop — and this control
// only asks for them through the host, which asks the launcher. A refusal is shown by the key the
// host sends beside its code (`error.words`), rendered in the reader's language, and never as the
// number; the host's English sentence (`error.explanation`) is the fallback for a key this build
// does not know yet, so a newer host never puts a bare key on the page.
//
// Pause and Resume, honestly mapped. The daemon has no "pause" an operator can call: the agent is
// paused by the operator's own hand — any mouse movement or key press — and the pause ends by itself
// after a quiet moment or with Resume (`/resume`). A Pause button here would have to be Stop under
// another name, and a button that says "pause" but ends the grant would be a lie the first time the
// operator pressed it expecting to carry on. So the banner offers Stop (`/stop`: the action in flight
// is cut short, everything held down is released, the grant ends), says that touching the mouse or
// keyboard pauses the agent, and offers Resume only while the daemon says it is paused
// (`human.paused`). `/revoke` is the quieter way to end a grant without Stop's emergency release; it
// is offered for a grant another conversation holds, which this conversation has no reason to stop.

import { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "./api";
import { DICT, t } from "./i18n";
import { Icon } from "./icons";
import { invalidate, useQuery } from "./store";
import { errorText } from "./ui";
import { Segmented } from "./ui/components";
import { Sheet } from "./ui/dialogs";

export const COMPUTER = "/api/computer";
export const COMPUTER_APPS = "/api/computer/apps";

/** The choices of how long, in minutes, and the bound the host holds a grant to. */
export const PRESETS = [10, 30, 60] as const;
export const MAX_MINUTES = 240;

/** While the control or the banner is on screen the state is read this often, so a pause, a Stop
 *  from the shell or the end of the grant shows within moments. */
export const POLL_MS = 3000;
/** The header button alone reads it rarely, only to notice a grant made from the desktop shell. */
export const IDLE_POLL_MS = 20000;

export type PermissionState = "granted" | "denied" | "unknown" | "not_applicable";
export type ScopeKind = "window" | "app" | "display" | "desktop";
export type ComputerScope = { kind: ScopeKind; window?: string; app?: string; display?: string };
export type ComputerLease = { id: string; session: string; expires_at?: string; scope?: ComputerScope; max_actions?: number; actions?: number };
export type ComputerState = {
  native: boolean;
  available: boolean;
  reason?: string;
  reason_words?: Words;
  os?: string;
  session_type?: string;
  permissions?: { screen?: PermissionState; accessibility?: PermissionState; input_monitoring?: PermissionState };
  secure?: { locked: boolean; reason?: string };
  lease?: { active: boolean; lease?: ComputerLease; remaining_ms?: number };
  human?: { paused: boolean; since?: string; resumes_at?: string };
  settings_links?: { screen?: string; accessibility?: string };
};
export type ComputerWindow = { id: string; title: string; focused?: boolean; minimized?: boolean };
export type ComputerApp = { id: string; name: string; reachable?: boolean; elevated?: boolean; windows: ComputerWindow[] };

/** A sentence the host names by a key of the dictionary and the values for its holes. */
export type Words = { key: string; vars?: Record<string, string | number> };

/** The host's words in the reader's language, or nothing when this build has no such key. */
export function wordsText(words: unknown): string {
  if (!words || typeof words !== "object") return "";
  const { key, vars } = words as { key?: unknown; vars?: unknown };
  if (typeof key !== "string" || !DICT[key]) return "";
  return t(key, vars && typeof vars === "object" ? (vars as Record<string, string | number>) : undefined);
}

/** Where this conversation stands: nothing to draw, a desktop that cannot be driven, control here
 *  (and paused by the operator's hand), control in another conversation, or nobody's. */
export type ComputerView = "hidden" | "unavailable" | "mine" | "paused" | "other" | "free";

export function computerView(state: ComputerState | undefined, sessionId: string): ComputerView {
  if (!state || !state.native) return "hidden";
  if (!state.available) return "unavailable";
  const lease = state.lease;
  if (!lease?.active || !lease.lease) return "free";
  if (lease.lease.session !== sessionId) return "other";
  return state.human?.paused ? "paused" : "mine";
}

/** The sentence for a failed request: a desktop refusal in the reader's language, else its English
 *  explanation, otherwise the usual text. */
export function refusalText(e: unknown): string {
  if (e instanceof ApiError) {
    const error = e.data.error;
    const said = error && typeof error === "object" ? wordsText((error as { words?: unknown }).words) : "";
    if (said) return said;
    if (error && typeof error === "object" && typeof (error as { explanation?: unknown }).explanation === "string") {
      const explanation = (error as { explanation: string }).explanation.trim();
      if (explanation) return explanation;
    }
  }
  return errorText(e);
}

/** "12 min left", "40 s left": rounded up, so the last minute never reads as none. */
export function timeLeft(ms: number): string {
  const seconds = Math.max(0, Math.ceil(ms / 1000));
  if (seconds >= 60) return t("cu.left.min", { n: Math.ceil(seconds / 60) });
  return t("cu.left.sec", { n: seconds });
}

/** The time a grant has left, counted down here between reads rather than jumping every poll. */
function useRemaining(state: ComputerState | undefined, ticking: boolean): number | null {
  const remaining = state?.lease?.remaining_ms;
  const deadline = useMemo(() => (typeof remaining === "number" ? Date.now() + remaining : null), [state, remaining]);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!ticking || deadline === null) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [ticking, deadline]);
  return deadline === null ? null : Math.max(0, deadline - now);
}

async function send(path: string, body: unknown, onError: (text: string) => void): Promise<boolean> {
  try {
    await api.post(path, body);
    return true;
  } catch (e) {
    onError(refusalText(e));
    return false;
  } finally {
    invalidate(COMPUTER);
  }
}

/** The state as the conversation's screen needs it, and where this conversation stands. Polled only
 *  in the native application, and there rarely, just to notice a grant made from the desktop shell;
 *  while the control is open its sheet polls instead, and on a server nothing does. The probe is the
 *  same cached entry without a timer: whether to poll is only known once it has answered. */
export function useComputer(sessionId: string, open: boolean): { state: ComputerState | undefined; view: ComputerView } {
  const probe = useQuery<ComputerState>(COMPUTER, { staleMs: 2000 });
  const native = !!probe.data?.native;
  const query = useQuery<ComputerState>(native ? COMPUTER : null, { pollMs: open ? 0 : IDLE_POLL_MS, staleMs: 2000 });
  const state = query.data ?? probe.data;
  return { state, view: computerView(state, sessionId) };
}

/** The header's button: drawn only in the native application, lit while this conversation holds the
 *  computer, and the way into the control. The phone's menu offers the same control as a tile. */
export function ComputerButton({ view, onOpen }: { view: ComputerView; onOpen: () => void }) {
  if (view === "hidden") return null;
  const holding = view === "mine" || view === "paused";
  return (
    <button
      type="button"
      className={`iconbtn computer-button ${holding ? "on" : ""}`}
      onClick={onOpen}
      aria-label={t("cu.button")}
      title={holding ? t("cu.banner.title") : t("cu.button")}
      data-computer={view}
    >
      <Icon name="monitor" />
    </button>
  );
}

/** The banner above the composer while this conversation controls the computer: what is happening,
 *  how long it has left, Stop, and Resume while the operator's hand has paused it. */
export function ComputerBanner({ sessionId }: { sessionId: string }) {
  const probe = useQuery<ComputerState>(COMPUTER, { staleMs: 2000 });
  const shown = computerView(probe.data, sessionId);
  const holding = shown === "mine" || shown === "paused";
  const query = useQuery<ComputerState>(holding ? COMPUTER : null, { pollMs: POLL_MS, staleMs: 2000 });
  const state = query.data ?? probe.data;
  const view = computerView(state, sessionId);
  const left = useRemaining(state, view === "mine" || view === "paused");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => setError(""), [view]);
  if (view !== "mine" && view !== "paused") return null;
  async function act(path: string) {
    setBusy(true);
    setError("");
    await send(path, {}, setError);
    setBusy(false);
  }
  const paused = view === "paused";
  return (
    <div className="setting-notices computer-banner-wrap">
      <div className="setting-notice tone-warning computer-banner" role="status" data-computer={view}>
        <Icon name={paused ? "pause" : "monitor"} size={14} />
        <span className="setting-notice-text">
          <b>{paused ? t("cu.banner.paused") : t("cu.banner.title")}</b>
          <span className="sub"> {[left !== null ? timeLeft(left) : "", paused ? t("cu.banner.paused.sub") : t("cu.banner.hint")].filter(Boolean).join(" · ")}</span>
          {error && <span className="computer-error" role="alert">{error}</span>}
        </span>
        {paused && <button type="button" className="btn small" disabled={busy} onClick={() => void act(`${COMPUTER}/resume`)}>{t("cu.resume")}</button>}
        <button type="button" className="btn small danger solid" disabled={busy} onClick={() => void act(`${COMPUTER}/stop`)}><Icon name="stop" size={14} />{t("cu.stop")}</button>
      </div>
    </div>
  );
}

/** Why the computer cannot be driven: one sentence in the reader's language, and under it the
 *  launcher's own English reason (no daemon in this build, a Wayland session) as the detail that
 *  tells the operator what to change. Without a key, the reason is all there is. */
function Unavailable({ state }: { state: ComputerState | undefined }) {
  const said = wordsText(state?.reason_words);
  const detail = state?.reason || "";
  // The host's own reasons are the key's English word for word; repeating one under its translation
  // would only put the English back on the page.
  const english = state?.reason_words && DICT[state.reason_words.key]?.en;
  return (
    <div className="sub computer-reason">
      {said || detail || t("cu.unavailable")}
      {said && detail && detail !== english && <div className="sub computer-reason-detail">{detail}</div>}
    </div>
  );
}

/** The control itself: where control stands, the grant, and what the system allows. */
export function ComputerSheet({ sessionId, toast, onClose }: { sessionId: string; toast: (text: string) => void; onClose: () => void }) {
  const query = useQuery<ComputerState>(COMPUTER, { pollMs: POLL_MS, staleMs: 2000 });
  const state = query.data;
  const view = computerView(state, sessionId);
  const left = useRemaining(state, view !== "free");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => setError(""), [view]);
  async function act(path: string, body: unknown, done?: string) {
    setBusy(true);
    setError("");
    const ok = await send(path, body, setError);
    setBusy(false);
    if (ok && done) toast(done);
    return ok;
  }
  return (
    <Sheet title={t("cu.title")} onClose={onClose} className="computer-sheet">
      {view === "unavailable" || view === "hidden" ? (
        <Unavailable state={state} />
      ) : (
        <>
          <div className="sub">{t("cu.sub")}</div>
          {state?.secure?.locked && <div className="setting-notice tone-warning computer-note"><Icon name="lock" size={14} /><span className="setting-notice-text">{t("cu.locked")}</span></div>}
          {(view === "mine" || view === "paused") && (
            <div className="setting-notice tone-warning computer-status" data-computer={view}>
              <Icon name={view === "paused" ? "pause" : "monitor"} size={14} />
              <span className="setting-notice-text">
                <b>{view === "paused" ? t("cu.banner.paused") : t("cu.banner.title")}</b>
                <span className="sub"> {[left !== null ? timeLeft(left) : "", scopeWords(state?.lease?.lease?.scope)].filter(Boolean).join(" · ")}</span>
              </span>
              {view === "paused" && <button type="button" className="btn small" disabled={busy} onClick={() => void act(`${COMPUTER}/resume`, {})}>{t("cu.resume")}</button>}
              <button type="button" className="btn small danger solid" disabled={busy} onClick={() => void act(`${COMPUTER}/stop`, {}, t("cu.stopped"))}><Icon name="stop" size={14} />{t("cu.stop")}</button>
            </div>
          )}
          {view === "other" && (
            <div className="setting-notice computer-status" data-computer="other">
              <Icon name="monitor" size={14} />
              <span className="setting-notice-text">
                <b>{t("cu.other")}</b>
                <span className="sub"> {[left !== null ? timeLeft(left) : "", t("cu.other.sub")].filter(Boolean).join(" · ")}</span>
              </span>
              <button type="button" className="btn small" disabled={busy} onClick={() => void act(`${COMPUTER}/revoke`, { session_id: state?.lease?.lease?.session }, t("cu.revoked"))}>{t("cu.revoke")}</button>
            </div>
          )}
          {error && <div className="runerror computer-error" role="alert"><Icon name="alert" size={14} /><span>{error}</span></div>}
          {view !== "mine" && view !== "paused" && (
            <GrantForm busy={busy} onGrant={(scope, minutes) => void act(`${COMPUTER}/grant`, { session_id: sessionId, scope, minutes }, t("cu.granted", { n: minutes }))} />
          )}
          {state && <Permissions state={state} busy={busy} onAsk={(which) => void act(`${COMPUTER}/permissions`, { which })} />}
        </>
      )}
    </Sheet>
  );
}

function scopeWords(scope: ComputerScope | undefined): string {
  if (!scope) return "";
  return t(`cu.scope.now.${scope.kind}`);
}

function GrantForm({ busy, onGrant }: { busy: boolean; onGrant: (scope: ComputerScope, minutes: number) => void }) {
  const [minutes, setMinutes] = useState<number>(30);
  const [typed, setTyped] = useState("30");
  const [kind, setKind] = useState<"window" | "app" | "desktop">("window");
  const [picked, setPicked] = useState("");
  const apps = useQuery<{ apps: ComputerApp[] }>(kind === "desktop" ? null : COMPUTER_APPS, { pollMs: POLL_MS * 2 });
  const list = apps.data?.apps ?? [];
  useEffect(() => setPicked(""), [kind]);
  const valid = Number.isInteger(minutes) && minutes >= 1 && minutes <= MAX_MINUTES;
  const ready = valid && (kind === "desktop" || !!picked);
  function choose(value: number) {
    setMinutes(value);
    setTyped(String(value));
  }
  function grant() {
    if (!ready) return;
    const scope: ComputerScope = kind === "desktop" ? { kind } : kind === "app" ? { kind, app: picked } : { kind, window: picked };
    onGrant(scope, minutes);
  }
  return (
    <div className="computer-grant">
      <div className="section-title">{t("cu.grant.title")}</div>
      <div className="computer-line">
        <span className="computer-label">{t("cu.minutes")}</span>
        <Segmented<string> value={PRESETS.includes(minutes as 10) ? String(minutes) : ""} label={t("cu.minutes")}
          options={PRESETS.map((n) => ({ id: String(n), label: t("cu.preset", { n }) }))} onChange={(id) => choose(Number(id))} />
        <input className={`field computer-minutes ${valid ? "" : "invalid"}`} type="number" inputMode="numeric" min={1} max={MAX_MINUTES} step={1}
          value={typed} aria-label={t("cu.minutes.custom")} aria-invalid={!valid}
          onChange={(e) => { setTyped(e.target.value); setMinutes(Number(e.target.value)); }} />
      </div>
      {!valid && <div className="sub attn">{t("cu.minutes.bound", { n: MAX_MINUTES })}</div>}
      <div className="computer-line">
        <span className="computer-label">{t("cu.scope")}</span>
        <Segmented<"window" | "app" | "desktop"> value={kind} label={t("cu.scope")}
          options={[{ id: "window", label: t("cu.scope.window") }, { id: "app", label: t("cu.scope.app") }, { id: "desktop", label: t("cu.scope.desktop") }]} onChange={setKind} />
      </div>
      {kind === "desktop" ? (
        <div className="sub attn">{t("cu.scope.desktop.warn")}</div>
      ) : (
        <div className="computer-list" role="radiogroup" aria-label={t(kind === "window" ? "cu.pick.window" : "cu.pick.app")}>
          {apps.loading && <div className="sub">{t("cu.apps.loading")}</div>}
          {apps.error && !list.length && <div className="sub attn">{apps.error}</div>}
          {!apps.loading && !apps.error && !list.length && <div className="sub">{t("cu.apps.none")}</div>}
          {kind === "app"
            ? list.map((app) => (
              <label key={app.id} className={`computer-row ${app.reachable === false ? "off" : ""}`}>
                <input type="radio" name="computer-target" value={app.id} checked={picked === app.id} disabled={app.reachable === false} onChange={() => setPicked(app.id)} />
                <span className="computer-row-main"><b className="truncate">{app.name}</b>
                  <span className="sub truncate">{app.reachable === false ? t("cu.app.elevated") : t("cu.app.windows", { n: app.windows.length })}</span></span>
              </label>
            ))
            : list.flatMap((app) => app.windows.map((w) => (
              <label key={`${app.id}/${w.id}`} className={`computer-row ${app.reachable === false ? "off" : ""}`}>
                <input type="radio" name="computer-target" value={w.id} checked={picked === w.id} disabled={app.reachable === false} onChange={() => setPicked(w.id)} />
                <span className="computer-row-main"><b className="truncate">{w.title || t("cu.window.untitled")}</b>
                  <span className="sub truncate">{[app.name, w.minimized ? t("cu.window.minimized") : "", app.reachable === false ? t("cu.app.elevated") : ""].filter(Boolean).join(" · ")}</span></span>
              </label>
            )))}
        </div>
      )}
      <div className="computer-actions">
        <button type="button" className="btn primary" disabled={busy || !ready} onClick={grant}>{t("cu.grant", { n: valid ? minutes : "…" })}</button>
      </div>
      <div className="sub">{t("cu.grant.note")}</div>
    </div>
  );
}

function Permissions({ state, busy, onAsk }: { state: ComputerState; busy: boolean; onAsk: (which: "screen" | "accessibility") => void }) {
  const os = state.os ?? "";
  if (os === "darwin") {
    const rows: { which: "screen" | "accessibility"; label: string }[] = [
      { which: "screen", label: t("cu.perm.screen") },
      { which: "accessibility", label: t("cu.perm.accessibility") },
    ];
    return (
      <div className="computer-permissions" data-os="darwin">
        <div className="section-title">{t("cu.perm.title")}</div>
        {rows.map(({ which, label }) => {
          const value = state.permissions?.[which] ?? "unknown";
          const link = state.settings_links?.[which];
          return (
            <div key={which} className="computer-row" data-permission={which} data-state={value}>
              <span className={`dot ${value === "granted" ? "ok" : value === "denied" ? "error" : "waiting"}`} aria-hidden="true" />
              <span className="computer-row-main"><b>{label}</b><span className="sub">{t(`cu.perm.${value === "granted" || value === "denied" ? value : "unknown"}`)}</span></span>
              {value !== "granted" && <button type="button" className="btn small" disabled={busy} onClick={() => onAsk(which)}>{t("cu.perm.ask")}</button>}
              {link && <a className="btn small" href={link} target="_blank" rel="noreferrer">{t("cu.perm.open")}</a>}
            </div>
          );
        })}
        <div className="sub attn computer-adhoc">{t("cu.perm.adhoc")}</div>
      </div>
    );
  }
  if (os === "windows") {
    return (
      <div className="computer-permissions" data-os="windows">
        <div className="section-title">{t("cu.perm.title")}</div>
        <div className="sub">{t("cu.win.limits")}</div>
      </div>
    );
  }
  if (os === "linux") {
    return (
      <div className="computer-permissions" data-os="linux">
        <div className="section-title">{t("cu.perm.title")}</div>
        <div className="sub">{t("cu.linux.session", { type: state.session_type || "?" })}</div>
        <div className="sub">{t("cu.linux.wayland")}</div>
      </div>
    );
  }
  return null;
}
