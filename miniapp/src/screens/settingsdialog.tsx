// The parts of the Settings dialog that are behaviour rather than sections: the keys it answers, the
// focus it keeps, and the search results drawn as the sections' own live controls.
//
// The results are the real section bodies, filtered. A row drawn on its own outside its section
// would need every section taken apart into rows, and the sections are large components other work
// keeps changing; filtering what a section draws keeps each row exactly the control it is on its
// page — changed from the results, it is changed — and a row added to a section later is found by
// the search as soon as the index names it.

import "./settings.css";
import type React from "react";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { useLayer } from "../ui/dialogs";
import { lang, plural, t } from "../i18n";
import { go } from "../ui/index";
import { fieldPath, type IndexField } from "./settingsindex";

/** The attribute a row the search found carries, and the one on everything the search hides. */
const HIT = "data-hit";
const OFF = "data-search-off";
const HEADINGS = ".section-title, .settings-subhead";

/**
 * Leaves in `root` only the rows whose `data-setting` is in `anchors`, the boxes that hold them and
 * the heading over each. Whatever is inside a found row stays whole (a card anchored as one, like the
 * colours, keeps its swatches). Answers the anchors it found, so the caller can offer a link for the
 * rest: a row behind a tab that is not open, or one a section draws only once something is switched on.
 */
export function filterHits(root: HTMLElement, anchors: Set<string>): Set<string> {
  for (const el of root.querySelectorAll(`[${HIT}], [${OFF}]`)) {
    el.removeAttribute(HIT);
    el.removeAttribute(OFF);
  }
  const hits = [...root.querySelectorAll<HTMLElement>("[data-setting]")].filter((el) => anchors.has(el.dataset.setting ?? ""));
  for (const hit of hits) hit.setAttribute(HIT, "");
  const walk = (parent: Element) => {
    for (const child of parent.children) {
      if (child.hasAttribute(HIT)) continue;
      if (hits.some((hit) => child.contains(hit))) walk(child);
      else child.setAttribute(OFF, "");
    }
  };
  walk(root);
  // The heading nearest above each found row says which group it is in ("History compaction"), the
  // second half of "Section › group"; the card's other headings stay hidden with their rows.
  for (const hit of hits) {
    for (let node: Element | null = hit; node && node !== root; node = node.parentElement) {
      let before = node.previousElementSibling;
      while (before && !before.matches(HEADINGS) && before.hasAttribute(OFF)) before = before.previousElementSibling;
      if (before?.matches(HEADINGS)) {
        before.removeAttribute(OFF);
        break;
      }
      if (before) break;
    }
  }
  return new Set(hits.map((hit) => hit.dataset.setting ?? ""));
}

/** A section's body cut down to the rows a search found, with a link for each found row the body
 *  does not draw right now. The section is the one the dialog draws, so the row works here as it does
 *  on its page. A section's settings arrive after it is drawn, so the filter runs again whenever the
 *  body changes; the links for missing rows wait a moment for that, rather than flashing up first. */
export function HitBody({ fields, children }: { fields: IndexField[]; children: React.ReactNode }) {
  const box = useRef<HTMLDivElement>(null);
  const [found, setFound] = useState<Set<string> | null>(null);
  const key = fields.map((field) => field.anchor).join("\n");
  useLayoutEffect(() => {
    const root = box.current;
    if (!root) return;
    const anchors = new Set(key.split("\n"));
    let frame = 0;
    let settled = false;
    const run = () => {
      frame = 0;
      const seen = filterHits(root, anchors);
      if (settled) setFound(seen);
    };
    run();
    const observer = new MutationObserver(() => {
      if (!frame) frame = requestAnimationFrame(run);
    });
    observer.observe(root, { childList: true, subtree: true });
    const wait = window.setTimeout(() => {
      settled = true;
      run();
    }, 600);
    return () => {
      observer.disconnect();
      cancelAnimationFrame(frame);
      window.clearTimeout(wait);
    };
  }, [key]);
  const missing = found ? fields.filter((field) => !found.has(field.anchor)) : [];
  return (
    <>
      <div ref={box} className="settings-hit-body">{children}</div>
      {missing.length > 0 && (
        <div className="card settings-hit-links">
          {missing.map((field) => (
            <a key={field.anchor} className="settings-hit-link" href={fieldPath(field)} onClick={(e) => go(e, fieldPath(field))}>
              <b>{field.title[lang()]}</b>
              <span className="sub">{field.about[lang()]}</span>
            </a>
          ))}
        </div>
      )}
    </>
  );
}

/** Whether a key press was typed into something: `/` there is a character, not a shortcut. */
function typing(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  return !!el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName));
}

/**
 * The dialog's keys and focus. Escape is answered in this order: an open list inside the dialog
 * closes itself first (the dropdown hears the same key), then a search with words in it is emptied,
 * then the dialog closes. `/` puts the cursor in the search. Focus moves into the dialog when it opens;
 * the shell gives it back to whatever opened it (App.tsx), since only the shell sees it before it goes.
 */
export function useDialogKeys({ dialog, search, query, clear, onClose }: { dialog: React.RefObject<HTMLElement | null>; search: React.RefObject<HTMLInputElement | null>; query: string; clear: () => void; onClose: () => void }) {
  const state = useRef({ query, clear, onClose });
  state.current = { query, clear, onClose };
  useLayer(() => {
    const root = dialog.current;
    // The dropdown and the model list close themselves on the same key; the dialog stays.
    if (root?.querySelector('.dropdown-btn[aria-expanded="true"], .models-menu')) return;
    if (state.current.query) state.current.clear();
    else state.current.onClose();
  });
  useEffect(() => {
    // Into the dialog, unless a link already put the cursor somewhere inside it.
    if (!dialog.current?.contains(document.activeElement)) dialog.current?.focus({ preventScroll: true });
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "/" || e.ctrlKey || e.metaKey || e.altKey || typing(e.target)) return;
      if (!search.current || search.current.getClientRects().length === 0) return;
      e.preventDefault();
      search.current.focus();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [dialog, search]);
}

/** Whether a sheet or a confirmation is open over the dialog. While one is, the dialog stops saying
 *  it is modal: Chromium honours one modal at a time, and with two it let the rows under the sheet back
 *  into what a screen reader (and a test reading by role) sees. Sheets are portalled to the body. */
export function useCovered(): boolean {
  const [covered, setCovered] = useState(false);
  useEffect(() => {
    const check = () => setCovered(!!document.querySelector("body > .overlay-root"));
    check();
    const observer = new MutationObserver(check);
    observer.observe(document.body, { childList: true });
    return () => observer.disconnect();
  }, []);
  return covered;
}

/** Tab and Shift+Tab go round the dialog's own controls: the screen under it is out of reach. */
export function trapTab(e: React.KeyboardEvent<HTMLElement>) {
  if (e.key !== "Tab") return;
  const items = [...e.currentTarget.querySelectorAll<HTMLElement>("button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), a[href], [tabindex]:not([tabindex='-1'])")].filter((el) => el.getClientRects().length > 0);
  if (!items.length) return;
  const at = items.indexOf(document.activeElement as HTMLElement);
  if (e.shiftKey && at <= 0) {
    e.preventDefault();
    items[items.length - 1].focus();
  } else if (!e.shiftKey && (at < 0 || at === items.length - 1)) {
    e.preventDefault();
    items[0].focus();
  }
}

/** Up and Down move through the list of sections (and the rows a search found under them); Enter
 *  opens the one focused, as the link it is. From the search field, Down enters the list. */
export function moveInList(e: React.KeyboardEvent<HTMLElement>, list: HTMLElement | null) {
  if ((e.key !== "ArrowDown" && e.key !== "ArrowUp") || !list) return;
  const links = [...list.querySelectorAll<HTMLElement>("a[href]")].filter((el) => el.getClientRects().length > 0);
  if (!links.length) return;
  const at = links.indexOf(document.activeElement as HTMLElement);
  const next = at < 0 ? (e.key === "ArrowDown" ? 0 : links.length - 1) : Math.min(links.length - 1, Math.max(0, at + (e.key === "ArrowDown" ? 1 : -1)));
  e.preventDefault();
  links[next].focus();
}

/** "4 rows in 2 sections", each noun agreeing with its number in both languages. */
export function hitCount(rows: number, sections: number): string {
  return t("settings.find.count", { rows: plural("settings.find.rows", rows), sections: plural("settings.find.sections", sections) });
}
