// A terminal's own actions where it is one row of a list — a project's Terminals page, on a desktop
// and on a phone: rename it, end what runs in it, or forget it once it has ended. The same requests
// and the same questions as the Terminals screen's full view and the session's dock, so ending a
// staff member's terminal warns here exactly as it does there.

import { useState, type ReactNode } from "react";
import { api, type TerminalView } from "../api";
import { OverflowMenu, Sheet, type MenuItem } from "../ui/dialogs";
import { t } from "../i18n";
import { invalidate } from "../store";
import { errorText } from "../ui";
import { endTerminal } from "./actions";

/** A terminal's commands and the rename sheet they may open: the row menu's here, and the phone's
 *  long-press sheet on the Terminals screen, so both ask and warn the same way. */
export function useTerminalCommands(row: TerminalView, toast: (text: string) => void, onRemoved?: () => void): { items: MenuItem[]; layer: ReactNode } {
  const [renaming, setRenaming] = useState(false);
  const [title, setTitle] = useState(row.title);
  const [saving, setSaving] = useState(false);
  const shown = row.title || t("term.untitled");
  const running = row.status === "running";

  async function rename() {
    if (saving) return;
    setSaving(true);
    try {
      await api.renameTerminal(row.id, title.trim());
      invalidate("/api/terminals");
      setRenaming(false);
    } catch (error) {
      toast(errorText(error));
    } finally {
      setSaving(false);
    }
  }
  async function end() {
    await endTerminal(row.id, row.title, toast);
    invalidate("/api/terminals");
  }
  async function remove() {
    try {
      await api.removeTerminal(row.id);
    } catch (error) {
      toast(errorText(error));
      return;
    }
    invalidate("/api/terminals");
    onRemoved?.();
  }

  const items: MenuItem[] = [
    { label: t("term.rename"), icon: "pen", hint: t("term.rename.hint"), onSelect: () => { setTitle(row.title); setRenaming(true); } },
    running
      ? { label: t("term.end"), icon: "stop", danger: true, hint: t("ph.term.end.hint"), onSelect: () => void end() }
      : { label: t("term.remove"), icon: "trash", danger: true, onSelect: () => void remove() },
  ];
  const layer = renaming ? (
    <Sheet title={t("term.rename")} onClose={() => setRenaming(false)} size="narrow">
      <form className="term-rename" onSubmit={(e) => { e.preventDefault(); void rename(); }}>
        <input className="field" aria-label={t("term.rename")} autoFocus value={title} maxLength={200} placeholder={shown} onChange={(e) => setTitle(e.target.value)} />
        <div className="sub">{t("term.rename.hint")}</div>
        <button className="btn primary" type="submit" disabled={saving}>{t("common.save")}</button>
      </form>
    </Sheet>
  ) : null;
  return { items, layer };
}

export function TerminalRowMenu({ row, toast, onRemoved }: { row: TerminalView; toast: (text: string) => void; onRemoved?: () => void }) {
  const { items, layer } = useTerminalCommands(row, toast, onRemoved);
  const shown = row.title || t("term.untitled");
  // Beside the row's own link or button, never inside it: a press on the menu is not a press on the row.
  return (
    <span className="term-row-menu" data-terminal-menu={row.id} onClick={(e) => e.stopPropagation()} onKeyDown={(e) => e.stopPropagation()}>
      <OverflowMenu contextSelector=".focus-term-row, .phone-term-row" small className="quiet" label={t("term.row.menu", { title: shown })} items={items} />
      {layer}
    </span>
  );
}
