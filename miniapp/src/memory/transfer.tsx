// Memory in and out. The export is the host's zip, fetched with the credentials a link cannot carry
// and handed to the browser (or the desktop window) as a download. The import is two steps: paste
// what another assistant answered to the import prompt, or pick a Daedalus export; the host parses
// and scans it, and the preview shows every note with where it would go and what the scan found —
// agent-directed text can never be ticked, sensitive or already-held notes start unticked.

import { useMemo, useState } from "react";
import { api, type MemoryImportItem } from "../api";
import { Icon } from "../icons";
import { plural, t } from "../i18n";
import { errorText } from "../ui";
import { copyText } from "../ui/components";
import { Sheet, toast } from "../ui/dialogs";
import { undoToast } from "./undo";

/** The export zip, downloaded under the name the host gives it. */
export async function downloadExport(): Promise<void> {
  const response = await fetch("/api/memory/export.zip", { headers: api.authHeaders() });
  if (!response.ok) throw new Error(t("memory.export.failed", { status: response.status }));
  const name = /filename="([^"]+)"/.exec(response.headers.get("Content-Disposition") ?? "")?.[1] ?? "daedalus-memory.zip";
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  toast(t("memory.export.done"));
}

/** A file as base64 without its data-URL prefix, for the preview's ``archive`` field. */
function asBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error ?? new Error("read failed"));
    reader.onload = () => resolve(String(reader.result).replace(/^data:[^,]*,/, ""));
    reader.readAsDataURL(file);
  });
}

/** Whether an import item may be ticked at all: text addressed to an AI agent is refused by the host
 *  whatever is sent, so the preview does not pretend otherwise. */
export function importable(item: MemoryImportItem): boolean {
  return item.injection.length === 0;
}

/** The import's items grouped by the entry they would land in, in the order they first appear. */
export function byTarget(items: MemoryImportItem[]): [string, MemoryImportItem[]][] {
  const groups = new Map<string, MemoryImportItem[]>();
  for (const item of items) groups.set(item.title || item.slug, [...(groups.get(item.title || item.slug) ?? []), item]);
  return [...groups.entries()];
}

export function ImportSheet({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [text, setText] = useState("");
  const [items, setItems] = useState<MemoryImportItem[] | null>(null);
  const [ticked, setTicked] = useState<Set<number>>(new Set());
  const [busy, setBusy] = useState(false);
  const prompt = t("memory.import.prompt");

  async function preview(body: { text?: string; archive?: string }) {
    setBusy(true);
    try {
      const res = await api.post<{ items: MemoryImportItem[] }>("/api/memory/import/preview", body);
      setItems(res.items);
      setTicked(new Set(res.items.filter((i) => i.checked && importable(i)).map((i) => i.key)));
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  async function pick(file: File | undefined) {
    if (!file) return;
    if (/\.zip$/i.test(file.name)) await preview({ archive: await asBase64(file) });
    else await preview({ text: await file.text() });
  }

  async function apply() {
    if (!items) return;
    setBusy(true);
    try {
      const chosen = items.filter((i) => ticked.has(i.key));
      const res = await api.post<{ imported: number; refused: number; history_id: number | null }>("/api/memory/import/apply", { items: chosen });
      undoToast(res.refused ? t("memory.import.done.refused", { n: res.imported, refused: res.refused }) : plural("memory.import.done", res.imported), res.history_id, onDone);
      onDone();
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  const groups = useMemo(() => byTarget(items ?? []), [items]);
  const toggle = (key: number) => setTicked((s) => { const n = new Set(s); if (n.has(key)) n.delete(key); else n.add(key); return n; });

  if (items) {
    return (
      <Sheet title={t("memory.import.preview")} onClose={onClose} size="wide" className="memory-import">
        <div className="sub">{items.length ? t("memory.import.preview.sub", { n: items.length }) : t("memory.import.none")}</div>
        {groups.map(([target, list]) => (
          <section key={target} className="memory-import-group" data-target={target}>
            <div className="memory-section-head">{target}</div>
            <ul className="memory-import-list">
              {list.map((item) => (
                <li key={item.key} className="memory-import-item" data-import={item.key} data-flagged={!item.checked || undefined}>
                  <input type="checkbox" checked={ticked.has(item.key)} disabled={!importable(item)} aria-label={t("memory.import.take")} onChange={() => toggle(item.key)} />
                  <span className="memory-import-text">
                    <span>{item.text}</span>
                    <span className="memory-import-flags">
                      {item.date && <span className="faint">{item.date}</span>}
                      {item.injection.length > 0 && <span className="memory-flag bad" title={item.injection.join("; ")}>{t("memory.import.injection")}</span>}
                      {item.sensitive.map((s) => <span key={s} className="memory-flag warn">{t(`settings.memory.subject.${s}`)}</span>)}
                      {item.duplicate && <span className="memory-flag">{t("memory.import.duplicate")}</span>}
                      {item.forgotten && <span className="memory-flag">{t("memory.import.forgotten")}</span>}
                    </span>
                  </span>
                </li>
              ))}
            </ul>
          </section>
        ))}
        <div className="sheet-foot">
          <button className="btn ghost" onClick={() => setItems(null)}>{t("memory.import.back")}</button>
          <span className="grow" />
          <button className="btn primary" data-action="import-apply" disabled={busy || ticked.size === 0} onClick={() => void apply()}>{plural("memory.import.apply", ticked.size)}</button>
        </div>
      </Sheet>
    );
  }

  return (
    <Sheet title={t("memory.import.title")} onClose={onClose} size="wide" className="memory-import">
      <div className="sub">{t("memory.import.sub")}</div>
      <div className="memory-import-step">{t("memory.import.step1")}</div>
      <div className="memory-import-prompt">
        <pre>{prompt}</pre>
        <button className="btn small" data-action="copy-prompt" onClick={async () => toast((await copyText(prompt)) ? t("memory.import.copied") : prompt)}><Icon name="copy" size={14} /> {t("common.copy")}</button>
      </div>
      <div className="memory-import-step">{t("memory.import.step2")}</div>
      <textarea className="field" rows={7} value={text} placeholder={t("memory.import.paste")} aria-label={t("memory.import.paste")} onChange={(e) => setText(e.target.value)} />
      <div className="sub">{t("memory.import.untrusted")}</div>
      <div className="sheet-foot">
        <label className="btn ghost memory-file">
          <Icon name="attach" size={14} /> {t("memory.import.file")}
          <input type="file" accept=".zip,.md,.json,.txt" hidden onChange={(e) => void pick(e.target.files?.[0])} />
        </label>
        <span className="grow" />
        <button className="btn ghost" onClick={onClose}>{t("common.cancel")}</button>
        <button className="btn primary" data-action="import-preview" disabled={busy || !text.trim()} onClick={() => void preview({ text })}>{t("memory.import.next")}</button>
      </div>
    </Sheet>
  );
}
