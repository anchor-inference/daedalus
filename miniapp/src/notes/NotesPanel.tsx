// The Notes tab of the right panel: a chat's notes and, when the chat is in a real project, the
// project's beside them under two sub-tabs. The listing comes in one read with both scopes and their
// files; every change made here goes into it at once and the host's `notes.changed` brings in the
// changes made elsewhere (another window, the phone, the agent).

import "./notes.css";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useEvent, useStreamUp } from "../events";
import { Icon } from "../icons";
import { plural, t } from "../i18n";
import { MediaViewer, type ViewerItem } from "../media";
import { downloadHref, FilePreview, type PreviewSource } from "../preview";
import { peek, prime, useQuery } from "../store";
import { confirmDialog, toast as undoToast, type MenuItem } from "../ui/dialogs";
import { errorText } from "../ui";
import { notesApi, referencedBy, seedFileUrl } from "./api";
import { attachToMessage, copyMarkdown, downloadNote } from "./export";
import { FilesShelf } from "./FilesShelf";
import { NoteCard } from "./NoteCard";
import { NoteEditor, type EditorTarget, type PickGroup } from "./NoteEditor";
import {
  fileBase, fileIndex, isImage, matches, notesKey, ordered, readMode, readSubtab, reference, rememberSubtab, shownTitle,
  type Note, type NoteFile, type NoteScope, type NotesListing, type ScopeListing,
} from "./model";

export type NotesPanelProps = {
  sessionId: string;
  /** The chat's project when it is a real one; a plain chat's scratch project gives no Project tab. */
  project: { id: string; name: string } | null;
  phone: boolean;
  toast: (text: string) => void;
};

const EMPTY: ScopeListing = { notes: [], files: [] };
const REFRESH_MS = 250;

export function NotesPanel({ sessionId, project, phone, toast }: NotesPanelProps) {
  const key = notesKey(sessionId);
  const query = useQuery<NotesListing>(key, { staleMs: 2000 });
  // A listing is only taken as one when it has the chat's scope: a host older or newer than this app,
  // or a stand-in that answers every route alike, must leave the tab empty rather than break the chat.
  const listing = query.data?.chat?.notes ? query.data : undefined;
  const projectScope = project && listing?.project_scope ? listing.project_scope : null;
  const hasProject = !!project;
  const [scope, setScope] = useState<NoteScope>(() => (hasProject ? readSubtab() : "chat"));
  const shownScope: NoteScope = hasProject ? scope : "chat";
  const [search, setSearch] = useState("");
  const [shelf, setShelf] = useState(false);
  const [editor, setEditor] = useState<EditorTarget | null>(null);
  const [leaving, setLeaving] = useState<Set<string>>(new Set());
  const [uploading, setUploading] = useState<{ key: number; name: string; progress: number }[]>([]);
  const [dropping, setDropping] = useState(false);
  const [viewer, setViewer] = useState<{ items: ViewerItem[]; index: number } | null>(null);
  const [preview, setPreview] = useState<PreviewSource | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const wide = !phone && typeof window !== "undefined" && window.innerWidth >= 1100;

  // ── the listing, kept fresh ─────────────────────────────────────────────────────────────────
  const timer = useRef<number | undefined>(undefined);
  const refreshSoon = useCallback(() => {
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => void query.refresh(), REFRESH_MS);
  }, [query.refresh]); // eslint-disable-line react-hooks/exhaustive-deps
  useEvent(["notes.changed"], (event) => {
    if (event.session_id === sessionId || (project && event.project_id === project.id)) refreshSoon();
  }, [sessionId, project?.id, refreshSoon]);
  // The event is live only: a stream that dropped and came back may have missed one.
  const up = useStreamUp();
  const wasUp = useRef(up);
  useEffect(() => {
    if (up && !wasUp.current) refreshSoon();
    wasUp.current = up;
  }, [up, refreshSoon]);
  useEffect(() => () => window.clearTimeout(timer.current), []);

  /** Put a note as the host now has it into the listing, under its scope, or take it out. */
  const put = useCallback((note: Note, gone = false) => {
    const now = peek<NotesListing>(key);
    if (!now) return;
    const place = (list: ScopeListing | null, kind: NoteScope): ScopeListing | null => {
      if (!list) return list;
      const rest = list.notes.filter((n) => n.id !== note.id);
      return { ...list, notes: !gone && note.scope === kind && !note.deleted_at ? [...rest, note] : rest };
    };
    prime<NotesListing>(key, { ...now, chat: place(now.chat, "chat") as ScopeListing, project_scope: place(now.project_scope, "project") });
  }, [key]);
  const putFiles = useCallback((made: NoteFile[], gone = false) => {
    const now = peek<NotesListing>(key);
    if (!now) return;
    const place = (list: ScopeListing | null, kind: NoteScope): ScopeListing | null => {
      if (!list) return list;
      const ids = new Set(made.map((f) => f.id));
      const rest = list.files.filter((f) => !ids.has(f.id));
      return { ...list, files: gone ? rest : [...made.filter((f) => f.scope === kind), ...rest] };
    };
    prime<NotesListing>(key, { ...now, chat: place(now.chat, "chat") as ScopeListing, project_scope: place(now.project_scope, "project") });
  }, [key]);

  const chat = listing?.chat ?? EMPTY;
  const current = shownScope === "project" ? projectScope ?? EMPTY : chat;
  // A chat note sees its project's files too; a project note only the project's.
  const chatFiles = useMemo(() => fileIndex(projectScope?.files, chat.files), [projectScope?.files, chat.files]);
  const projectFiles = useMemo(() => fileIndex(projectScope?.files), [projectScope?.files]);
  const filesFor = useCallback((kind: NoteScope) => (kind === "chat" ? chatFiles : projectFiles), [chatFiles, projectFiles]);
  const visible = useMemo(() => ordered(current.notes).filter((n) => matches(n, search, filesFor(shownScope))), [current.notes, search, filesFor, shownScope]);
  const countOf = (list: ScopeListing | null, kind: NoteScope) => (search ? (list?.notes ?? []).filter((n) => matches(n, search, filesFor(kind))).length : list?.notes.length ?? 0);

  const pickScope = (next: NoteScope) => {
    setScope(next);
    rememberSubtab(next);
  };

  // ── a link to a note's heading opens it ─────────────────────────────────────────────────────
  useEffect(() => {
    if (!listing) return;
    const params = new URLSearchParams(window.location.search);
    const wanted = params.get("note");
    if (!wanted) return;
    const found = [...listing.chat.notes, ...(listing.project_scope?.notes ?? [])].find((n) => n.id === wanted);
    params.delete("note");
    window.history.replaceState(window.history.state, "", `${window.location.pathname}${params.size ? `?${params}` : ""}${window.location.hash}`);
    if (!found) return;
    if (hasProject) pickScope(found.scope);
    setEditor({ noteId: found.id, scope: found.scope, mode: "read", opened: Date.now(), heading: decodeURIComponent(window.location.hash.slice(1)) || undefined });
  }, [!!listing]); // eslint-disable-line react-hooks/exhaustive-deps -- once the listing is here

  // ── actions on a note ───────────────────────────────────────────────────────────────────────
  const open = useCallback((note: Note) => setEditor({ noteId: note.id, scope: note.scope, mode: note.body.trim() ? "read" : readMode(wide), opened: Date.now() }), [wide]);
  const create = (template?: { title: string; body: string }) => setEditor({ noteId: null, scope: shownScope, mode: readMode(wide), opened: Date.now(), template });
  const pin = useCallback((note: Note) => {
    put({ ...note, pinned: !note.pinned });
    notesApi.meta(note.id, { pinned: !note.pinned }).then((n) => put(n), (e) => {
      put(note);
      toast(errorText(e));
    });
  }, [put, toast]);
  const remove = useCallback((note: Note) => {
    setEditor((now) => (now?.noteId === note.id ? null : now));
    setLeaving((now) => new Set(now).add(note.id));
    window.setTimeout(() => {
      put(note, true);
      setLeaving((now) => {
        const next = new Set(now);
        next.delete(note.id);
        return next;
      });
    }, 200);
    notesApi.remove(note.id).then(
      () => undoToast(t("notes.deleted", { title: shownTitle(note) || t("notes.untitled") }), {
        undo: () => void notesApi.restore(note.id).then((n) => put(n), (e) => toast(errorText(e))),
      }),
      (e) => {
        put(note);
        toast(errorText(e));
      },
    );
  }, [put, toast]);
  const act = useCallback(async (action: "copy" | "download" | "attach" | "duplicate" | "move" | "delete" | "pin", note: Note) => {
    const files = filesFor(note.scope);
    try {
      if (action === "pin") return pin(note);
      if (action === "delete") return remove(note);
      if (action === "copy") {
        await copyMarkdown(note);
        return toast(t("notes.copied"));
      }
      if (action === "download") {
        await downloadNote(note, files, t("notes.untitled"));
        return;
      }
      if (action === "attach") {
        const where = await attachToMessage(sessionId, note, files, t("notes.untitled"));
        setEditor(null);
        return toast(t(where === "composer" ? "notes.attached" : "notes.attached.draft"));
      }
      if (action === "duplicate") {
        const copy = await notesApi.duplicate(note.id, t("notes.copy.title", { title: shownTitle(note) || t("notes.untitled") }));
        put(copy);
        return toast(t("notes.duplicated"));
      }
      if (action === "move") {
        const target: NoteScope = note.scope === "chat" ? "project" : "chat";
        const moved = await notesApi.move(note.id, sessionId, target);
        put(moved);
        refreshSoon();
        return toast(t(target === "project" ? "notes.moved.project" : "notes.moved.chat"));
      }
    } catch (e) {
      toast(errorText(e));
    }
  }, [filesFor, pin, remove, put, sessionId, toast, refreshSoon]);

  const menuFor = useCallback((note: Note, of: Note[]): MenuItem[] => {
    const group = of.filter((n) => n.pinned === note.pinned);
    const inGroup = group.findIndex((n) => n.id === note.id);
    return [
      { label: t("notes.open.short"), icon: "eye", onSelect: () => open(note) },
      { label: t(note.pinned ? "notes.unpin" : "notes.pin"), icon: "pushpin", onSelect: () => pin(note) },
      "-",
      { label: t("notes.copy"), icon: "copy", onSelect: () => void act("copy", note) },
      { label: t("notes.download"), icon: "download", onSelect: () => void act("download", note) },
      { label: t("notes.attach"), icon: "send", onSelect: () => void act("attach", note) },
      { label: t("notes.duplicate"), icon: "copy", onSelect: () => void act("duplicate", note) },
      ...(hasProject ? [{ label: t("notes.move", { where: note.scope === "chat" ? t("notes.scope.project") : t("notes.scope.chat") }), icon: "folder" as const, onSelect: () => void act("move", note) }] : []),
      ...(!search && group.length > 1 ? ["-" as const,
        { label: t("notes.up"), icon: "up" as const, disabled: inGroup <= 0, onSelect: () => reorder(note, group[inGroup - 1]?.id, "before") },
        { label: t("notes.down"), icon: "down" as const, disabled: inGroup >= group.length - 1, onSelect: () => reorder(note, group[inGroup + 1]?.id, "after") },
      ] : []),
      "-",
      { label: t("notes.delete"), icon: "trash", danger: true, onSelect: () => remove(note) },
    ];
  }, [open, pin, act, remove, hasProject, search]); // eslint-disable-line react-hooks/exhaustive-deps -- reorder reads the listing

  // ── order: drag on a desktop, Move up/down anywhere ─────────────────────────────────────────
  const commitOrder = (ids: string[]) => {
    const list = ordered(current.notes);
    const byId = new Map(list.map((n) => [n.id, n]));
    const next = ids.map((id, index) => ({ ...(byId.get(id) as Note), position: index }));
    const now = peek<NotesListing>(key);
    if (now) {
      const patch = (l: ScopeListing | null, kind: NoteScope) => (l && kind === shownScope ? { ...l, notes: next } : l);
      prime<NotesListing>(key, { ...now, chat: patch(now.chat, "chat") as ScopeListing, project_scope: patch(now.project_scope, "project") });
    }
    notesApi.order(sessionId, shownScope, ids).catch((e) => {
      toast(errorText(e));
      refreshSoon();
    });
  };
  const reorder = (note: Note, other: string | undefined, where: "before" | "after") => {
    if (!other) return;
    const ids = ordered(current.notes).map((n) => n.id).filter((id) => id !== note.id);
    const at = ids.indexOf(other);
    ids.splice(where === "before" ? at : at + 1, 0, note.id);
    commitOrder(ids);
  };
  const list = useRef<HTMLDivElement>(null);
  const [dragOrder, setDragOrder] = useState<string[] | null>(null);
  const drag = useRef<{ id: string; startY: number; dy: number; origTop: number; centers: Map<string, number>; group: string[]; others: string[] } | null>(null);
  const grab = useCallback((note: Note, e: React.PointerEvent<HTMLElement>) => {
    if (e.button !== 0 || search) return;
    e.preventDefault();
    const cards = Array.from(list.current?.querySelectorAll<HTMLElement>("[data-note]") ?? []);
    const centers = new Map(cards.map((el) => [el.dataset.note as string, el.offsetTop + el.offsetHeight / 2]));
    const all = ordered(current.notes);
    const group = all.filter((n) => n.pinned === note.pinned).map((n) => n.id);
    const el = cards.find((c) => c.dataset.note === note.id);
    if (!el) return;
    drag.current = { id: note.id, startY: e.clientY, dy: 0, origTop: el.offsetTop, centers, group, others: all.filter((n) => n.pinned !== note.pinned).map((n) => n.id) };
    el.classList.add("dragging");
    const move = (ev: PointerEvent) => {
      const d = drag.current;
      if (!d) return;
      d.dy = ev.clientY - d.startY;
      const center = (d.centers.get(d.id) ?? 0) + d.dy;
      const rest = d.group.filter((id) => id !== d.id);
      const index = rest.filter((id) => (d.centers.get(id) ?? 0) < center).length;
      const nextGroup = [...rest.slice(0, index), d.id, ...rest.slice(index)];
      const pinnedFirst = note.pinned ? [...nextGroup, ...d.others] : [...d.others, ...nextGroup];
      setDragOrder((now) => (now && now.join() === pinnedFirst.join() ? now : pinnedFirst));
      placeDragged();
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      window.removeEventListener("pointercancel", up);
      const d = drag.current;
      drag.current = null;
      const card = list.current?.querySelector<HTMLElement>(`[data-note="${note.id}"]`);
      card?.classList.remove("dragging");
      if (card) card.style.transform = "";
      setDragOrder((final) => {
        if (final && d && final.join() !== all.map((n) => n.id).join()) commitOrder(final);
        return null;
      });
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", up);
  }, [current.notes, search]); // eslint-disable-line react-hooks/exhaustive-deps -- commitOrder reads the listing
  const placeDragged = () => {
    const d = drag.current;
    const card = d && list.current?.querySelector<HTMLElement>(`[data-note="${d.id}"]`);
    if (d && card) card.style.transform = `translateY(${d.dy - (card.offsetTop - d.origTop)}px)`;
  };
  const shown = dragOrder ? dragOrder.map((id) => visible.find((n) => n.id === id)).filter((n): n is Note => !!n) : visible;

  // A card that moved (a new order, a pin, a note above it gone) slides from where it was.
  const tops = useRef(new Map<string, number>());
  useLayoutEffect(() => {
    placeDragged();
    const box = list.current;
    if (!box) return;
    const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches || document.documentElement.dataset.motion === "reduce";
    const next = new Map<string, number>();
    box.querySelectorAll<HTMLElement>("[data-note]").forEach((el) => {
      const id = el.dataset.note as string;
      next.set(id, el.offsetTop);
      const before = tops.current.get(id);
      if (!reduce && before !== undefined && before !== el.offsetTop && drag.current?.id !== id) {
        el.animate?.([{ transform: `translateY(${before - el.offsetTop}px)` }, { transform: "translateY(0)" }], { duration: 240, easing: "cubic-bezier(.2,.9,.3,1)" });
      }
    });
    tops.current = next;
  });

  // ── files ───────────────────────────────────────────────────────────────────────────────────
  const uploadSeq = useRef(0);
  const addFiles = async (incoming: File[]) => {
    if (!incoming.length) return;
    setShelf(true);
    await Promise.all(incoming.map(async (file) => {
      const job = { key: ++uploadSeq.current, name: file.name, progress: 0 };
      setUploading((now) => [...now, job]);
      try {
        const { files: made } = await notesApi.upload(sessionId, shownScope, [file], (progress) => setUploading((now) => now.map((u) => (u.key === job.key ? { ...u, progress } : u))));
        seedFileUrl(made[0].id, file);
        putFiles(made);
      } catch (e) {
        toast(t("notes.upload.failed", { name: file.name, error: errorText(e) }));
      } finally {
        setUploading((now) => now.filter((u) => u.key !== job.key));
      }
    }));
  };
  const openFile = useCallback((file: NoteFile) => {
    if (isImage(file)) {
      const pictures = current.files.filter(isImage);
      return setViewer({ index: Math.max(0, pictures.findIndex((f) => f.id === file.id)), items: pictures.map((f) => ({ key: f.id, kind: "image" as const, name: f.name, filename: f.name, source: { base: fileBase(f.id), path: f.name, mime: f.mime }, download: downloadHref(fileBase(f.id), f.name) })) });
    }
    setPreview({ base: fileBase(file.id), path: file.name, mime: file.mime });
  }, [current.files]);
  const copyRef = (file: NoteFile) => navigator.clipboard.writeText(reference(file)).then(() => toast(t("notes.files.copied")), (e) => toast(errorText(e)));
  const removeFile = async (file: NoteFile) => {
    try {
      await notesApi.removeFile(file.id);
      putFiles([file], true);
      toast(t("notes.files.removed", { name: file.name }));
    } catch (e) {
      const users = referencedBy(e);
      if (!users) return toast(errorText(e));
      const titles = users.map((n) => `«${n.title || t("notes.untitled")}»`).join(", ");
      if (!(await confirmDialog({ title: t("notes.files.inuse", { name: file.name }), body: plural("notes.files.inuse.body", users.length, { notes: titles }), action: t("notes.files.remove"), danger: true }))) return;
      try {
        await notesApi.removeFile(file.id, true);
        putFiles([file], true);
        toast(t("notes.files.removed", { name: file.name }));
      } catch (again) {
        toast(errorText(again));
      }
    }
  };

  const headingLink = useCallback((noteId: string, slug: string) => {
    const params = new URLSearchParams(window.location.search);
    params.set("panel", "notes");
    params.set("note", noteId);
    const href = `${window.location.origin}${window.location.pathname}?${params}#${encodeURIComponent(slug)}`;
    navigator.clipboard.writeText(href).then(() => toast(t("notes.heading.copied")), (e) => toast(errorText(e)));
  }, [toast]);

  // ── what is drawn ───────────────────────────────────────────────────────────────────────────
  const editing = editor ? (editor.noteId ? [...chat.notes, ...(projectScope?.notes ?? [])].find((n) => n.id === editor.noteId) ?? null : null) : null;
  const editorFiles = editor ? filesFor(editor.scope) : chatFiles;
  const pickable: PickGroup[] = useMemo(() => {
    if (!editor) return [];
    const groups: PickGroup[] = [];
    if (editor.scope === "chat") groups.push({ label: t("notes.scope.chat"), files: chat.files });
    if (projectScope) groups.push({ label: project ? t("notes.scope.projectnamed", { name: project.name }) : t("notes.scope.project"), files: projectScope.files });
    return groups.filter((g) => g.files.length);
  }, [editor?.scope, chat.files, projectScope?.files, project?.name]); // eslint-disable-line react-hooks/exhaustive-deps
  const scopeLabel = (kind: NoteScope) => (kind === "project" && project ? t("notes.scope.projectnamed", { name: project.name }) : t("notes.scope.chatnote"));

  const empty = !!listing && current.notes.length === 0;
  return (
    <div
      className={`notes-panel ${phone ? "phone" : ""}`}
      onDragEnter={(e) => { if (e.dataTransfer.types.includes("Files")) setDropping(true); }}
      onDragOver={(e) => { if (e.dataTransfer.types.includes("Files")) e.preventDefault(); }}
      onDragLeave={(e) => { if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setDropping(false); }}
      onDrop={(e) => {
        if (!e.dataTransfer.files.length) return;
        e.preventDefault();
        setDropping(false);
        void addFiles(Array.from(e.dataTransfer.files));
      }}
    >
      {hasProject && (
        <div className="notes-subtabs" role="tablist" aria-label={t("notes.scopes")} style={{ "--at": shownScope === "project" ? 0 : 1 } as React.CSSProperties}>
          <i className="notes-subtab-pill" aria-hidden="true" />
          {(["project", "chat"] as const).map((kind) => (
            <button key={kind} type="button" role="tab" data-scope={kind} aria-selected={shownScope === kind} className={`notes-subtab ${shownScope === kind ? "on" : ""}`} onClick={() => pickScope(kind)}>
              <Icon name={kind === "project" ? "folder" : "chat"} size={14} />
              <span className="notes-subtab-name">{kind === "project" ? project!.name : t("notes.scope.chat")}</span>
              <span className="notes-subtab-count">{countOf(kind === "project" ? projectScope : chat, kind)}</span>
            </button>
          ))}
        </div>
      )}
      <div className="notes-bar">
        <label className="notes-search">
          <Icon name="search" size={14} />
          <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder={t("notes.search")} aria-label={t("notes.search")} onKeyDown={(e) => { if (e.key === "Escape" && search) { e.stopPropagation(); setSearch(""); } }} />
          {search && <button type="button" className="notes-search-clear" onClick={() => setSearch("")} aria-label={t("notes.search.clear")} title={t("notes.search.clear")}><Icon name="close" size={12} /></button>}
        </label>
        <button type="button" className={`iconbtn small notes-shelf-toggle ${shelf ? "on" : ""}`} aria-pressed={shelf} onClick={() => setShelf((v) => !v)} aria-label={t("notes.files")} title={t("notes.files")}>
          <Icon name="attach" size={16} />
          {current.files.length > 0 && <span className="notes-shelf-count">{current.files.length}</span>}
        </button>
        <button type="button" className="btn primary small notes-new" onClick={() => create()}><Icon name="plus" size={14} /><span>{t("notes.new")}</span></button>
      </div>
      {shelf && <FilesShelf files={current.files} uploading={uploading} onAdd={() => fileInput.current?.click()} onOpen={openFile} onCopy={copyRef} onRemove={(f) => void removeFile(f)} />}
      <div key={shownScope} className="notes-scope">
        {!listing && query.error && <div className="notes-error">{query.error}<button type="button" className="btn small" onClick={() => void query.refresh()}>{t("common.retry")}</button></div>}
        {!listing && !query.error && <div className="notes-loading" aria-busy="true"><i /><i /><i /></div>}
        {empty && <EmptyState scope={shownScope} projectName={project?.name ?? ""} onCreate={create} />}
        {!empty && listing && visible.length === 0 && <div className="notes-none">{t("notes.search.none", { query: search })}</div>}
        {!empty && visible.length > 0 && (
          <div ref={list} className="notes-list" role="list">
            {shown.map((note, i) => (
              <NoteCard
                key={note.id}
                note={note}
                index={i}
                files={filesFor(note.scope)}
                query={search}
                menu={menuFor(note, visible)}
                draggable={!phone && !search}
                leaving={leaving.has(note.id)}
                onOpen={open}
                onPin={pin}
                onGrab={grab}
              />
            ))}
          </div>
        )}
      </div>
      {dropping && <div className="notes-drop" aria-hidden="true"><Icon name="attach" size={22} /><span>{t("notes.drop.panel", { where: scopeLabel(shownScope) })}</span></div>}
      <input ref={fileInput} type="file" multiple hidden onChange={(e) => { void addFiles(Array.from(e.target.files ?? [])); e.target.value = ""; }} />
      {editor && (
        <NoteEditor
          key={editor.opened}
          sessionId={sessionId}
          target={editor}
          live={editing}
          files={editorFiles}
          pickable={pickable}
          wide={wide}
          scopeLabel={scopeLabel(editor.scope)}
          moveTo={hasProject ? { scope: editor.scope === "chat" ? "project" : "chat", label: editor.scope === "chat" ? t("notes.scope.project") : t("notes.scope.chat") } : null}
          onClose={() => setEditor(null)}
          onSaved={(note) => {
            put(note);
            if (editor.noteId === null) setEditor((now) => (now && now.noteId === null ? { ...now, noteId: note.id } : now));
          }}
          onUploaded={(made) => putFiles(made)}
          onAction={(action, note) => void act(action, note)}
          onHeadingLink={headingLink}
          toast={toast}
        />
      )}
      {viewer && <MediaViewer items={viewer.items} start={viewer.index} onClose={() => setViewer(null)} />}
      {preview && <FilePreview src={preview} onClose={() => setPreview(null)} />}
    </div>
  );
}

/** What a scope without notes says: what notes are for here, and three ways to start one. */
function EmptyState({ scope, projectName, onCreate }: { scope: NoteScope; projectName: string; onCreate: (template?: { title: string; body: string }) => void }) {
  const templates = (["checklist", "meeting", "decision"] as const).map((id) => ({ id, title: t(`notes.template.${id}`), body: t(`notes.template.${id}.body`) }));
  return (
    <div className="notes-empty">
      <svg className="notes-empty-art" viewBox="0 0 120 84" aria-hidden="true">
        <rect className="sheet back" x="30" y="6" width="62" height="72" rx="8" />
        <rect className="sheet front" x="20" y="12" width="62" height="66" rx="8" />
        <path className="lines" d="M31 30h34M31 40h40M31 50h26" />
        <path className="tick" d="M31 62l4 4 8-9" />
        <circle className="dot" cx="94" cy="20" r="5" />
      </svg>
      <h4>{scope === "project" ? t("notes.empty.project", { name: projectName }) : t("notes.empty.chat")}</h4>
      <p>{t(scope === "project" ? "notes.empty.project.body" : "notes.empty.chat.body")}</p>
      <button type="button" className="btn primary" onClick={() => onCreate()}><Icon name="plus" size={14} />{t("notes.new")}</button>
      <div className="notes-templates" aria-label={t("notes.templates")}>
        {templates.map((tpl) => (
          <button key={tpl.id} type="button" className="chip" onClick={() => onCreate({ title: tpl.title, body: tpl.body })}>{tpl.title}</button>
        ))}
      </div>
      <p className="notes-tip">{t("notes.tip")}</p>
    </div>
  );
}
