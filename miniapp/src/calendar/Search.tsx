// Finding an event by its words: a button in the header (`/` opens it) that drops a search field and
// its results under it, a sheet below 1024 px. A result jumps the calendar to its day and opens it.

import { useEffect, useState, type CSSProperties } from "react";
import { api } from "../api";
import { locale, t } from "../i18n";
import { Combobox, IconButton, Popover } from "../ui/primitives";
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

/** The search button and, while it is open, the field with what it found. The caller holds `open`
 *  because the `/` key opens it from outside. */
export function SearchControl({ zone, open, onOpen, onClose, onPick }: { zone: string; open: boolean; onOpen: () => void; onClose: () => void; onPick: (o: Occurrence) => void }) {
  const [anchor, setAnchor] = useState<HTMLSpanElement | null>(null);
  const [query, setQuery] = useState("");
  const found = useSearch(query);
  const close = () => { setQuery(""); onClose(); };
  const note = found.error ? <span className="cal-search-note bad">{found.error}</span>
    : query.trim().length < 2 ? t("cal.search.placeholder")
    : found.busy && !found.results ? t("common.loading")
    : t("cal.search.none");
  return (
    <>
      <span ref={setAnchor} className="cal-search-anchor">
        <IconButton icon="search" label={t("cal.search")} kbd="/" pressed={open} onClick={() => (open ? close() : onOpen())} />
      </span>
      {open && (
        <Popover anchor={anchor} onClose={close} label={t("cal.search")} width={400} side="bottom" align="end" role="dialog" className="cal-search-pop">
          <Combobox inline autoFocus items={found.results ?? []} query={query} onQuery={setQuery} label={t("cal.search")} placeholder={t("cal.search.placeholder")}
            toLabel={(o) => `${o.id} ${o.title}`} itemClassName="cal-search-item" empty={note}
            render={(o) => <Result o={o} zone={zone} />}
            onChoose={(o) => { onPick(o); close(); }} />
        </Popover>
      )}
    </>
  );
}

function Result({ o, zone }: { o: Occurrence; zone: string }) {
  const at = toWall(o.start_at, zone);
  const day = o.all_day ? (o.start_date || o.start_at.slice(0, 10)) : at.day;
  return (
    <>
      <span className="cal-dot" style={{ "--c": o.color } as CSSProperties} aria-hidden="true" />
      <span className="cal-search-text">
        <b>{o.title || t("cal.untitled")}</b>
        <small>{dayLabel(day, locale(), { weekday: "short", day: "numeric", month: "short", year: "numeric" })}{o.all_day ? "" : ` · ${clockLabel(at.minutes, locale())}`}{o.location ? ` · ${o.location}` : ""}</small>
      </span>
    </>
  );
}
