// A file's changes, drawn the way a review reads them: a header that stays at the top while the file
// scrolls under it, the lines with their numbers and an edge in the colour of what happened to them,
// long unchanged runs folded, and a word for the files that have no lines to show. The Changes page
// mounts it for a proposal's files; a chat's own working-tree changes can mount the same one, which
// is why it takes a parsed file and says nothing about where the diff came from.

import { memo, useMemo, useState, type ReactNode } from "react";
import { Icon } from "./icons";
import { plural, t } from "./i18n";
import { diffFileName, diffRows, splitPath, splitRows, type DiffFile, type DiffLine, type DiffRow, type SplitRow } from "./diff";
import { Segmented } from "./ui/primitives";
import "./diffpane.css";

export type DiffMode = "unified" | "split";

const SIGN = { add: "+", del: "−", ctx: "", meta: "" } as const;

/** The counts beside a file's name: "+24" and "−4", each shown only when there is something to count. */
export function Counts({ added, removed }: { added: number; removed: number }) {
  return (
    <>
      {(added > 0 || removed === 0) && <span className="dp-add">+{added}</span>}
      {removed > 0 && <span className="dp-del">−{removed}</span>}
    </>
  );
}

/**
 * The file's name over its changes: the folder dimmed, the name in full weight, what was added and
 * removed. `viewed` and `mode` draw the controls the page owns the state of; `children` stand at the
 * end, for a caller's own buttons. It sticks to the top of the scrolling column it is placed in.
 */
export function FileDiffHeader({ file, viewed, onViewed, mode, onMode, children }: { file: DiffFile; viewed?: boolean; onViewed?: (on: boolean) => void; mode?: DiffMode; onMode?: (mode: DiffMode) => void; children?: ReactNode }) {
  const { dir, name } = splitPath(diffFileName(file));
  return (
    <div className="dp-head">
      <Icon name="file" size={16} />
      <span className="dp-name" title={diffFileName(file)}><span className="dp-dir">{dir}</span><b>{name}</b></span>
      {(file.added > 0 || file.removed > 0) && <span className="dp-counts"><Counts added={file.added} removed={file.removed} /></span>}
      <span className="dp-spacer" />
      {onViewed && (
        <button type="button" className="dp-viewed" role="checkbox" aria-checked={!!viewed} onClick={() => onViewed(!viewed)}>
          <span className={`dp-box ${viewed ? "on" : ""}`} aria-hidden="true">{viewed && <Icon name="check" size={12} />}</span>
          {t("diff.viewed")}
        </button>
      )}
      {onMode && mode && (
        <Segmented<DiffMode> value={mode} onChange={onMode} label={t("diff.view")} className="dp-mode"
          options={[{ id: "unified", label: t("diff.view.unified") }, { id: "split", label: t("diff.view.split") }]} />
      )}
      {children}
    </div>
  );
}

const UnifiedLine = memo(function UnifiedLine({ line }: { line: DiffLine }) {
  if (line.type === "meta") return <div className="dp-line meta"><span className="dp-no" /><span className="dp-no" /><span className="dp-sign" /><span className="dp-text">{line.text}</span></div>;
  return (
    <div className={`dp-line ${line.type}`}>
      <span className="dp-no">{line.oldNo}</span>
      <span className="dp-no">{line.newNo}</span>
      <span className="dp-sign">{SIGN[line.type]}</span>
      <span className="dp-text">{line.text}</span>
    </div>
  );
});

const Half = memo(function Half({ line, side }: { line: DiffLine | null; side: "old" | "new" }) {
  if (!line) return <div className="dp-half blank"><span className="dp-no" /><span className="dp-text" /></div>;
  return (
    <div className={`dp-half ${line.type}`}>
      <span className="dp-no">{side === "old" ? line.oldNo : line.newNo}</span>
      <span className="dp-text">{line.text}</span>
    </div>
  );
});

function FoldRow({ lines, onOpen }: { lines: DiffLine[]; onOpen: () => void }) {
  const words = plural("diff.unchanged", lines.length);
  return (
    <button type="button" className="dp-fold" onClick={onOpen} aria-label={t("diff.fold.show", { n: lines.length })}>
      <Icon name="expand" size={14} />
      {words}
    </button>
  );
}

function GapRow({ count }: { count: number }) {
  return <div className="dp-fold gap">{plural("diff.unchanged", count)}</div>;
}

function Rows({ rows, open }: { rows: DiffRow[]; open: (id: string) => void }) {
  return (
    <>
      {rows.map((row, i) => {
        if (row.type === "hunk") return <div key={i} className="dp-hunk">{row.header}</div>;
        if (row.type === "gap") return <GapRow key={i} count={row.count} />;
        if (row.type === "fold") return <FoldRow key={i} lines={row.lines} onOpen={() => open(row.id)} />;
        return <UnifiedLine key={i} line={row.line} />;
      })}
    </>
  );
}

function SplitRows({ rows, open }: { rows: SplitRow[]; open: (id: string) => void }) {
  return (
    <>
      {rows.map((row, i) => {
        if (row.type === "hunk") return <div key={i} className="dp-hunk">{row.header}</div>;
        if (row.type === "gap") return <GapRow key={i} count={row.count} />;
        if (row.type === "fold") return <FoldRow key={i} lines={row.lines} onOpen={() => open(row.id)} />;
        return <div key={i} className="dp-pair"><Half line={row.left} side="old" /><Half line={row.right} side="new" /></div>;
      })}
    </>
  );
}

/** What stands where a file has no lines: made empty, only moved, or binary. */
function nothingToShow(file: DiffFile): string {
  if (file.binary) return t("diff.binary");
  if (file.kind === "renamed") return t("diff.renamed", { from: file.oldPath, to: file.newPath });
  if (file.kind === "added") return t("diff.emptyAdded");
  if (file.kind === "deleted") return t("diff.emptyDeleted");
  return t("diff.noLines");
}

/**
 * The lines of one file. `cut` says the patch the file came from was cut short and is drawn under the
 * lines (it is the caller's words: a pull request's diff is cut at a size the host chose, a chat's at
 * another). The component never shows a missing hunk as though the file ended there.
 */
export function FileDiffBody({ file, mode, cut }: { file: DiffFile; mode: DiffMode; cut?: ReactNode }) {
  const [open, setOpen] = useState<ReadonlySet<string>>(new Set());
  const rows = useMemo(() => diffRows(file, open), [file, open]);
  const sides = useMemo(() => (mode === "split" ? splitRows(rows) : null), [rows, mode]);
  const reveal = (id: string) => setOpen((now) => new Set(now).add(id));
  const empty = !file.hunks.length;
  return (
    <div className={`dp-body ${mode}`}>
      {empty ? <div className="dp-none">{nothingToShow(file)}</div> : sides ? <SplitRows rows={sides} open={reveal} /> : <Rows rows={rows} open={reveal} />}
      {cut && <div className="dp-cut" role="note">{cut}</div>}
    </div>
  );
}
