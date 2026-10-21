// What a grant of the computer covers, chosen by looking at it: one window, one app, one monitor or
// the whole desktop, each card showing a live picture of what it stands for, and how long the grant
// lasts. Used in the conversation's Computer control (computer.tsx) and, compact, inline in Quick Chat.
//
// The pictures come from the host's preview route, which asks the launcher, which asks the desktop
// daemon; reading one needs no grant, and a protected window answers with a refusal rather than a
// picture, so its card shows a lock. A picture is fetched only while its card is on screen and the
// page is visible, and read again every few seconds then: a dozen cards polled from a hidden tab
// would keep the daemon capturing windows nobody is looking at.

import { useEffect, useRef, useState, type CSSProperties, type RefObject } from "react";
import { api, ApiError } from "./api";
import { t } from "./i18n";
import { Icon, type IconName } from "./icons";
import { useQuery } from "./store";
import {
  COMPUTER,
  COMPUTER_APPS,
  LARGE_EDGE,
  MAX_MINUTES,
  PRESETS,
  PREVIEW_REFRESH_MS,
  THUMB_EDGE,
  appFace,
  buildScope,
  chosenPreview,
  displayName,
  displayResolution,
  displayScale,
  previewUrl,
  sortedDisplays,
  stillThere,
  validMinutes,
  windowCards,
  type ComputerApp,
  type ComputerDisplay,
  type ComputerScope,
  type PreviewTarget,
  type ScopeKind,
} from "./computerscope";
import "./computer.css";

/** The apps are read again this often while the picker is open, so a window opened meanwhile appears. */
const APPS_POLL_MS = 6000;

const TABS: { kind: ScopeKind; icon: IconName }[] = [
  { kind: "window", icon: "panel" },
  { kind: "app", icon: "apps" },
  { kind: "display", icon: "monitor" },
  { kind: "desktop", icon: "grid" },
];

type PreviewState = "loading" | "ready" | "error" | "protected";

/** Whether an element is on screen; true at once where the browser cannot tell, so a picture is still shown. */
function useOnScreen(ref: RefObject<HTMLElement | null>): boolean {
  const [seen, setSeen] = useState(typeof IntersectionObserver === "undefined");
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver((entries) => setSeen(entries.some((e) => e.isIntersecting)), { rootMargin: "64px" });
    observer.observe(el);
    return () => observer.disconnect();
  }, [ref]);
  return seen;
}

/** A target's picture as a blob URL (an <img> cannot send the auth header), refreshed while on screen.
 *  A refresh that fails keeps the last good picture: a window busy for a moment is not a broken one. */
function usePreview(target: PreviewTarget | null, edge: number, ref: RefObject<HTMLElement | null>): { url: string | null; state: PreviewState } {
  const onScreen = useOnScreen(ref);
  const [url, setUrl] = useState<string | null>(null);
  const [state, setState] = useState<PreviewState>("loading");
  const key = target ? previewUrl(target, edge) : "";
  useEffect(() => {
    setUrl(null);
    setState("loading");
  }, [key]);
  useEffect(() => {
    if (!key || !onScreen) return;
    let alive = true;
    let made: string | null = null;
    const read = async () => {
      if (typeof document !== "undefined" && document.visibilityState === "hidden") return;
      try {
        const res = await fetch(key, { headers: api.authHeaders(), cache: "no-store" });
        if (!res.ok) throw new ApiError(res.status, `preview ${res.status}`);
        if (!(res.headers.get("Content-Type") || "").startsWith("image/")) throw new Error("not a picture");
        const next = URL.createObjectURL(await res.blob());
        if (!alive) return URL.revokeObjectURL(next);
        if (made) URL.revokeObjectURL(made);
        made = next;
        setUrl(next);
        setState("ready");
      } catch (e) {
        if (!alive || made) return;
        setState(e instanceof ApiError && e.status === 409 ? "protected" : "error");
      }
    };
    void read();
    const timer = window.setInterval(() => void read(), PREVIEW_REFRESH_MS);
    return () => {
      alive = false;
      window.clearInterval(timer);
      if (made) URL.revokeObjectURL(made);
    };
  }, [key, onScreen]);
  return { url, state };
}

/** A picture with its shimmer while it loads and a quiet glyph where none can be had. */
function Thumb({ target, edge = THUMB_EDGE, icon, className = "", style }: { target: PreviewTarget | null; edge?: number; icon: IconName; className?: string; style?: CSSProperties }) {
  const ref = useRef<HTMLSpanElement>(null);
  const { url, state } = usePreview(target, edge, ref);
  const [shown, setShown] = useState(false);
  const key = target ? previewUrl(target, edge) : "";
  useEffect(() => setShown(false), [key]);
  const failed = !url && (state === "error" || state === "protected" || !target);
  return (
    <span ref={ref} className={`cu-thumb ${className} ${shown ? "ready" : ""}`} style={style} data-preview={url ? "ready" : target ? state : "none"}>
      {url && className.includes("large") && <img className="cu-thumb-backdrop" src={url} alt="" draggable={false} aria-hidden />}
      {url && <img src={url} alt="" draggable={false} onLoad={() => setShown(true)} />}
      {!url && !failed && <span className="cu-shimmer" aria-hidden />}
      {failed && (
        <span className="cu-thumb-glyph" title={state === "protected" ? t("cu.preview.protected") : undefined}>
          <Icon name={state === "protected" ? "lock" : icon} size={20} />
        </span>
      )}
    </span>
  );
}

/** The monitors as they stand on the desk, each with its picture, scaled into one strip. In the
 *  Monitor tab a monitor is chosen by pressing it; for the whole desktop every one is lit. */
function DisplayMap({ displays, picked, all, compact, onPick }: { displays: ComputerDisplay[]; picked: string; all: boolean; compact?: boolean; onPick?: (id: string) => void }) {
  if (!displays.length) return <div className="cu-empty"><Icon name="monitor" size={20} /><span>{t("cu.displays.none")}</span></div>;
  const left = Math.min(...displays.map((d) => d.bounds.x));
  const top = Math.min(...displays.map((d) => d.bounds.y));
  const width = Math.max(...displays.map((d) => d.bounds.x + d.bounds.w)) - left || 1;
  const height = Math.max(...displays.map((d) => d.bounds.y + d.bounds.h)) - top || 1;
  const tall = compact ? 112 : 196;
  const ordered = sortedDisplays(displays);
  return (
    <div className="cu-map" style={{ aspectRatio: `${width} / ${height}`, width: `min(100%, ${Math.round((tall * width) / height)}px)` }}
      role={onPick ? "radiogroup" : undefined} aria-label={onPick ? t("cu.pick.display") : undefined}>
      {ordered.map((d) => {
        const index = ordered.indexOf(d);
        const on = all || d.id === picked;
        const style: CSSProperties = {
          left: `${((d.bounds.x - left) / width) * 100}%`,
          top: `${((d.bounds.y - top) / height) * 100}%`,
          width: `${(d.bounds.w / width) * 100}%`,
          height: `${(d.bounds.h / height) * 100}%`,
        };
        const body = (
          <>
            <Thumb target={{ display: d.id }} edge={compact ? THUMB_EDGE : 480} icon="monitor" />
            <span className="cu-map-label">
              <b className="truncate">{displayName(d, index)}</b>
              {d.primary && <span className="cu-badge">{t("cu.display.primary")}</span>}
            </span>
            {on && <span className="cu-check" aria-hidden><Icon name="check" size={12} /></span>}
          </>
        );
        return onPick ? (
          <button key={d.id} type="button" role="radio" aria-checked={on} className={`cu-map-tile ${on ? "on" : ""}`} style={style}
            data-display={d.id} onClick={() => onPick(d.id)} title={`${displayName(d, index)} · ${displayResolution(d)}`}>
            {body}
          </button>
        ) : (
          <span key={d.id} className={`cu-map-tile ${on ? "on" : ""}`} style={style} data-display={d.id}>{body}</span>
        );
      })}
    </div>
  );
}

/** One choosable card: its picture, its name and a line under it; a card that cannot be chosen says why. */
function Card({ on, blocked, target, icon, title, sub, badge, index, onPick, value }: {
  on: boolean; blocked: string; target: PreviewTarget | null; icon: IconName; title: string; sub: string; badge?: string; index: number; value: string; onPick: () => void;
}) {
  return (
    <button type="button" role="radio" aria-checked={on} disabled={!!blocked} className={`cu-card ${on ? "on" : ""} ${blocked ? "off" : ""}`}
      style={{ "--n": Math.min(index, 8) } as CSSProperties} data-value={value} onClick={onPick} title={blocked || title}>
      <Thumb target={blocked ? null : target} icon={blocked ? "lock" : icon} />
      <span className="cu-card-text">
        <b className="truncate">{title}</b>
        <span className="cu-card-sub truncate">{blocked || sub}</span>
      </span>
      {badge && <span className="cu-badge cu-card-badge">{badge}</span>}
      {on && <span className="cu-check" aria-hidden><Icon name="check" size={12} /></span>}
    </button>
  );
}

function presetLabel(n: number): string {
  return n >= 60 && n % 60 === 0 ? t("cu.preset.h", { n: n / 60 }) : t("cu.preset", { n });
}

export type ScopeChoice = { scope: ComputerScope | null; minutes: number; ready: boolean };

/** The scope picker. With `onGrant` it ends in the "Give control" button; compact (Quick Chat) it
 *  draws no large picture and no button, and reports the choice through `onChange` instead, since
 *  there the grant goes with the message. */
export function ComputerScopePicker({ busy, onGrant, onChange, compact }: {
  busy: boolean;
  onGrant?: (scope: ComputerScope, minutes: number) => void;
  onChange?: (choice: ScopeChoice) => void;
  compact?: boolean;
}) {
  const [kind, setKind] = useState<ScopeKind>("window");
  // A pick per tab, so looking at the monitors does not forget the window chosen a moment ago.
  const [picks, setPicks] = useState<Record<ScopeKind, string>>({ window: "", app: "", display: "", desktop: "" });
  const [minutes, setMinutes] = useState<number>(30);
  const [typed, setTyped] = useState("30");
  const [custom, setCustom] = useState(false);
  const state = useQuery<{ displays?: ComputerDisplay[] }>(COMPUTER, { staleMs: 2000 });
  const needsApps = kind === "window" || kind === "app";
  const apps = useQuery<{ apps: ComputerApp[] }>(needsApps ? COMPUTER_APPS : null, { pollMs: APPS_POLL_MS });
  const list = apps.data?.apps ?? [];
  const displays = state.data?.displays ?? [];
  const picked = picks[kind];

  // One monitor is the only monitor: it is chosen already.
  useEffect(() => {
    if (displays.length === 1 && !picks.display) setPicks((now) => ({ ...now, display: displays[0].id }));
  }, [displays, picks.display]);
  // A window closed or a monitor unplugged since it was chosen is let go.
  useEffect(() => {
    if (!picked) return;
    if ((needsApps && !apps.data) || (kind === "display" && !state.data)) return;
    if (!stillThere(kind, picked, list, displays)) setPicks((now) => ({ ...now, [kind]: "" }));
  }, [kind, picked, list, displays, needsApps, apps.data, state.data]);

  const valid = validMinutes(minutes);
  const scope = buildScope(kind, picked);
  const ready = valid && !!scope;
  const report = useRef(onChange);
  report.current = onChange;
  useEffect(() => {
    report.current?.({ scope, minutes, ready });
  }, [kind, picked, minutes, ready]);

  function pick(id: string) {
    setPicks((now) => ({ ...now, [kind]: id }));
  }
  function choose(value: number) {
    setCustom(false);
    setMinutes(value);
    setTyped(String(value));
  }
  function grant() {
    if (ready && scope && onGrant) onGrant(scope, minutes);
  }

  const cards = kind === "window" ? windowCards(list) : [];
  const tabIndex = TABS.findIndex((tab) => tab.kind === kind);
  const isPreset = !custom && (PRESETS as readonly number[]).includes(minutes);

  return (
    <div className={`cu-picker ${compact ? "compact" : ""}`} data-kind={kind}>
      <div className="cu-tabs" role="tablist" aria-label={t("cu.scope")} style={{ "--i": tabIndex } as CSSProperties}>
        <span className="cu-tabs-thumb" aria-hidden />
        {TABS.map((tab) => (
          <button key={tab.kind} type="button" role="tab" aria-selected={tab.kind === kind} className={tab.kind === kind ? "on" : ""}
            data-tab={tab.kind} onClick={() => setKind(tab.kind)}>
            <Icon name={tab.icon} size={14} />
            <span className="truncate">{t(`cu.tab.${tab.kind}`)}</span>
          </button>
        ))}
      </div>

      <div className="cu-pane" key={kind}>
        {(kind === "display" || kind === "desktop") ? (
          <div className="cu-stage map">
            <DisplayMap displays={displays} picked={picked} all={kind === "desktop"} compact={compact} onPick={kind === "display" ? pick : undefined} />
            {kind === "display" && <div className="cu-stage-caption">{picked ? displayCaption(displays, picked) : t("cu.pick.display.hint")}</div>}
            {kind === "desktop" && <div className="cu-warn"><Icon name="alert" size={14} /><span>{t("cu.scope.desktop.warn")}</span></div>}
          </div>
        ) : (
          <>
            {!compact && <LargePreview kind={kind} picked={picked} apps={list} />}
            <div className="cu-cards" role="radiogroup" aria-label={t(kind === "window" ? "cu.pick.window" : "cu.pick.app")}>
              {apps.loading && !list.length && [0, 1, 2].map((n) => <span key={n} className="cu-card ghost" aria-hidden><span className="cu-thumb"><span className="cu-shimmer" /></span><span className="cu-card-text"><span className="cu-line" /><span className="cu-line short" /></span></span>)}
              {apps.error && !list.length && <div className="cu-empty attn">{apps.error}</div>}
              {!apps.loading && !apps.error && !list.length && <div className="cu-empty"><Icon name="panel" size={20} /><span>{t("cu.apps.none")}</span></div>}
              {kind === "window"
                ? cards.map((card, index) => (
                  <Card key={card.id} index={index} value={card.id} on={picked === card.id} blocked={card.blocked} target={{ window: card.id }} icon="panel"
                    title={card.title} sub={[card.app, card.minimized ? t("cu.window.minimized") : ""].filter(Boolean).join(" · ")} onPick={() => pick(card.id)} />
                ))
                : list.map((app, index) => {
                  const face = appFace(app);
                  return (
                    <Card key={app.id} index={index} value={app.id} on={picked === app.id} blocked={app.reachable === false ? t("cu.app.elevated") : ""}
                      target={face ? { window: face.id } : null} icon="apps" title={app.name} sub={t("cu.app.windows", { n: app.windows.length })} onPick={() => pick(app.id)} />
                  );
                })}
            </div>
          </>
        )}
      </div>

      <div className="cu-duration">
        <span className="cu-label"><Icon name="clock" size={14} />{t("cu.minutes")}</span>
        <div className="cu-pills" role="radiogroup" aria-label={t("cu.minutes")}>
          {PRESETS.map((n) => (
            <button key={n} type="button" role="radio" aria-checked={isPreset && minutes === n} className={`cu-pill ${isPreset && minutes === n ? "on" : ""}`} onClick={() => choose(n)}>
              {presetLabel(n)}
            </button>
          ))}
          <button type="button" role="radio" aria-checked={!isPreset} className={`cu-pill ${!isPreset ? "on" : ""}`} onClick={() => setCustom(true)}>{t("cu.minutes.other")}</button>
          {!isPreset && (
            <span className={`cu-custom ${valid ? "" : "invalid"}`}>
              <input className="computer-minutes" type="number" inputMode="numeric" min={1} max={MAX_MINUTES} step={1} autoFocus={custom}
                value={typed} aria-label={t("cu.minutes.custom")} aria-invalid={!valid}
                onChange={(e) => { setTyped(e.target.value); setMinutes(Number(e.target.value)); }} />
              <span>{t("cu.minutes.unit")}</span>
            </span>
          )}
        </div>
      </div>
      {!valid && <div className="cu-bound attn">{t("cu.minutes.bound", { n: MAX_MINUTES })}</div>}

      {onGrant && (
        <div className="cu-actions">
          <span className="cu-summary truncate">{scope ? (valid ? summary(kind, picked, list, displays, minutes) : "") : t(`cu.need.${kind}`)}</span>
          <button type="button" className="btn primary cu-cta" disabled={busy || !ready} onClick={grant}>
            <Icon name="monitor" size={16} />{t("cu.grant", { n: valid ? minutes : "…" })}
          </button>
        </div>
      )}
    </div>
  );
}

function displayCaption(displays: ComputerDisplay[], id: string): string {
  const ordered = sortedDisplays(displays);
  const index = ordered.findIndex((d) => d.id === id);
  if (index < 0) return "";
  const d = ordered[index];
  return [displayName(d, index), d.primary ? t("cu.display.primary") : "", displayResolution(d), displayScale(d)].filter(Boolean).join(" · ");
}

/** "notes.txt · Editor — 30 min": what the button is about to give, in one line beside it. */
function summary(kind: ScopeKind, picked: string, apps: ComputerApp[], displays: ComputerDisplay[], minutes: number): string {
  let what = t("cu.scope.desktop");
  if (kind === "window") {
    const card = windowCards(apps).find((c) => c.id === picked);
    what = card ? card.title : "";
  } else if (kind === "app") {
    what = apps.find((a) => a.id === picked)?.name ?? "";
  } else if (kind === "display") {
    const ordered = sortedDisplays(displays);
    const index = ordered.findIndex((d) => d.id === picked);
    what = index >= 0 ? displayName(ordered[index], index) : "";
  }
  return t("cu.summary", { what, n: minutes });
}

/** The chosen window or app large, with its name; before a choice, a word on what to choose. */
function LargePreview({ kind, picked, apps }: { kind: ScopeKind; picked: string; apps: ComputerApp[] }) {
  const target = chosenPreview(kind, picked, apps);
  const card = kind === "window" ? windowCards(apps).find((c) => c.id === picked) : null;
  const app = kind === "app" ? apps.find((a) => a.id === picked) : null;
  if (!target) {
    return (
      <div className="cu-stage empty">
        <span className="cu-stage-glyph"><Icon name={kind === "app" ? "apps" : "panel"} size={22} /></span>
        <span>{t(`cu.need.${kind}`)}</span>
      </div>
    );
  }
  return (
    <div className="cu-stage">
      <Thumb key={previewUrl(target, LARGE_EDGE)} target={target} edge={LARGE_EDGE} icon={kind === "app" ? "apps" : "panel"} className="large" />
      <div className="cu-stage-caption">
        <b className="truncate">{card?.title ?? app?.name ?? ""}</b>
        <span className="truncate">{card ? card.app : app ? t("cu.app.windows", { n: app.windows.length }) : ""}</span>
      </div>
    </div>
  );
}
