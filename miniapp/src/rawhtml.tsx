// Markup the app has already rendered to a string — markdown, a highlighted line, a code card —
// put into the page without React redrawing it on every render.

import { createElement, memo, useMemo } from "react";

/**
 * An element whose content is `html`, written into the page only when `html` changes.
 *
 * Every `dangerouslySetInnerHTML` in the app goes through this. React 19 compares that prop by the
 * identity of the `{ __html }` object, not by the string in it, and writes `innerHTML` whenever the
 * object is new — React 18 compared the strings. An inline `{{ __html: html }}` is a new object on
 * every render, so each render threw away the element's whole subtree and built it again from the
 * same string. While a run was on, the live turn re-rendered every second for its elapsed time and
 * the session screen on every event and poll: the running command's code card lost its scroll
 * position every second or two, and a file open in the right-hand panel lost its scroll and the
 * operator's selection in it. The object is now made once per string, so an unchanged string keeps
 * its nodes, and with them the scroll position and the selection.
 */
export const RawHtml = memo(function RawHtml({ html, className, as = "div" }: { html: string; className?: string; as?: "div" | "span" }) {
  const inner = useMemo(() => ({ __html: html }), [html]);
  return createElement(as, { className, dangerouslySetInnerHTML: inner });
});
