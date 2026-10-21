// What kind of conversation a chat is: an ordinary chat, one about the operator's own computer
// (`computer`, the Computer control), or one carried out in the operator's own Chrome (`chrome`). The
// host keeps it on the session (`sessions.kind`); this file holds what the app does with it — the
// tabs over the chat lists, the mark a computer or Chrome chat carries where kinds are listed mixed,
// and the mode a new chat starts in.
//
// The chosen tab is remembered per viewer, in this browser only: it is a way of looking at the list,
// not a fact about the chats, and a private window that cannot store it simply starts on All. The same
// tab is the new chat's default mode, so an operator looking at their computer chats who presses New
// chat starts another one of those.

import { useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from "react";
import type { BrowserUserStatus, SessionSummary } from "./api";
import { COMPUTER, type ComputerState, wordsText } from "./computer";
import { USER_STATUS, chromeState } from "./browser/yourchrome";
import { t } from "./i18n";
import { Icon, type IconName } from "./icons";
import { useQuery } from "./store";
import { Popover, Sheet } from "./ui/dialogs";

export type ChatKind = "chat" | "computer" | "chrome";
export type KindTab = "all" | ChatKind;

export const KIND_TABS: readonly KindTab[] = ["all", "chat", "computer", "chrome"];
const KINDS: readonly ChatKind[] = ["chat", "computer", "chrome"];

/** A session's kind; a host from before the column, or a row without it, is an ordinary chat. */
export function sessionKind(s: Pick<SessionSummary, "kind">): ChatKind {
  return s.kind && KINDS.includes(s.kind) ? s.kind : "chat";
}

/** The icon a kind is drawn with; an ordinary chat has none of its own, its plate says the rest. */
export function kindIcon(kind: ChatKind): IconName | null {
  return kind === "computer" ? "monitor" : kind === "chrome" ? "globe" : null;
}

/**
 * The sessions a tab shows. A subagent or a fork is judged by the chat it hangs under, so a computer
 * chat keeps its helpers and its forks under it: judged by their own kind, a helper (always an
 * ordinary chat) would have been dropped and its leader shown without the fold that reveals it, or,
 * with the leader filtered out, listed alone as an orphan.
 */
export function byTab<T extends SessionSummary>(sessions: T[], tab: KindTab): T[] {
  if (tab === "all") return sessions;
  const ids = new Map(sessions.map((s) => [s.id, s]));
  const root = (s: T): T => {
    let at = s;
    const seen = new Set<string>();
    for (;;) {
      seen.add(at.id);
      const up = at.metadata?.subagent_of || at.metadata?.forked_from?.session_id;
      const next = up ? ids.get(up) : undefined;
      if (!next || seen.has(next.id)) return at;
      at = next;
    }
  };
  return sessions.filter((s) => sessionKind(root(s)) === tab);
}

/** How many top-level chats each tab would show, for the tabs' counts. */
export function tabCounts(sessions: SessionSummary[]): Record<KindTab, number> {
  const top = sessions.filter((s) => !s.metadata?.subagent_of && !s.archived);
  const counts: Record<KindTab, number> = { all: top.length, chat: 0, computer: 0, chrome: 0 };
  for (const s of top) counts[sessionKind(s)] += 1;
  return counts;
}

// The remembered tab: one value for every list in this browser, so the desktop column, the phone's
// Chats page and the new chat's default mode agree without passing it about.
const TAB_KEY = "daedalus.chats.kind";
const listeners = new Set<() => void>();

/** A stored value read back, or All for anything this build does not know. */
export function parseTab(raw: string | null): KindTab {
  return raw && (KIND_TABS as readonly string[]).includes(raw) ? (raw as KindTab) : "all";
}

function readTab(): KindTab {
  try {
    return parseTab(localStorage.getItem(TAB_KEY));
  } catch {
    return "all";
  }
}

let currentTab: KindTab = readTab();

export function kindTab(): KindTab {
  return currentTab;
}

export function setKindTab(tab: KindTab): void {
  if (tab === currentTab) return;
  currentTab = tab;
  try {
    localStorage.setItem(TAB_KEY, tab);
  } catch {
    /* private mode: the tab lasts for the visit */
  }
  for (const listener of [...listeners]) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function useKindTab(): [KindTab, (tab: KindTab) => void] {
  const tab = useSyncExternalStore(subscribe, kindTab, kindTab);
  return [tab, setKindTab];
}

/** Whether a kind of new chat can be started here, and why not when it cannot. */
export type KindOffer = { ok: boolean; reason: string };
export type KindOffers = Record<ChatKind, KindOffer> & { chromeConnected: boolean };

/** The mode a new chat starts in: the tab in view when that kind can be started, else a plain chat. */
export function defaultKind(tab: KindTab, offers: Pick<KindOffers, "computer" | "chrome">): ChatKind {
  if (tab === "computer" && offers.computer.ok) return "computer";
  if (tab === "chrome" && offers.chrome.ok) return "chrome";
  return "chat";
}

/** What this installation offers: the Computer control only in the native application, and the
 *  operator's Chrome only where a daemon for it is configured and running. */
export function kindOffers(computer: ComputerState | undefined, chrome: BrowserUserStatus | undefined): KindOffers {
  const state = chromeState(chrome);
  return {
    chat: { ok: true, reason: "" },
    computer: computer?.native
      ? { ok: true, reason: "" }
      : { ok: false, reason: (computer && (wordsText(computer.reason_words) || computer.reason)) || t("kind.computer.off") },
    chrome: state === "none"
      ? { ok: false, reason: t("kind.chrome.off") }
      : state === "down" ? { ok: false, reason: t("kind.chrome.down") } : { ok: true, reason: "" },
    chromeConnected: state === "connected",
  };
}

export function useKindOffers(): KindOffers {
  const computer = useQuery<ComputerState>(COMPUTER, { staleMs: 60000 });
  const chrome = useQuery<BrowserUserStatus>(USER_STATUS, { staleMs: 30000 });
  return kindOffers(computer.data, chrome.data);
}

// A computer chat's sheet opens by itself when the chat is reached from New chat in Computer mode.
// The request is held here rather than in the address: a reload, or Back to the chat later, must not
// open the sheet again, and an address flag would do both.
let sheetFor: string | null = null;

export function requestComputerSheet(sessionId: string): void {
  sheetFor = sessionId;
}

/** Whether the sheet was asked for this chat; asking is answered once. */
export function takeComputerSheet(sessionId: string): boolean {
  if (sheetFor !== sessionId) return false;
  sheetFor = null;
  return true;
}

export function tabLabel(tab: KindTab): string {
  return t(`kind.tab.${tab}`);
}

/**
 * The tabs over a chat list: All, Chats, Computer, Browser. Drawn as a tab bar with an underline, not
 * as pills, so they do not read as one more row of the status chips under them. The underline slides
 * to the next tab rather than jumping, measured from the buttons themselves, because the labels are as wide
 * as their words and Russian ones are half as wide again. Arrows move between them, as a tab list's do.
 */
export function KindTabs({ tab, onChange, counts, className = "" }: { tab: KindTab; onChange: (tab: KindTab) => void; counts?: Record<KindTab, number>; className?: string }) {
  const box = useRef<HTMLDivElement>(null);
  const [pill, setPill] = useState<{ left: number; width: number } | null>(null);
  useLayoutEffect(() => {
    const place = () => {
      const on = box.current?.querySelector<HTMLElement>(`[data-kind-tab="${tab}"]`);
      if (on) setPill({ left: on.offsetLeft, width: on.offsetWidth });
    };
    place();
    if (typeof ResizeObserver === "undefined" || !box.current) return;
    const observer = new ResizeObserver(place);
    observer.observe(box.current);
    return () => observer.disconnect();
  }, [tab, counts?.all]);
  const onKey = (e: React.KeyboardEvent) => {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    e.preventDefault();
    const at = KIND_TABS.indexOf(tab);
    const next = KIND_TABS[(at + (e.key === "ArrowRight" ? 1 : KIND_TABS.length - 1)) % KIND_TABS.length];
    onChange(next);
    box.current?.querySelector<HTMLElement>(`[data-kind-tab="${next}"]`)?.focus();
  };
  return (
    <div ref={box} className={`kind-tabs ${className}`} role="tablist" aria-label={t("kind.tabs.label")} onKeyDown={onKey}>
      {pill && <span className="kind-tabs-pill" data-on={tab} aria-hidden style={{ transform: `translateX(${pill.left}px)`, width: pill.width }} />}
      {KIND_TABS.map((k) => {
        const icon = k === "computer" || k === "chrome" ? kindIcon(k) : null;
        const n = counts?.[k];
        return (
          <button key={k} type="button" role="tab" data-kind-tab={k} aria-selected={tab === k} tabIndex={tab === k ? 0 : -1}
            className={`kind-tab ${tab === k ? "on" : ""}`} onClick={() => onChange(k)} title={t(`kind.tab.${k}.title`)}>
            {icon && <Icon name={icon} size={13} />}
            <span>{tabLabel(k)}</span>
            {n !== undefined && n > 0 && k !== "all" && k !== "chat" && <b className="num">{n}</b>}
          </button>
        );
      })}
    </div>
  );
}

/** The small mark a computer or Chrome chat carries where kinds are listed together; nothing for a
 *  plain chat, which is what a list is mostly made of and needs no mark to say so. */
export function KindMark({ kind, size = 12 }: { kind: ChatKind; size?: number }) {
  const icon = kindIcon(kind);
  if (!icon) return null;
  const label = t(`kind.mark.${kind}`);
  return <span className={`kind-mark kind-${kind}`} role="img" aria-label={label} title={label}><Icon name={icon} size={size} /></span>;
}

const MODE_ICON: Record<ChatKind, IconName> = { chat: "bots", computer: "monitor", chrome: "globe" };

/**
 * The new chat's mode: an ordinary chat, a chat about this computer, or one in the operator's Chrome.
 * A kind this installation cannot start stays in the list, greyed, with the reason under its name, so
 * the operator learns where it would be offered rather than wondering why it is missing.
 */
export function KindSelect({ kind, offers, onChoose, sheet, className = "" }: { kind: ChatKind; offers: KindOffers; onChoose: (kind: ChatKind) => void; sheet: boolean; className?: string }) {
  const trigger = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  const pick = (k: ChatKind) => {
    setOpen(false);
    if (k !== kind) onChoose(k);
  };
  const hint = (k: ChatKind) => {
    if (!offers[k].ok) return offers[k].reason;
    if (k === "chrome" && !offers.chromeConnected) return t("kind.chrome.waiting");
    return t(`kind.mode.${k}.hint`);
  };
  const list = (
    <div className="mode-list kind-list" role="menu" aria-label={t("kind.mode.label")}>
      {!sheet && <div className="menu-heading sub">{t("kind.mode.label")}</div>}
      {KINDS.map((k) => (
        <button key={k} type="button" role="menuitemradio" aria-checked={k === kind} disabled={!offers[k].ok} data-kind={k}
          className={`model-row mode-row kind-row ${k === kind ? "on" : ""}`} onClick={() => pick(k)} title={offers[k].ok ? undefined : offers[k].reason}>
          <span className={`kind-row-icon kind-${k}`}><Icon name={MODE_ICON[k]} size={16} /></span>
          <span className="grow model-text">
            <span className="truncate">{t(`kind.mode.${k}`)}</span>
            <span className="sub clamp-2">{hint(k)}</span>
          </span>
          {k === kind && <Icon name="check" size={16} />}
        </button>
      ))}
    </div>
  );
  const label = t(`kind.mode.${kind}`);
  return (
    <>
      <button ref={trigger} type="button" className={`composer-mode kind-select kind-${kind} ${open ? "on" : ""} ${className}`} data-kind={kind}
        onClick={() => setOpen(!open)} title={`${t("kind.mode.label")}: ${label}`} aria-label={`${t("kind.mode.label")}: ${label}`} aria-haspopup="menu" aria-expanded={open}>
        <Icon name={MODE_ICON[kind]} size={14} />
        <span className="truncate">{label}</span>
        <Icon name="chevron" size={12} />
      </button>
      {open && sheet && (
        <Sheet title={t("kind.mode.label")} onClose={() => setOpen(false)} className="mode-sheet" size="narrow">
          {list}
        </Sheet>
      )}
      {open && !sheet && (
        <Popover anchor={trigger.current} onClose={() => setOpen(false)} className="mode-menu kind-menu" label={t("kind.mode.label")}>
          {list}
        </Popover>
      )}
    </>
  );
}

/** The new chat's mode, following the tab in view until the operator picks one here, and falling back
 *  to a plain chat when the kind picked stops being offered (the Chrome daemon went down meanwhile). */
export function useNewChatKind(offers: KindOffers): [ChatKind, (kind: ChatKind) => void] {
  const [tab] = useKindTab();
  const [picked, setPicked] = useState<ChatKind | null>(null);
  useEffect(() => setPicked(null), [tab]);
  const wanted = picked ?? defaultKind(tab, offers);
  return [offers[wanted].ok ? wanted : "chat", setPicked];
}
