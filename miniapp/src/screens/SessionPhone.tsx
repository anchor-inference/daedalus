// The session's phone dress: the top bar, the ⋮ sheet that holds every action the desktop header
// shows as buttons, and the small sheets those actions open (search, rename, sub-agents). The
// screen keeps the state and the commands; this file only lays them out the way the phone design
// does, so a desktop never renders any of it and the two cannot drift apart in what they can do —
// every desktop header action has its row or tile here.

import { useEffect, useState, type ReactNode } from "react";
import type { SessionDetail } from "../api";
import { Dot, statusWord } from "../ui/components";
import { Icon, type IconName } from "../icons";
import { plural, t } from "../i18n";
import { BottomSheet, IconButton, SheetRow, TopBar } from "../ui/phone";
import { ChatSearchBody } from "../chatsearch";

export type SessionTile = { id: string; icon: IconName; label: string; count?: number | null; attn?: boolean; disabled?: boolean; onSelect: () => void };
export type SessionRow = { icon: IconName; label: string; value?: ReactNode; danger?: boolean; disabled?: boolean; onSelect: () => void };

/**
 * The bar: hamburger (or back), a two-line title button and at most two glyphs. The title opens the
 * session's details, where the full title, the model and the context are; the second line is the
 * live state while something happens and the model with its effort otherwise.
 */
export function SessionTopBar({ title, sub, back, onTitle, chip, onMenu, editing, shared }: { title: string; sub: ReactNode; back?: () => void; onTitle?: () => void; chip?: ReactNode; onMenu: () => void; editing?: ReactNode; shared?: "public" | "key" | null }) {
  // A chat anyone can open says so on the bar itself, not only inside the ⋮ sheet: the desktop's
  // header chip has no room here, so it rides at the head of the second line.
  const share = shared ? <span className={`chip ph-share-tag ${shared === "public" ? "bad" : "attn"}`}>{t(shared === "public" ? "session.share.chip.public" : "session.share.chip.key")}</span> : null;
  return (
    <TopBar
      className="chat-head ph-chat-top"
      title={editing ?? <span className="chat-title">{title}</span>}
      sub={editing ? undefined : <>{share}{sub}</>}
      back={back}
      onTitle={editing ? undefined : onTitle}
      titleLabel={onTitle ? t("session.phone.title", { title }) : undefined}
      actions={<>{chip}<IconButton icon="vdots" label={t("session.actions")} onClick={onMenu} popup="menu" /></>}
    />
  );
}

/** The bar's second line while the session is not idle: a coloured dot and the state in words. */
export function StateSub({ tone, children }: { tone: "running" | "waiting" | "failed" | "compacting" | "offline" | "saving"; children: ReactNode }) {
  return <span className={`ph-chat-state ${tone}`}><Dot status={tone === "offline" ? "waiting" : tone === "saving" ? "idle" : tone} /><span>{children}</span></span>;
}

/** The ⋮ sheet: tools as tiles first, then the session's commands as rows, destructive last. */
export function SessionMenuSheet({ tiles, rows, onClose }: { tiles: SessionTile[]; rows: SessionRow[]; onClose: () => void }) {
  const safe = rows.filter((row) => !row.danger);
  const danger = rows.filter((row) => row.danger);
  const pick = (run: () => void) => { onClose(); run(); };
  return (
    <BottomSheet onClose={onClose} className="ph-actions ph-session-menu" label={t("session.actions")}>
      <div className="ph-tiles four" role="group" aria-label={t("session.phone.tools")}>
        {tiles.map((tile) => (
          <button key={tile.id} type="button" className={`ph-tile ${tile.attn ? "attn" : ""}`} data-tile={tile.id} disabled={tile.disabled} onClick={() => pick(tile.onSelect)}>
            <Icon name={tile.icon} size={22} />
            <span>{tile.count ? `${tile.label} · ${tile.count}` : tile.label}</span>
          </button>
        ))}
      </div>
      <div role="menu">
        {safe.map((row) => <SheetRow key={row.label} icon={row.icon} label={row.label} value={row.value} disabled={row.disabled} onClick={() => pick(row.onSelect)} />)}
        {danger.length > 0 && <div className="ph-msep" role="separator" />}
        {danger.map((row) => <SheetRow key={row.label} icon={row.icon} label={row.label} disabled={row.disabled} danger onClick={() => pick(row.onSelect)} />)}
      </div>
    </BottomSheet>
  );
}

/** Search in this chat, as a sheet: the desktop's popover anchored to a header button has no button
 *  to anchor to on a phone. */
export function SearchSheet({ sessionId, onPick, onClose }: { sessionId: string; onPick: (seq: number) => void; onClose: () => void }) {
  // The field takes the focus once the sheet has taken it for itself on opening, so the keyboard
  // comes up with the sheet rather than after a second tap.
  useEffect(() => {
    const frame = requestAnimationFrame(() => document.querySelector<HTMLInputElement>(".ph-search-sheet input[type=search]")?.focus());
    return () => cancelAnimationFrame(frame);
  }, []);
  return (
    <BottomSheet title={t("chatsearch.open")} onClose={onClose} className="ph-search-sheet">
      <ChatSearchBody sessionId={sessionId} onPick={(seq) => { onClose(); onPick(seq); }} />
    </BottomSheet>
  );
}

/** Rename as a sheet with one field: an input squeezed into the 56 px bar left no room to read it. */
export function RenameSheet({ title, onSave, onClose }: { title: string; onSave: (title: string) => void; onClose: () => void }) {
  const [value, setValue] = useState(title);
  return (
    <BottomSheet
      title={t("session.rename")}
      onClose={onClose}
      className="ph-rename"
      footer={<div className="ph-foot-row">
        <button type="button" className="ph-btn" onClick={onClose}>{t("common.cancel")}</button>
        <button type="button" className="ph-btn primary" disabled={!value.trim()} onClick={() => onSave(value)}>{t("common.save")}</button>
      </div>}
    >
      <div className="ph-sheet-pad">
        <input className="ph-field" autoFocus value={value} onChange={(e) => setValue(e.target.value)} aria-label={t("session.rename")} onKeyDown={(e) => { if (e.key === "Enter") onSave(value); }} />
      </div>
    </BottomSheet>
  );
}

/** The sub-agents and the leader, as rows: the desktop's popover beside its header button. */
export function SubagentsSheet({ detail, onOpen, onClose }: { detail: SessionDetail; onOpen?: (id: string) => void; onClose: () => void }) {
  const children = detail.subagents ?? [];
  const open = (id: string) => { onClose(); onOpen?.(id); };
  return (
    <BottomSheet title={t("session.subagents.title")} onClose={onClose} className="ph-actions">
      {children.length > 0 && <div className="ph-sheet-pad sub">{plural("session.subagents", children.length)}</div>}
      <div role="menu">
        {detail.subagent_of && <SheetRow icon="back" label={detail.leader_title ?? t("session.leader.word")} hint={t("session.leader.word")} onClick={() => open(detail.subagent_of!)} />}
        {children.map((child) => (
          <SheetRow
            key={child.session_id}
            icon="spawn"
            label={child.name || child.session_id}
            hint={`${child.running ? statusWord("running") : child.status === "failed" ? statusWord("failed") : child.kept ? t("session.sub.kept") : statusWord("done")}${child.model ? ` · ${child.model}` : ""}`}
            chevron
            onClick={() => open(child.session_id)}
          />
        ))}
      </div>
    </BottomSheet>
  );
}
