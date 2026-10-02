// The bell in the sidebar on a desktop: the unseen count, and a popover with what needs the
// operator on top (with its buttons, answered in place) and the day's feed under it. On a phone the
// Inbox tab is the same centre, so there is no bell there.

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { Notification } from "./api";
import { useLayer } from "./ui/dialogs";
import { Icon } from "./icons";
import { navigate, pathFor } from "./router";
import { useProjects } from "./projects";
import { NeedsYou, NotificationList, NotificationRow, byDay, byProject, markAllSeen, openEntry, projectNames, useNotifications, useSummary } from "./notifications";
import { plural, t } from "./i18n";

type Chip = "all" | "needs" | "projects";
const WIDTH = 400;
/** The popover's feed: a day's worth, not the archive. "Show all" is the Inbox. */
const FEED = 30;

export function Bell() {
  const summary = useSummary();
  const [open, setOpen] = useState(false);
  const button = useRef<HTMLButtonElement>(null);
  const n = summary.unseen;
  const label = n > 0 ? `${t("bell.title")} · ${plural("bell.unseen", n)}` : t("bell.title");
  return (
    <>
      <button ref={button} className={`iconbtn quiet bell ${open ? "on" : ""}`} onClick={() => setOpen((o) => !o)} title={label} aria-label={label} aria-haspopup="dialog" aria-expanded={open}>
        <Icon name="bell" size={18} />
        {n > 0 && <span className={`bell-badge num ${summary.needs_you > 0 ? "urgent" : ""}`} aria-hidden>{n > 99 ? "99+" : n}</span>}
      </button>
      {open && <BellPanel anchor={button.current} onClose={() => setOpen(false)} unseen={n} needs={summary.needs_you} />}
    </>
  );
}

function BellPanel({ anchor, onClose, unseen, needs }: { anchor: HTMLElement | null; onClose: () => void; unseen: number; needs: number }) {
  const box = useRef<HTMLDivElement>(null);
  const [chip, setChip] = useState<Chip>("all");
  const [pos, setPos] = useState<{ top: number; left: number; maxHeight: number } | null>(null);
  const projects = useProjects();
  const names = projectNames(projects.data);
  const feed = useNotifications("all", null, FEED);
  useLayer(onClose);

  useLayoutEffect(() => {
    if (!anchor) return;
    const place = () => {
      const r = anchor.getBoundingClientRect();
      const left = Math.max(8, Math.min(r.left, window.innerWidth - WIDTH - 8));
      const top = r.bottom + 6;
      setPos({ top, left, maxHeight: Math.max(200, window.innerHeight - top - 12) });
    };
    place();
    window.addEventListener("resize", place);
    return () => window.removeEventListener("resize", place);
  }, [anchor]);

  useEffect(() => {
    const onDown = (e: MouseEvent | TouchEvent) => {
      const target = e.target as Node;
      if (box.current?.contains(target) || anchor?.contains(target)) return;
      onClose();
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("touchstart", onDown);
    box.current?.focus();
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("touchstart", onDown);
      anchor?.focus();
    };
  }, [anchor, onClose]);

  const open = (entry: Notification) => {
    openEntry(entry);
    onClose();
  };
  // "Needs you" is drawn above the feed on its own, so the feed leaves those entries out.
  const entries = (feed.data?.entries ?? []).filter((e) => chip === "projects" || !e.needs_you);
  const groups = chip === "projects" ? byProject(feed.data?.entries ?? [], names) : byDay(entries);
  const row = (entry: Notification) => <NotificationRow key={entry.id} entry={entry} names={names} onActivate={() => open(entry)} />;

  return createPortal(
    <div
      ref={box}
      className="bell-pop"
      role="dialog"
      aria-label={t("bell.title")}
      tabIndex={-1}
      style={{ top: pos?.top ?? 8, left: pos?.left ?? 8, maxHeight: pos?.maxHeight, visibility: pos ? "visible" : "hidden" }}
      onClick={(e) => e.stopPropagation()}
    >
      <div className="bell-head">
        <b className="grow">{t("bell.title")}</b>
        <button className="linkbtn accent" onClick={() => void markAllSeen()} disabled={unseen === 0}>{t("inbox.markall")}</button>
        <button className="iconbtn small quiet" title={t("bell.settings")} aria-label={t("bell.settings")} onClick={() => { navigate(pathFor("settings", "notifications")); onClose(); }}>
          <Icon name="settings" size={16} />
        </button>
      </div>
      <div className="chips bell-chips">
        <button className="chip select" aria-pressed={chip === "all"} onClick={() => setChip("all")}>{t("common.all")}</button>
        <button className="chip select" aria-pressed={chip === "needs"} onClick={() => setChip("needs")}>{t("centre.needs")}{needs > 0 ? ` · ${needs}` : ""}</button>
        <button className="chip select" aria-pressed={chip === "projects"} onClick={() => setChip("projects")}>{t("centre.projects")}</button>
      </div>
      <div className="bell-body">
        {chip !== "projects" && <NeedsYou names={names} onOpened={onClose} empty={chip === "needs" ? <div className="bell-empty">{t("centre.needs.empty")}</div> : null} />}
        {chip !== "needs" && <NotificationList groups={groups} row={row} />}
        {chip !== "needs" && feed.data && groups.length === 0 && needs === 0 && <div className="bell-empty">{t("bell.empty")}</div>}
      </div>
      <div className="bell-foot">
        <button className="btn small ghost" onClick={() => { navigate(pathFor("inbox")); onClose(); }}>{t("bell.showall")}</button>
      </div>
    </div>,
    document.body,
  );
}
