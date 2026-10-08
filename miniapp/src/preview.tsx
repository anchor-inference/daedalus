// File previews: images, Markdown, CSV, PDF, Word, Excel, audio/video and plain text — inline in
// the right panel, in a sheet on a phone, and in a dialog for attachments waiting in the composer. Everything is fetched with the
// auth header (an <img src> cannot carry one) and shown from a blob URL; the office formats are
// converted in the browser by libraries loaded only when such a file is opened.

import { Overlay, useLayer } from "./ui/dialogs";
import { useEffect, useState } from "react";
import { api } from "./api";
import { Icon } from "./icons";
import { renderMarkdown } from "./md";
import { errorText, fmtBytes } from "./ui";
import { t } from "./i18n";
import { SourceView, JsonView, DiffView, ImageView } from "./previewparts";
import { RawHtml } from "./rawhtml";
import { HtmlPreview, HtmlNavigation } from "./htmlpreview";
import { parseCsv } from "./csv";
export { parseCsv } from "./csv";
import { FileSkeleton } from "./feedback";
import { createSharedFetch, type Joined } from "./sharedfetch";

/** Where the bytes come from: a file under a session API root, or a File object from the composer.
 * `lines` ("20-40", or a single number) is what an answer cited: the file opens as source with that range marked. */
export type PreviewSource = { base: string; path: string; lines?: string } | { file: File };

export const sessionBase = (sessionId: string) => `/api/sessions/${sessionId}`;

export type PreviewKind = "image" | "markdown" | "csv" | "pdf" | "docx" | "sheet" | "audio" | "video" | "text" | "html" | "json" | "diff" | "other";

const IMAGE_RE = /\.(png|jpe?g|gif|webp|bmp|svg|avif|ico)$/i;
const TEXT_RE = /\.(txt|log|json|jsonl|ya?ml|toml|ini|cfg|conf|env|py|ts|tsx|js|jsx|mjs|cjs|sh|bash|zsh|sql|xml|rs|go|java|kt|c|h|cpp|hpp|cs|rb|php|swift|css|scss|less|diff|patch|lock|gitignore|dockerfile|makefile|tex|bib|srt|vtt)$/i;

export function previewKind(name: string): PreviewKind {
  const n = name.toLowerCase();
  if (/\.(json|jsonl)$/.test(n)) return "json";
  if (/\.(diff|patch)$/.test(n)) return "diff";
  if (IMAGE_RE.test(n)) return "image";
  if (/\.(md|markdown|mdx)$/.test(n)) return "markdown";
  if (/\.(csv|tsv)$/.test(n)) return "csv";
  if (/\.pdf$/.test(n)) return "pdf";
  if (/\.docx$/.test(n)) return "docx";
  if (/\.(xlsx|xlsm|xls|ods)$/.test(n)) return "sheet";
  if (/\.(mp3|wav|ogg|oga|m4a|flac|aac|opus)$/.test(n)) return "audio";
  if (/\.(mp4|webm|mov|m4v|ogv)$/.test(n)) return "video";
  if (/\.(html?|htm)$/.test(n)) return "html";
  if (TEXT_RE.test(n) || !/\.[a-z0-9]{1,8}$/.test(n)) return "text";
  return "other";
}

export function canPreview(name: string): boolean {
  return previewKind(name) !== "other";
}

/** An emoji for a file row, by what the file is. */
export function fileGlyph(name: string, dir = false): string {
  if (dir) return "📁";
  switch (previewKind(name)) {
    case "image":
      return "🖼";
    case "pdf":
      return "📕";
    case "docx":
      return "📝";
    case "sheet":
    case "csv":
      return "📊";
    case "audio":
      return "🎵";
    case "video":
      return "🎬";
    case "markdown":
      return "📘";
    default:
      return "📄";
  }
}

/** The address a browser tab can open the file at: the token travels in the query, since a tab carries no header. */
export function downloadHref(base: string, path: string): string {
  let token: string | null = null;
  try {
    token = sessionStorage.getItem("daedalus_token");
  } catch {
    /* private mode */
  }
  return `${base}/download?path=${encodeURIComponent(path)}${token ? `&token=${encodeURIComponent(token)}` : ""}`;
}

function sourceName(src: PreviewSource): string {
  return "file" in src ? src.file.name : src.path.split("/").pop() || src.path;
}

const downloads = createSharedFetch<Blob>();

/** The bytes of a source. A file on the host is read once however many viewers ask for it at the
 *  same moment (sharedfetch.ts); a file from the composer is already here. */
function sourceBlob(src: PreviewSource, onProgress: (value: number | null) => void): Joined<Blob> {
  if ("file" in src) return { promise: Promise.resolve(src.file), release: () => undefined };
  const url = `${src.base}/download?path=${encodeURIComponent(src.path)}`;
  return downloads.join(url, (signal, progress) => fetchBlob(url, signal, progress), onProgress);
}

async function fetchBlob(url: string, signal: AbortSignal, progress: (value: number | null) => void): Promise<Blob> {
  const res = await fetch(url, { headers: api.authHeaders(), signal });
  if (!res.ok) throw new Error(res.status === 404 ? t("preview.nofile") : t("preview.failed", { status: res.status }));
  if (!res.body) return res.blob();
  const reader = res.body.getReader();
  const total = Number(res.headers.get("content-length"));
  const chunks: Uint8Array<ArrayBuffer>[] = [];
  let received = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    received += value.length;
    progress(total > 0 ? Math.min(1, received / total) : null);
  }
  return new Blob(chunks, { type: res.headers.get("content-type") || "application/octet-stream" });
}

/** A blob URL for the source, revoked when the component that asked for it goes away. */
export function useBlobUrl(src: PreviewSource | null): { url: string | null; blob: Blob | null; error: string | null; progress: number | null } {
  const [state, setState] = useState<{ url: string | null; blob: Blob | null; error: string | null; progress: number | null }>({ url: null, blob: null, error: null, progress: null });
  const key = src === null ? "" : "file" in src ? `file:${src.file.name}:${src.file.size}:${src.file.lastModified}` : `${src.base}:${src.path}`;
  useEffect(() => {
    if (!src) return;
    let url: string | null = null;
    let gone = false;
    setState({ url: null, blob: null, error: null, progress: null });
    const read = sourceBlob(src, (progress) => { if (!gone) setState((s) => ({ ...s, progress })); });
    read.promise
      .then((blob) => {
        if (gone) return;
        url = URL.createObjectURL(blob);
        setState({ url, blob, error: null, progress: 1 });
      })
      .catch((e) => !gone && setState({ url: null, blob: null, error: errorText(e), progress: null }));
    return () => {
      gone = true;
      read.release();
      if (url) URL.revokeObjectURL(url);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  return state;
}

/** An image fetched with the auth header. */
export function AuthImg({ src, alt, className, onClick }: { src: PreviewSource; alt: string; className?: string; onClick?: () => void }) {
  const { url, error } = useBlobUrl(src);
  if (error) return <span className="sub">{alt}: {error}</span>;
  if (!url) return <span className={`${className ?? ""} img-loading`} aria-busy="true" />;
  return <img className={className} src={url} alt={alt} onClick={onClick} style={onClick ? { cursor: "zoom-in" } : undefined} />;
}

// ── parsers ────────────────────────────────────────────────────────────────────────────

function Grid({ rows, note }: { rows: string[][]; note?: string }) {
  if (!rows.length) return <div className="empty">{t("preview.empty")}</div>;
  const width = Math.max(...rows.map((r) => r.length));
  return (
    <div className="tablewrap preview-grid">
      <table>
        <thead>
          <tr>
            <th className="num">#</th>
            {Array.from({ length: width }, (_, i) => <th key={i}>{rows[0][i] ?? ""}</th>)}
          </tr>
        </thead>
        <tbody>
          {rows.slice(1).map((r, i) => (
            <tr key={i}>
              <td className="num sub">{i + 1}</td>
              {Array.from({ length: width }, (_, j) => <td key={j}>{r[j] ?? ""}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
      {note && <div className="sub" style={{ padding: "6px 0" }}>{note}</div>}
    </div>
  );
}

// ── cited lines ────────────────────────────────────────────────────────────────────────

/** "20-40" or "20" → the inclusive line range, or null when it is neither. */
export function parseRange(spec: string): { from: number; to: number } | null {
  const m = /^\s*(\d+)\s*(?:[-–:]\s*(\d+))?\s*$/.exec(spec);
  if (!m) return null;
  const from = Number(m[1]);
  const to = m[2] ? Number(m[2]) : from;
  return from >= 1 && to >= from ? { from, to } : null;
}

const TEXT_LIMIT = 512_000;

/** What the viewer learned about the file once it arrived: for the host's toolbar (download, size). */
export type ViewerInfo = { url: string | null; size: number | null; name: string; kind: PreviewKind; loading: boolean; progress: number | null };

/** The file itself, rendered inline into whatever hosts it: the preview dialog, the right panel's
 *  Preview tab, a phone sheet. It fetches, parses and draws; the host draws the chrome around it. */
export function Viewer({ src, onInfo, onNavigation, className }: { src: PreviewSource; onInfo?: (info: ViewerInfo) => void; onNavigation?: (nav: HtmlNavigation | null) => void; className?: string }) {
  const name = sourceName(src);
  // A cited range is about the source, so a Markdown or CSV file opens as text rather than rendered;
  // the rendered view stays one press away. The range once decided the view outright, with the
  // Preview button disabled, and a file an answer cited by lines could never be read rendered.
  const cited = "path" in src && src.lines ? parseRange(src.lines) : null;
  const [source, setSource] = useState(!!cited);
  const [binary, setBinary] = useState(false);
  const kind = binary ? "other" : source && ["markdown", "csv", "html", "json", "diff", "text"].includes(previewKind(name)) ? "text" : previewKind(name);
  const { url, blob, error, progress } = useBlobUrl(src);
  const [pdfUrl, setPdfUrl] = useState<string | null>(null);
  const [pdfError, setPdfError] = useState<string | null>(null);
  const [body, setBody] = useState<{ html?: string; text?: string; rows?: string[][]; sheets?: { name: string; rows: string[][] }[]; error?: string } | null>(null);
  const [sheet, setSheet] = useState(0);

  useEffect(() => {
    setPdfUrl(null);
    setPdfError(null);
    if (kind !== "pdf" || !blob) return;
    // The native PDF viewer cannot run in a sandboxed frame. A data URL gives its document an
    // opaque origin while keeping the viewer usable; the original blob URL remains a download.
    const reader = new FileReader();
    reader.onload = () => setPdfUrl(String(reader.result));
    reader.onerror = () => setPdfError(t("preview.nofile"));
    reader.readAsDataURL(new Blob([blob], { type: "application/pdf" }));
    return () => { reader.onload = null; reader.onerror = null; reader.abort(); };
  }, [blob, kind]);

  useEffect(() => {
    setBody(null);
    setSheet(0);
    if (!blob) return;
    let gone = false;
    const done = (b: typeof body) => !gone && setBody(b);
    (async () => {
      try {
        if (kind === "markdown" || kind === "text" || kind === "csv" || kind === "html" || kind === "json" || kind === "diff") {
          const text = await blob.slice(0, TEXT_LIMIT).text();
          if (text.includes("\0")) { if (!gone) setBinary(true); return; }
          const clipped = blob.size > TEXT_LIMIT ? `\n\n${t("preview.more", { size: fmtBytes(blob.size - TEXT_LIMIT) })}` : "";
          if (kind === "markdown") done({ html: renderMarkdown(text + clipped) });
          else if (kind === "csv") {
            const rows = parseCsv(text, 2000, /\.tsv$/i.test(name) ? "\t" : undefined);
            done({ rows, error: rows.length >= 2000 ? t("preview.rows") : undefined });
          } else done({ text: text + clipped });
        } else if (kind === "docx") {
          const mammoth = await import("mammoth");
          const r = await mammoth.convertToHtml({ arrayBuffer: await blob.arrayBuffer() });
          done({ html: r.value });
        } else if (kind === "sheet") {
          const XLSX = await import("xlsx");
          const wb = XLSX.read(await blob.arrayBuffer(), { type: "array" });
          const sheets = wb.SheetNames.map((n) => {
            const rows = XLSX.utils.sheet_to_json<unknown[]>(wb.Sheets[n], { header: 1, raw: false, defval: "" }) as unknown[][];
            return { name: n, rows: rows.slice(0, 2000).map((r) => r.map((c) => (c === null || c === undefined ? "" : String(c)))) };
          });
          done({ sheets });
        } else done({});
      } catch (e) {
        done({ error: errorText(e) });
      }
    })();
    return () => {
      gone = true;
    };
  }, [blob, kind]);

  const failure = error ?? pdfError ?? (body?.rows ? null : body?.error);
  const loading = !failure && (!url || (kind === "pdf" && !pdfUrl) || (kind !== "image" && kind !== "pdf" && kind !== "audio" && kind !== "video" && kind !== "other" && !body));

  useEffect(() => { onInfo?.({ url, size: blob?.size ?? null, name, kind, loading, progress }); }, [url, blob, name, kind, loading, progress, onInfo]);

  return (
    <div className={`viewer ${kind} ${className ?? ""}`}>
      {!onInfo && loading && <div className={`preview-progress ${progress === null ? "busy" : ""}`} role="progressbar" aria-label={t("common.loading")}><i style={progress === null ? undefined : { width: `${progress * 100}%` }} /></div>}
      {["markdown", "html", "json", "diff"].includes(previewKind(name)) && <div className="source-tools segmented"><button className={!source ? "on" : ""} aria-pressed={!source} onClick={() => setSource(false)}>{t("preview.rendered")}</button><button className={source ? "on" : ""} aria-pressed={source} onClick={() => setSource(true)}>{t("preview.source")}</button></div>}
      {body?.sheets && body.sheets.length > 1 && (
        <div className="segmented preview-tabs">
          {body.sheets.map((s, i) => (
            <button key={s.name} className={i === sheet ? "on" : ""} onClick={() => setSheet(i)}>{s.name}</button>
          ))}
        </div>
      )}
      <div className="preview-body">
        {failure && <div className="empty">{failure}</div>}
        {loading && <FileSkeleton />}
        {!failure && url && kind === "image" && <ImageView url={url} name={name} />}
        {!failure && pdfUrl && kind === "pdf" && <iframe className="preview-frame" src={pdfUrl} title={name} />}
        {!failure && url && kind === "audio" && <audio className="preview-media" controls src={url} />}
        {!failure && url && kind === "video" && <video className="preview-media" controls src={url} />}
        {!failure && body?.html && kind === "markdown" && <RawHtml className="answer preview-doc" html={body.html} />}
        {!failure && body?.html && kind === "docx" && <RawHtml className="answer preview-doc docx" html={body.html} />}
        {!failure && body?.text !== undefined && kind === "html" && <HtmlPreview text={body.text} base={"base" in src ? src.base : undefined} path={"path" in src ? src.path : name} onNavigation={onNavigation} />}
        {!failure && body?.text !== undefined && kind === "text" && <SourceView text={body.text} name={name} range={cited} />}
        {!failure && body?.text !== undefined && kind === "json" && <JsonView text={body.text} />}
        {!failure && body?.text !== undefined && kind === "diff" && <DiffView text={body.text} />}
        {!failure && body?.rows && <Grid rows={body.rows} note={body.error} />}
        {!failure && body?.sheets && <Grid rows={body.sheets[sheet]?.rows ?? []} note={(body.sheets[sheet]?.rows.length ?? 0) >= 2000 ? t("preview.rows") : undefined} />}
        {!failure && url && kind === "other" && (
          <div className="empty">
            {t("preview.none")}
            <div style={{ marginTop: 10 }}>
              <a className="btn small" href={url} download={name}>{t("preview.download")} {fmtBytes(blob?.size ?? 0)}</a>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

/** The viewer in a dialog: for a file waiting in the composer, and for phones without the panel. */
export function FilePreview({ src, onClose }: { src: PreviewSource; onClose: () => void }) {
  const name = sourceName(src);
  const cited = "path" in src && src.lines ? parseRange(src.lines) : null;
  const [info, setInfo] = useState<ViewerInfo | null>(null);
  useLayer(onClose);
  return (
    <Overlay>
    <div className="sheet-backdrop preview-backdrop" onClick={onClose}>
      <div className={`sheet preview ${info?.kind ?? previewKind(name)}`} onClick={(e) => e.stopPropagation()} role="dialog" aria-label={name}>
        <div className="sheet-head">
          <h3 className="preview-name" title={"file" in src ? name : src.path}>
            <span aria-hidden>{fileGlyph(name)}</span> {name}
            {cited && <span className="sub">{t("preview.lines", { range: cited.from === cited.to ? cited.from : `${cited.from}–${cited.to}` })}</span>}
            {info?.size != null && <span className="sub"> · {fmtBytes(info.size)}</span>}
          </h3>
          <div className="head-actions">
            {info?.url && (
              <a className="iconbtn small" href={info.url} download={name} title={t("common.download")} aria-label={t("common.download")}>
                <Icon name="download" size={16} />
              </a>
            )}
            <button className="iconbtn small" onClick={onClose} aria-label={t("common.close")} title={t("common.close")}>
              <Icon name="close" size={16} />
            </button>
          </div>
        </div>
        {info?.loading && <div className="preview-progress busy" role="progressbar" aria-label={t("common.loading")}><i /></div>}
        <Viewer src={src} onInfo={setInfo} />
      </div>
    </div>
    </Overlay>
  );
}
