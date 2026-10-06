// Finding an event by its words: a field in the header on a desktop (`/` focuses it), a sheet on a
// phone. A result jumps the calendar to its day and opens it.

import { forwardRef, useEffect, useImperativeHandle, useRef, useState, type CSSProperties } from "react";
import { api } from "../api";
import { locale, t } from "../i18n";
import { Icon } from "../icons";
import { Sheet } from "../ui/dialogs";
import { clockLabel, dayLabel } from "./dates";
import type { Occurrence } from "./types";
import { toWall } from "./zone";

function useSearch(query: string): { results: Occurrence[] | null; busy: boolean; error: string } {
  const [results, setResults] = useState<Occurrence[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    const words = query.trim();
    if (words.length < 2) { setResults(null); setError(""); return; }
    let live = true;
    setBusy(true);
    // A quarter of a second after the last key: a search per keystroke would answer words already gone.
    const timer = window.setTimeout(() => {
      api.get<Occurrence[]>(`/api/calendar/search?q=${encodeURIComponent(words)}&limit=20`)
        .then((found) => { if (live) { setResults(found); setError(""); } })
        .catch((exc: Error) => { if (live) setError(exc.message); })
        .finally(() => { if (live) setBusy(false); });
    }, 250);
    return () => { live = false; window.clearTimeout(timer); };
  }, [query]);
  return { results, busy, error };
}

function Results({ results, busy, error, zone, onPick, active }: { results: Occurrence[] | null; busy: boolean; error: string; zone: string; onPick: (o: Occurrence) => void; active: number }) {
  if (error) return <div className="cal-search-note bad">{error}</div>;
  if (!results) return busy ? <div className="cal-search-note">{t("common.loading")}</div> : null;
  if (!results.length) return <div className="cal-search-note">{t("cal.search.none")}</div>;
  return (
    <ul className="cal-search-results" role="listbox" aria-label={t("cal.search.results")}>
      {results.map((o, i) => {
        const at = toWall(o.start_at, zone);
        const day = o.all_day ? (o.start_date || o.start_at.slice(0, 10)) : at.day;
        return (
          <li key={o.id} role="option" aria-selected={i === active}>
            <button type="button" className={`cal-search-item ${i === active ? "on" : ""}`} onMouseDown={(e) => e.preventDefault()} onClick={() => onPick(o)} style={{ "--c": o.color } as CSSProperties}>
              <span className="cal-dot" aria-hidden="true" />
              <span className="cal-search-text">
                <b>{o.title || t("cal.untitled")}</b>
                <small>{dayLabel(day, locale(), { weekday: "short", day: "numeric", month: "short", year: "numeric" })}{o.all_day ? "" : ` · ${clockLabel(at.minutes, locale())}`}{o.location ? ` · ${o.location}` : ""}</small>
              </span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}

export type SearchHandle = { focus: () => void };

export const SearchBox = forwardRef<SearchHandle, { zone: string; onPick: (o: Occurrence) => void }>(function SearchBox({ zone, onPick }, ref) {
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const found = useSearch(query);
  useImperativeHandle(ref, () => ({ focus: () => { input.current?.focus(); input.current?.select(); } }), []);
  useEffect(() => setActive(0), [found.results]);
  const pick = (o: Occurrence) => { onPick(o); setOpen(false); input.current?.blur(); };
  return (
    <div className="cal-search">
      <Icon name="search" size={14} />
      <input ref={input} type="search" value={query} placeholder={t("cal.search")} aria-label={t("cal.search")} aria-expanded={open && !!found.results} role="combobox" aria-autocomplete="list"
        onChange={(e) => { setQuery(e.target.value); setOpen(true); }} onFocus={() => setOpen(true)} onBlur={() => setOpen(false)}
        onKeyDown={(e) => {
          const list = found.results ?? [];
          if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); if (query) setQuery(""); else input.current?.blur(); }
          else if (e.key === "ArrowDown") { e.preventDefault(); setActive((a) => Math.min(list.length - 1, a + 1)); }
          else if (e.key === "ArrowUp") { e.preventDefault(); setActive((a) => Math.max(0, a - 1)); }
          else if (e.key === "Enter" && list[active]) { e.preventDefault(); pick(list[active]); }
        }} />
      <kbd className="cal-kbd" aria-hidden="true">/</kbd>
      {open && query.trim().length >= 2 && (
        <div className="cal-search-pop">
          <Results {...found} zone={zone} onPick={pick} active={active} />
        </div>
      )}
    </div>
  );
});

export function SearchSheet({ zone, onPick, onClose }: { zone: string; onPick: (o: Occurrence) => void; onClose: () => void }) {
  const [query, setQuery] = useState("");
  const found = useSearch(query);
  return (
    <Sheet title={t("cal.search")} onClose={onClose} className="cal-search-sheet">
      <input className="field" type="search" autoFocus value={query} onChange={(e) => setQuery(e.target.value)} placeholder={t("cal.search.placeholder")} aria-label={t("cal.search")} />
      <Results {...found} zone={zone} onPick={(o) => { onPick(o); onClose(); }} active={-1} />
    </Sheet>
  );
}
