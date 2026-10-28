// What a rich answer needs once it is on the page: site marks fetched through the host, a card
// picture that failed replaced by its site's mark, the popover of a citation chip, and the turn's
// sources — the button under the answer and the panel it opens.

import { memo, useEffect, useMemo, useState } from "react";
import { api } from "./api";
import { plural, t } from "./i18n";
import { Icon } from "./icons";
import { type Search, type TurnSources, type WebSource, knownSource, rememberSources, siteOf, sourceCount, turnSources, type SourceTool } from "./rich";

// ── site marks ──────────────────────────────────────────────────────────────────────────────

// One request per site for the whole app, answered by the host from its own cache. The bytes come
// back as a blob because the request must carry the credential, which an <img src> cannot (inside
// Telegram the credential is a header). A site with no mark resolves to null and keeps its letter.
const marks = new Map<string, Promise<string | null>>();

export function faviconUrl(host: string): Promise<string | null> {
  const known = marks.get(host);
  if (known) return known;
  const asked = fetch(`/api/favicon?host=${encodeURIComponent(host)}`, { headers: api.authHeaders() })
    .then(async (response) => (response.ok && (response.headers.get("content-type") ?? "").startsWith("image/") ? URL.createObjectURL(await response.blob()) : null))
    .catch(() => null);
  marks.set(host, asked);
  return asked;
}

function paint(mark: HTMLElement): void {
  const host = mark.dataset.host;
  if (!host || mark.dataset.painted) return;
  mark.dataset.painted = "1";
  void faviconUrl(host).then((url) => {
    if (!url) return;
    mark.style.backgroundImage = `url("${url}")`;
    mark.classList.add("has-mark");
  });
}

/** Fill the site marks of markup just written into the page. Cheap when there are none. */
export function hydrateRich(root: HTMLElement): void {
  root.querySelectorAll<HTMLElement>(".fav[data-host]").forEach(paint);
}

// ── the citation popover ────────────────────────────────────────────────────────────────────

let pop: HTMLDivElement | null = null;
let popFor: HTMLElement | null = null;
let hoverTimer = 0;

function closePop(): void {
  pop?.remove();
  pop = null;
  popFor = null;
}

function escapeText(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function openPop(chip: HTMLElement): void {
  if (popFor === chip) return;
  closePop();
  const urls = (chip.dataset.cite ?? "").split(" ").filter(Boolean);
  if (!urls.length) return;
  const rows = urls.map((url) => {
    const source = knownSource(url);
    const host = siteOf(url);
    const letter = escapeText((host.match(/[\p{L}\p{N}]/u)?.[0] ?? "·").toUpperCase());
    const title = source.title || url.replace(/^https?:\/\/(?:www\.)?/, "");
    return `<a class="cite-pop-row" href="${escapeText(url)}" target="_blank" rel="noreferrer"><span class="cite-pop-site"><span class="fav" data-host="${escapeText(host)}" aria-hidden="true">${letter}</span>${escapeText(host)}</span><span class="cite-pop-title">${escapeText(title)}</span></a>`;
  });
  pop = document.createElement("div");
  pop.className = "cite-pop";
  pop.setAttribute("role", "dialog");
  pop.setAttribute("aria-label", t("rich.sources.title"));
  pop.innerHTML = rows.join("");
  document.body.appendChild(pop);
  popFor = chip;
  hydrateRich(pop);
  // Under the chip, kept inside the window; above it when there is no room below.
  const box = chip.getBoundingClientRect();
  const width = Math.min(320, window.innerWidth - 16);
  pop.style.width = `${width}px`;
  pop.style.left = `${Math.max(8, Math.min(box.left, window.innerWidth - width - 8))}px`;
  const below = box.bottom + 6;
  const height = pop.getBoundingClientRect().height;
  pop.style.top = `${below + height > window.innerHeight - 8 ? Math.max(8, box.top - height - 6) : below}px`;
  pop.addEventListener("mouseenter", () => window.clearTimeout(hoverTimer));
  pop.addEventListener("mouseleave", () => { hoverTimer = window.setTimeout(closePop, 200); });
}

if (typeof document !== "undefined") {
  // A tap opens and closes the popover; a pointer that can hover opens it on hover as well, and the
  // popover stays while the pointer is over the chip or the popover itself.
  document.addEventListener("click", (e) => {
    const target = e.target as HTMLElement | null;
    const chip = target?.closest?.("button.cite") as HTMLElement | null;
    if (chip) {
      e.preventDefault();
      if (popFor === chip) closePop();
      else openPop(chip);
      return;
    }
    if (pop && !pop.contains(target)) closePop();
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closePop(); });
  document.addEventListener("scroll", (e) => { if (pop && !pop.contains(e.target as Node)) closePop(); }, true);
  const hovers = typeof window !== "undefined" && window.matchMedia?.("(hover: hover)").matches;
  if (hovers) {
    document.addEventListener("mouseover", (e) => {
      const chip = (e.target as HTMLElement | null)?.closest?.("button.cite") as HTMLElement | null;
      if (!chip) return;
      window.clearTimeout(hoverTimer);
      hoverTimer = window.setTimeout(() => openPop(chip), 250);
    });
    document.addEventListener("mouseout", (e) => {
      const chip = (e.target as HTMLElement | null)?.closest?.("button.cite") as HTMLElement | null;
      if (!chip || chip.contains(e.relatedTarget as Node | null)) return;
      window.clearTimeout(hoverTimer);
      if (popFor === chip) hoverTimer = window.setTimeout(closePop, 200);
    });
  }
  // A card's picture that did not load: the site's mark takes its place. Error events do not
  // bubble, so this listens in the capture phase.
  document.addEventListener("error", (e) => {
    const img = e.target as HTMLElement | null;
    if (!(img instanceof HTMLImageElement) || !img.classList.contains("rcard-img")) return;
    const host = img.dataset.host ?? "";
    const tile = document.createElement("span");
    tile.className = "fav fav-lg";
    tile.dataset.host = host;
    tile.setAttribute("aria-hidden", "true");
    tile.textContent = (host.match(/[\p{L}\p{N}]/u)?.[0] ?? "·").toUpperCase();
    img.parentElement?.classList.add("rcard-tile");
    img.replaceWith(tile);
    if (host) paint(tile);
  }, true);
}

// ── the turn's sources ──────────────────────────────────────────────────────────────────────

/** A site's mark as a component, for the panel and the button. */
export const Fav = memo(function Fav({ url, large = false }: { url: string; large?: boolean }) {
  const host = siteOf(url);
  const [mark, setMark] = useState<string | null>(null);
  useEffect(() => {
    let gone = false;
    if (host) void faviconUrl(host).then((found) => { if (!gone) setMark(found); });
    return () => { gone = true; };
  }, [host]);
  return (
    <span className={`fav ${large ? "fav-lg" : ""} ${mark ? "has-mark" : ""}`} style={mark ? { backgroundImage: `url("${mark}")` } : undefined} aria-hidden="true">
      {(host.match(/[\p{L}\p{N}]/u)?.[0] ?? "·").toUpperCase()}
    </span>
  );
});

/** A turn's sources, and the popover's knowledge of their titles kept up to date. */
export function useTurnSources(answer: string, activity: readonly SourceTool[]): TurnSources {
  const sources = useMemo(() => turnSources(answer, activity), [answer, activity]);
  useEffect(() => rememberSources(sources), [sources]);
  return sources;
}

/** "12 sources" with the first sites' marks stacked, under an answer that read or cited the web. */
export function SourcesButton({ sources, onOpen }: { sources: TurnSources; onOpen: () => void }) {
  const count = sourceCount(sources);
  if (!count) return null;
  const firsts = [...sources.cited, ...sources.more].filter((s, i, all) => all.findIndex((o) => siteOf(o.url) === siteOf(s.url)) === i).slice(0, 3);
  return (
    <button type="button" className="sources-button" onClick={onOpen} aria-label={plural("rich.sources.count", count)}>
      <span className="sources-stack">{firsts.map((source) => <Fav key={source.url} url={source.url} />)}</span>
      <span>{plural("rich.sources.count", count)}</span>
    </button>
  );
}

function SourceRow({ source }: { source: WebSource }) {
  const host = siteOf(source.url);
  return (
    <a className="source-row" href={source.url} target="_blank" rel="noreferrer">
      <span className="source-site"><Fav url={source.url} />{host}</span>
      <span className="source-title">{source.title || source.url.replace(/^https?:\/\/(?:www\.)?/, "")}</span>
      {source.snippet && <span className="source-snippet">{source.snippet}</span>}
    </a>
  );
}

function SearchRow({ search }: { search: Search }) {
  return (
    <div className="source-search">
      <Icon name="search" size={14} />
      <span className="source-search-text">
        <span className="source-search-label">{t("rich.sources.searched")}</span>
        <span className="source-search-query">{search.query}</span>
      </span>
      <span className="source-search-count">{search.count}</span>
    </div>
  );
}

/** The sources panel: what the answer cites, what else the turn read, and the searches it made. */
export function SourcesPanel({ sources }: { sources: TurnSources | null }) {
  const [all, setAll] = useState(false);
  if (!sources || (!sourceCount(sources) && !sources.searches.length)) return <div className="empty">{t("rich.sources.none")}</div>;
  const MORE_SHOWN = 8;
  const more = all ? sources.more : sources.more.slice(0, MORE_SHOWN);
  return (
    <div className="sources-panel">
      {sources.cited.length > 0 && (
        <section>
          <h4 className="sources-head">{t("rich.sources.cited")}</h4>
          {sources.cited.map((source) => <SourceRow key={source.url} source={source} />)}
        </section>
      )}
      {sources.more.length > 0 && (
        <section>
          <h4 className="sources-head">{sources.cited.length ? t("rich.sources.more") : t("rich.sources.read")}</h4>
          {more.map((source) => <SourceRow key={source.url} source={source} />)}
          {!all && sources.more.length > MORE_SHOWN && (
            <button type="button" className="sources-all" onClick={() => setAll(true)}>{plural("rich.sources.rest", sources.more.length - MORE_SHOWN)}</button>
          )}
        </section>
      )}
      {sources.searches.length > 0 && (
        <section>
          <h4 className="sources-head">{t("rich.sources.searches")}</h4>
          {sources.searches.map((search, index) => <SearchRow key={`${index}-${search.query}`} search={search} />)}
        </section>
      )}
    </div>
  );
}
