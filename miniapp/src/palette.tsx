// The command palette, Ctrl K: one field over everything the app can go to or do, in groups. Chats
// (by name, and by meaning through the same search the sidebar's field asks the host), projects,
// actions, screens and pages, and the settings (a section, or one row of one, from the settings index).
// Names only for what the shell already holds; the host is asked only for the chats, after a pause in
// the typing, so a key press never waits on the network.
//
// Enter opens the highlighted row; Ctrl Enter (or ⌘) opens a chat in a second pane beside the one on
// screen, which is `?with=` in the address; Tab walks from one group to the next.

import { useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyEvent, type ReactNode } from "react";
import "./palette.css";
import { api, type SessionSummary } from "./api";
import { Icon } from "./icons";
import { lang, plural, t } from "./i18n";
import { agentsListingOf } from "./mode";
import { navigate, pathFor } from "./router";
import { openPathOf } from "./subagents";
import { type PaletteGroup, type PaletteItem, besideSession, chatStatus, matchPalette } from "./shell";
import { KEYS } from "./sidebar";
import { Combobox, Sheet, type ComboboxGroup } from "./ui/primitives";

const ORDER: PaletteGroup[] = ["chats", "projects", "actions", "go", "settings"];
/** How many rows a group shows once something is typed, and before: the empty palette is a short list
 *  of where to go next, not the whole app. */
const TYPED: Record<PaletteGroup, number> = { chats: 6, projects: 4, actions: 5, go: 5, settings: 6 };
const IDLE: Record<PaletteGroup, number> = { chats: 5, projects: 0, actions: 6, go: 6, settings: 0 };
/** The pause after the last key before the host is asked for chats, as the sidebar's field waits. */
const PAUSE_MS = 250;

type SettingsIndex = typeof import("./screens/settingsindex");
type Found = { sessions: SessionSummary[]; semantic: boolean; indexing: boolean; partial: boolean };
/** `done` says which words it answered, so a list drawn for the keys before these is not mistaken for theirs. */
type Chats = { state: "idle" } | { state: "loading" } | { state: "failed" } | { state: "done"; word: string; found: Found };

/** The chats the host finds by words and by meaning for `query`, once typing has paused. Only the
 *  newest question's answer is kept; an older one still on its way is dropped with its request. */
function useChatSearch(query: string): Chats {
  const [chats, setChats] = useState<Chats>({ state: "idle" });
  const word = query.trim();
  useEffect(() => {
    if (!word) {
      setChats({ state: "idle" });
      return;
    }
    setChats((was) => (was.state === "done" ? was : { state: "loading" }));
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      void fetch(`/api/sessions/search?q=${encodeURIComponent(word)}&project=`, { headers: api.authHeaders(), signal: controller.signal })
        .then(async (response) => {
          if (!response.ok) throw new Error(String(response.status));
          const answer = (await response.json()) as { sessions: SessionSummary[]; semantic?: boolean; indexing?: boolean; partial?: boolean };
          if (controller.signal.aborted) return;
          const listing = agentsListingOf({ sessions: answer.sessions ?? [], projects: [] });
          setChats({ state: "done", word, found: { sessions: (listing?.sessions ?? []).filter((s) => !s.metadata?.subagent_of), semantic: !!answer.semantic, indexing: !!answer.indexing, partial: !!answer.partial } });
        })
        .catch(() => { if (!controller.signal.aborted) setChats({ state: "failed" }); });
    }, PAUSE_MS);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [word]);
  return chats;
}

/** The settings index is a large table only Settings and this need, so it is not part of the shell.
 *  It is fetched when the palette opens, once the first frame is drawn, not on the first key: parsing
 *  it there was the one long task a key press could cost. */
function useSettingsIndex(): SettingsIndex | null {
  const [index, setIndex] = useState<SettingsIndex | null>(null);
  useEffect(() => {
    let live = true;
    const load = () => void import("./screens/settingsindex").then((module) => { if (live) setIndex(module); }).catch(() => {});
    const idle = window.requestIdleCallback?.(load, { timeout: 500 });
    const timer = idle === undefined ? window.setTimeout(load, 120) : undefined;
    return () => {
      live = false;
      if (idle !== undefined) window.cancelIdleCallback?.(idle);
      window.clearTimeout(timer);
    };
  }, []);
  return index;
}

/** The settings rows that answer `query`: sections whose names hold it, then the rows of any section. */
function settingsHits(index: SettingsIndex, query: string): PaletteItem[] {
  const words = query.trim().toLowerCase();
  const here = lang();
  const sections = index.INDEX_SECTIONS.filter((section) => [section.title.en, section.title.ru, ...section.synonyms].join("\n").toLowerCase().includes(words)).slice(0, 3);
  const titleOf = (id: string) => index.INDEX_SECTIONS.find((section) => section.id === id)?.title[here] ?? id;
  const fields = index.matchFields(query).slice(0, TYPED.settings - sections.length);
  return [
    ...sections.map((section): PaletteItem => ({ id: `set-${section.id}`, group: "settings", label: section.title[here], hint: section.about[here], icon: section.icon, right: t("palette.right.section"), run: () => navigate(pathFor("settings", section.id)) })),
    ...fields.map((f): PaletteItem => ({ id: `set-${f.section}-${f.anchor}`, group: "settings", label: f.title[here], hint: titleOf(f.section), icon: "settings", right: t("palette.right.setting"), run: () => navigate(index.fieldPath(f)) })),
  ];
}

/** The typed words picked out of a name, in the plain ink and heavier. */
function Marked({ text, needle }: { text: string; needle: string }) {
  if (!needle) return <>{text}</>;
  const low = text.toLowerCase();
  const parts: ReactNode[] = [];
  let at = 0;
  for (;;) {
    const i = low.indexOf(needle, at);
    if (i < 0) break;
    if (i > at) parts.push(text.slice(at, i));
    parts.push(<mark key={i} className="palette-hit">{text.slice(i, i + needle.length)}</mark>);
    at = i + needle.length;
  }
  parts.push(text.slice(at));
  return <>{parts}</>;
}

/** One row's insides: its glyph, its name, a hint (cut from its start where it is a folder, because every
 *  project lives under the same home folder and an end-cut left six rows of an identical start), and the
 *  short word at the far end. The marks on both sides of a path keep its leading slash where it belongs
 *  inside the right-to-left box the start-cut relies on. */
function RowBody({ item, needle }: { item: PaletteItem; needle: string }) {
  const path = !!item.hint && (item.hint.startsWith("/") || item.hint.startsWith("~"));
  return (
    <>
      <Icon name={item.icon} size={16} path={item.iconPath} />
      <span className="palette-title" data-group={item.group}><Marked text={item.label} needle={needle} /></span>
      {item.hint && <span className={`palette-hint ${path ? "tail" : ""}`} title={item.hint}>{path ? `\u200e${item.hint}\u200e` : <Marked text={item.hint} needle={needle} />}</span>}
      {item.right && <span className={`palette-right ${item.tone ?? ""}`}>{item.right}</span>}
    </>
  );
}

/** Ctrl K: go somewhere by name, find a chat by what was said in it, jump to a setting, run an action. */
export function Palette({ items, initialQuery = "", onClose }: { items: () => PaletteItem[]; initialQuery?: string; onClose: () => void }) {
  const [query, setQuery] = useState(initialQuery);
  // What the shell held when the palette opened: it is a few seconds' visit, and a list that changed
  // under the highlighted row would take the choice away from the hand that was about to make it.
  const [held] = useState(items);
  const box = useRef<HTMLDivElement>(null);
  const needle = query.trim().toLowerCase();
  const chats = useChatSearch(query);
  const index = useSettingsIndex();

  const groups = useMemo<ComboboxGroup<PaletteItem>[]>(() => {
    const limit = needle ? TYPED : IDLE;
    const own = new Map<PaletteGroup, PaletteItem[]>(ORDER.map((g) => [g, held.filter((it) => it.group === g)]));
    const named = new Map<PaletteGroup, PaletteItem[]>(ORDER.map((g) => [g, matchPalette(own.get(g) ?? [], query, limit[g])]));
    // Chats the host found by meaning follow the ones whose names already match, never repeating one.
    // Only an answer to the words as they stand: the one before them is on screen no longer.
    const answered = chats.state === "done" && chats.word === query.trim();
    if (chats.state === "done" && answered) {
      const have = new Set((named.get("chats") ?? []).map((it) => it.id));
      const more = chats.found.sessions
        .filter((s) => !have.has(`s-${s.id}`))
        .map((s): PaletteItem => ({
          id: `s-${s.id}`, group: "chats", label: s.title, hint: [s.project, s.match?.snippet].filter(Boolean).join(" · "), icon: "bots", ...chatStatus(s),
          run: () => navigate(openPathOf(s.id)), beside: () => besideSession(s.id, () => navigate(openPathOf(s.id))),
        }));
      named.set("chats", [...(named.get("chats") ?? []), ...more].slice(0, TYPED.chats));
    }
    if (needle && index) named.set("settings", settingsHits(index, query));
    const note = (g: PaletteGroup) => {
      if (g !== "chats" || !needle) return undefined;
      if (chats.state === "loading" || (chats.state === "done" && !answered)) return t("palette.chats.loading");
      if (chats.state === "failed") return t("palette.chats.failed");
      if (chats.state !== "done") return undefined;
      if (chats.found.partial) return t("palette.chats.partial");
      if (chats.found.indexing) return t("palette.chats.indexing");
      return chats.found.semantic ? undefined : t("palette.chats.exact");
    };
    return ORDER.map((g) => ({ label: t(`palette.group.${g}`), items: named.get(g) ?? [], note: note(g) })).filter((g) => g.items.length > 0 || g.note);
  }, [held, query, needle, chats, index]);
  const total = groups.reduce((n, g) => n + g.items.length, 0);

  const choose = (item: PaletteItem, event?: Event) => {
    const keys = event as Partial<Pick<MouseEvent, "ctrlKey" | "metaKey">> | undefined;
    const beside = !!keys && (!!keys.ctrlKey || !!keys.metaKey);
    onClose();
    (beside && item.beside ? item.beside : item.run)();
  };

  // The keys the list does not answer on its own. Ctrl Enter: the list takes Enter and not Enter with a
  // modifier, so the highlighted row is found by its place among the rows. Tab goes to the first row of
  // the next group (Shift Tab to the top of this one, then the previous), by pressing the arrows the
  // list already answers to: the highlight is the list's own.
  const keys = (e: ReactKeyEvent<HTMLDivElement>) => {
    if (e.key !== "Tab" && !(e.key === "Enter" && (e.ctrlKey || e.metaKey))) return;
    const rows = Array.from(box.current?.querySelectorAll<HTMLElement>(".palette-row") ?? []);
    const at = rows.findIndex((row) => row.hasAttribute("data-highlighted"));
    e.preventDefault();
    if (e.key === "Enter") {
      const item = groups.flatMap((g) => g.items)[at];
      if (item) choose(item, e.nativeEvent);
      return;
    }
    if (!rows.length) return;
    const groupOf = (row: HTMLElement) => row.querySelector<HTMLElement>("[data-group]")?.dataset.group;
    const starts = rows.map((_, i) => i).filter((i) => i === 0 || groupOf(rows[i]) !== groupOf(rows[i - 1]));
    const here = at < 0 ? -1 : starts.filter((i) => i <= at).length - 1;
    const target = e.shiftKey
      ? at > (starts[here] ?? 0) ? starts[here] : starts[(here - 1 + starts.length) % starts.length]
      : starts[(here + 1) % starts.length];
    const field = e.target as HTMLElement;
    const press = target > at ? "ArrowDown" : "ArrowUp";
    // One press to a turn: the list reads where it stands from its last render, so presses fired
    // together inside this handler would all start from the row it stands on now.
    const step = (left: number) => {
      if (left <= 0 || !field.isConnected) return;
      field.dispatchEvent(new KeyboardEvent("keydown", { key: press, bubbles: true, cancelable: true }));
      window.setTimeout(() => step(left - 1));
    };
    step(Math.abs(target - at));
  };

  return (
    <Sheet ariaLabel={t("shell.search.label")} onClose={onClose} size="md" className="palette-sheet">
      <div ref={box} className="palette-box" onKeyDown={keys}>
        <Combobox<PaletteItem>
          inline
          autoFocus
          groups={groups}
          items={[]}
          query={query}
          onQuery={setQuery}
          label={t("shell.search.label")}
          placeholder={t("shell.search.placeholder")}
          itemClassName="palette-row"
          lead={<Icon name="search" size={16} />}
          trail={<kbd className="palette-kbd">{KEYS.escape}</kbd>}
          toLabel={(item) => item.id}
          onChoose={choose}
          render={(item) => <RowBody item={item} needle={needle} />}
          empty={t("shell.search.nomatch")}
        />
        <div className="palette-foot">
          <span><kbd className="palette-kbd">↑↓</kbd>{t("palette.key.move")}</span>
          <span><kbd className="palette-kbd">{KEYS.enter}</kbd>{t("palette.key.open")}</span>
          <span><kbd className="palette-kbd">{KEYS.beside}</kbd>{t("palette.key.beside")}</span>
          <span className="palette-count">{plural("palette.count", total)}</span>
        </div>
      </div>
    </Sheet>
  );
}
