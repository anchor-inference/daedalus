// Rich views share the same downloaded bytes. Numbered source is bounded in the DOM so a long
// generated file does not make the conversation expensive just because its panel is open.

import { Fragment, type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { highlightLines, langOf, HIGHLIGHT_MAX_LINES } from "./highlight";
import { jsonRows, parseJsonText, toggleRow } from "./jsontree";
import { diffFileName, lineAnchor, parseDiff } from "./diff";
import { t } from "./i18n";
import { RawHtml } from "./rawhtml";

export function SourceView({ text, name, range }: { text: string; name: string; range?: { from: number; to: number } | null }) {
  const [wrap, setWrap] = useState(false);
  const [jump, setJump] = useState(String(range?.from ?? 1));
  const [at, setAt] = useState(range?.from ?? 1);
  const [mark, setMark] = useState(range);
  const first = useRef<HTMLDivElement>(null);
  const lines = useMemo(() => highlightLines(text, text.split("\n").length > HIGHLIGHT_MAX_LINES ? "text" : langOf(name)), [text, name]);
  const start = Math.max(0, Math.min(lines.length - 1, at - 1) - 120);
  const end = Math.min(lines.length, start + 400);
  useEffect(() => { first.current?.scrollIntoView({ block: "center" }); }, [at, text]);
  return <>
    <div className="source-tools">
      <button className={`btn small ${wrap ? "on" : ""}`} aria-pressed={wrap} onClick={() => setWrap((v) => !v)}>{t("preview.wrap")}</button>
      <form onSubmit={(e) => { e.preventDefault(); const n = Math.max(1, Math.min(lines.length, Number(jump) || 1)); setAt(n); setMark({ from: n, to: n }); }}><input className="field" type="number" min="1" max={lines.length} aria-label={t("preview.goto")} value={jump} onChange={(e) => setJump(e.target.value)} /><button className="btn small" type="submit">{t("preview.goto")}</button></form>
      <span className="sub">{t("preview.lines.of", { from: start + 1, to: end, total: lines.length })}</span>
    </div>
    <div className={`filetext lined source-view ${wrap ? "wrap" : ""}`}>
      {start > 0 && <button className="linkbtn" onClick={() => setAt(Math.max(1, at - 280))}>{t("preview.previous")}</button>}
      {lines.slice(start, end).map((line, i) => { const n = start + i + 1; return <div key={n} data-line={n} className={`line ${mark && n >= mark.from && n <= mark.to ? "cited" : ""}`} ref={n === at ? first : undefined}><span className="linenum">{n}</span><RawHtml as="span" className="linebody" html={line || " "} /></div>; })}
      {end < lines.length && <button className="linkbtn" onClick={() => setAt(at + 280)}>{t("preview.next")}</button>}
    </div>
  </>;
}

export function JsonView({ text }: { text: string }) {
  const parsed = useMemo(() => parseJsonText(text), [text]);
  const [state, setState] = useState({ toggled: new Set<string>() });
  if ("error" in parsed) return <div className="empty">{parsed.error}</div>;
  const rows = jsonRows(parsed.value, state);
  return <div className="json-tree" role="tree" aria-label={t("preview.json")}>
    {rows.map((r) => <div key={r.id} className="json-row" role="treeitem" aria-level={r.depth + 1} aria-expanded={r.container ? r.open : undefined} style={{ paddingLeft: r.depth * 16 }}>
      {r.container ? <button className="linkbtn" onClick={() => setState((s) => toggleRow(s, r.id))} aria-label={`${r.key || t("preview.json")} (${r.count})`}><span aria-hidden>{r.open ? "⌄" : "›"}</span> {r.key && `${JSON.stringify(r.key)}: `}{r.kind === "array" ? `[${r.count}]` : `{${r.count}}`} <span className="sub">{r.text}</span></button> : <span>{r.key && `${JSON.stringify(r.key)}: `}<span className={`json-${r.kind}`}>{r.text}</span></span>}
    </div>)}
    {rows.length >= 5000 && <div className="sub">{t("preview.json.limit")}</div>}
  </div>;
}

/**
 * A unified diff, coloured. With ``onLine`` every line that maps to the head becomes a button that
 * hands its file and line to a review note; a plain click is the gesture because it is the only one a
 * phone has, and a drag that selects text is not taken for it.
 */
/** `file` shows that one file of the patch (a phone's review opens files one at a time); `notes`
 *  draws what was said about a line under it, by the new line's number. */
export function DiffView({ text, onLine, file, notes }: { text: string; onLine?: (anchor: { path: string; line: number }) => void; file?: string; notes?: (anchor: { path: string; line: number }) => ReactNode }) {
  const all = useMemo(() => parseDiff(text), [text]);
  const files = file === undefined ? all : all.filter((f) => diffFileName(f) === file);
  return <div className={`diff-view${onLine ? " commentable" : ""}`}>{files.map((f, i) => <section key={i}>
    <div className="diff-file">{diffFileName(f)} <span className="tk-add">+{f.added}</span> <span className="tk-del">−{f.removed}</span></div>
    {f.hunks.map((h, j) => <div key={j}><div className="diff-hunk">{h.header}</div>{h.lines.map((l, k) => {
      const anchor = onLine ? lineAnchor(f, h, k) : null;
      const pick = anchor && onLine ? () => { if (!window.getSelection()?.toString()) onLine(anchor); } : undefined;
      const row = <div key={k} className={`diff-line diff-${l.type}`} role={pick ? "button" : undefined} tabIndex={pick ? 0 : undefined}
        title={anchor ? t("diff.commentAt", { path: anchor.path, line: anchor.line }) : undefined}
        onClick={pick} onKeyDown={pick ? (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); pick(); } } : undefined}>
        <span className="linenum">{l.oldNo}</span><span className="linenum">{l.newNo}</span><span className="diff-sign">{l.type === "add" ? "+" : l.type === "del" ? "−" : " "}</span><span>{l.text}</span></div>;
      const said = notes && l.newNo != null && f.newPath && f.newPath !== "/dev/null" ? notes({ path: f.newPath, line: l.newNo }) : null;
      return said ? <Fragment key={k}>{row}{said}</Fragment> : row;
    })}</div>)}
    {!f.hunks.length && <pre className="filetext">{text}</pre>}
  </section>)}{!files.length && <pre className="filetext">{text}</pre>}</div>;
}

export function ImageView({ url, name }: { url: string; name: string }) {
  const [actual, setActual] = useState(false);
  const [size, setSize] = useState("");
  return <><div className="source-tools"><button className="btn small" aria-pressed={actual} onClick={() => setActual((v) => !v)}>{actual ? t("preview.fit") : "1:1"}</button><span className="sub">{size}</span></div><div className={`image-pan ${actual ? "actual" : ""}`}><img className="preview-image" src={url} alt={name} onLoad={(e) => setSize(`${e.currentTarget.naturalWidth} × ${e.currentTarget.naturalHeight} px`)} onClick={() => setActual((v) => !v)} /></div></>;
}
