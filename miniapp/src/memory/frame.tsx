// What the Memory screen's two views share: the header (the view switch, the search, the commands),
// the field under the list that files a note, and the pane an entry or a fact opens in. Both views
// are one list column beside one pane, as the Inbox is, so they are drawn from the same pieces.

import { useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { Icon } from "../icons";
import { t } from "../i18n";
import { navigate, pathFor } from "../router";
import { PageHeader, screenTitle, useMedia } from "../ui/index";
import { Button, IconButton, OverflowMenu, Segmented, useLayer, type MenuItem } from "../ui/primitives";

export type Mode = "entries" | "facts";

/** The pane stands beside the list from this width on; narrower, an entry fills the page and a fact
 *  opens in a sheet, because a 480 px pane would leave the list under 400. */
export const SPLIT_QUERY = "(min-width: 1280px)";

export function useSplit(): boolean {
  return useMedia(SPLIT_QUERY);
}

const showMode = (next: Mode) => navigate(pathFor("memory", null, next === "facts" ? { view: "facts" } : {}), { replace: true });

type HeadProps = {
  mode: Mode;
  subtitle?: ReactNode;
  query: string;
  onQuery: (query: string) => void;
  onHistory: () => void;
  /** The commands behind the header's ⋮: import, export, settings, and in All facts, Select. */
  menu: MenuItem[];
  /** Remember something with its scope, category and topics: All facts only; the entries have the field under the list. */
  onAdd?: () => void;
  /** An entry open as a page (a window too narrow for the pane): the way back to the list. */
  back?: string;
  title?: ReactNode;
  actions?: ReactNode;
};

/** The 52 px header. The search stands in it on a desktop; on a phone it is a glyph that opens a row
 *  under the switch, since a phone has no room beside the title for a field. */
export function MemoryHead({ mode, subtitle, query, onQuery, onHistory, menu, onAdd, back, title, actions }: HeadProps) {
  const phone = useMedia("(max-width: 1023px)");
  const [open, setOpen] = useState(false);
  const label = t(mode === "facts" ? "memory.search.label" : "memory.entries.search");
  const field = (
    <label className="memory-search">
      <Icon name="search" size={16} />
      <input
        type="search" value={query} autoFocus={phone} aria-label={label} placeholder={t(mode === "facts" ? "memory.search" : "memory.entries.search")}
        onChange={(e) => onQuery(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Escape" && query) { e.preventDefault(); e.stopPropagation(); onQuery(""); } }}
      />
      {query && <IconButton icon="close" size={28} label={t("memory.search.clear")} onClick={() => onQuery("")} />}
    </label>
  );
  return (
    <PageHeader
      back={back}
      title={title ?? screenTitle("memory")}
      subtitle={subtitle}
      filters={back ? undefined : (
        <div className="memory-filters">
          <Segmented label={t("memory.mode")} size="md" className="memory-mode" value={mode} onChange={showMode}
            options={[{ id: "entries", label: t("memory.mode.entries") }, { id: "facts", label: t("memory.mode.facts") }]} />
          {phone && open && field}
        </div>
      )}
      actions={
        <>
          {actions}
          {back ? null : phone ? <IconButton icon="search" label={label} pressed={open || !!query} onClick={() => { setOpen((v) => !v); if (open) onQuery(""); }} /> : field}
          <IconButton icon="clock" label={t("memory.history")} onClick={onHistory} />
          <OverflowMenu label={t("memory.more")} className="memory-more" items={menu} />
          {onAdd && (phone
            ? <IconButton icon="plus" label={t("memory.add")} className="primary" onClick={onAdd} />
            : <Button variant="primary" icon="plus" className="memory-add" onClick={onAdd}>{t("memory.sheet.add")}</Button>)}
        </>
      }
    />
  );
}

/** The field under a list or an entry: one line that grows, sent with Enter. On the list it files a
 *  note; in an entry it changes the entry in the operator's words. */
export function MemoryComposer({ placeholder, label, busy, onSend, inPane }: { placeholder: string; label: string; busy: boolean; onSend: (text: string) => Promise<boolean>; inPane?: boolean }) {
  const [text, setText] = useState("");
  const area = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    const el = area.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
  }, [text]);
  async function send() {
    const value = text.trim();
    if (!value || busy) return;
    if (await onSend(value)) setText("");
  }
  const key = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void send();
    }
  };
  return (
    <div className={`memory-composer ${inPane ? "in-pane" : ""}`} data-busy={busy || undefined}>
      <div className="memory-composer-pill">
        <textarea ref={area} rows={1} value={text} placeholder={placeholder} aria-label={label} onChange={(e) => setText(e.target.value)} onKeyDown={key} disabled={busy} />
        <IconButton icon={busy ? "dot" : "send"} label={label} className="memory-send primary" disabled={busy || !text.trim()} onClick={() => void send()} />
      </div>
    </div>
  );
}

/** The pane an entry or a fact opens in, beside the list: a kind and a menu at the top, the body, and
 *  at the bottom what changes it. Escape closes it unless a field has the focus, where Escape is the
 *  field's own (it cancels the edit). */
export function MemoryPane({ kind, meta, menu, onClose, label, foot, children }: { kind: string; meta?: ReactNode; menu?: MenuItem[]; onClose: () => void; label: string; foot?: ReactNode; children: ReactNode }) {
  const pane = useRef<HTMLElement>(null);
  useLayer(() => {
    const active = document.activeElement;
    if (active instanceof HTMLElement && pane.current?.contains(active) && active.matches("input, textarea")) return;
    onClose();
  }, true);
  return (
    <aside className="memory-pane" ref={pane} aria-label={label}>
      <div className="memory-pane-head">
        <span className="memory-pane-kind">{kind}</span>
        {meta && <span className="memory-pane-meta">{meta}</span>}
        <span className="grow" />
        {menu && menu.length > 0 && <OverflowMenu label={t("memory.pane.actions")} items={menu} />}
        <IconButton icon="close" label={t("memory.pane.close")} onClick={onClose} />
      </div>
      <div className="memory-pane-body">{children}</div>
      {foot}
    </aside>
  );
}
