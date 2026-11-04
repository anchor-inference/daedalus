// The note editor: a large dialog (the whole screen on a phone) with the Markdown source, the same
// drawing the answers get beside it or instead of it, and no Save button.
//
// Saving: every keystroke writes a draft on this device and schedules a save 700 ms later; blur, close
// and a hidden tab save at once. A save carries the version the text was typed on, and a refusal (the
// agent or another window saved in between) opens the conflict prompt instead of overwriting anything.
// Closing never discards: it saves first, and a save that cannot reach the host leaves the draft here
// and says so. A new note is created on its first words, so an untouched one leaves no trace.

import { useCallback, useDeferredValue, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { nameIncoming, pastedFiles } from "../attachfiles";
import { relTime } from "../format";
import { Icon, type IconName } from "../icons";
import { plural, t } from "../i18n";
import { MediaViewer, type ViewerItem } from "../media";
import { downloadHref, FilePreview, type PreviewSource } from "../preview";
import { Overlay, OverflowMenu, Popover, confirmDialog, trapDialogTab, useLayer, type MenuItem } from "../ui/dialogs";
import { errorText } from "../ui";
import { conflictOf, fileUrl, notesApi, seedFileUrl } from "./api";
import {
  COLORS, continueList, draftKey, dropDraft, fileBase, indent, insertBlock, insertText, isImage, lineDiff, linePrefix, link, readDraft,
  readingMinutes, reference, rememberMode, toggleTask, trigger, wordCount, wrap, writeDraft,
  type Edit, type EditorMode, type LineKind, type Note, type NoteFile, type NoteScope,
} from "./model";
import { NoteView, scrollToHeading } from "./NoteView";
import { fileFamily } from "../md";
import { TINT_MAX, tint } from "./highlight";
import { RawHtml } from "../rawhtml";
import { applyEdit, caretPoint } from "./textarea";

/** What the editor was opened on. `opened` names the opening: the editor keeps its state across the
 *  moment a new note gets its id, which a key made of the id would throw away mid-sentence. */
export type EditorTarget = { noteId: string | null; scope: NoteScope; mode: EditorMode; opened: number; heading?: string; template?: { title: string; body: string } };
export type PickGroup = { label: string; files: NoteFile[] };

export type NoteEditorProps = {
  sessionId: string;
  target: EditorTarget;
  /** The note as the listing has it now; null for a new note, or one deleted meanwhile. */
  live: Note | null;
  files: Map<string, NoteFile>;
  pickable: PickGroup[];
  wide: boolean;
  scopeLabel: string;
  /** The other scope this note could move to, with its name, or null. */
  moveTo: { scope: NoteScope; label: string } | null;
  onClose: () => void;
  onSaved: (note: Note) => void;
  onUploaded: (files: NoteFile[]) => void;
  onAction: (action: "copy" | "download" | "attach" | "duplicate" | "move" | "delete" | "pin", note: Note) => void;
  onHeadingLink: (noteId: string, slug: string) => void;
  toast: (text: string) => void;
};

type Status = "idle" | "dirty" | "saving" | "saved" | "offline" | "conflict" | "gone";
type Upload = { key: number; name: string; progress: number };
type Popup = { kind: "file" | "slash"; from: number; query: string; index: number };

const SAVE_MS = 700;
const RETRY_MS = 5000;
let uploadSeq = 0;

const isMac = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
const MOD = isMac ? "⌘" : "Ctrl";

export function NoteEditor(props: NoteEditorProps) {
  const { sessionId, target, live, files, wide, toast } = props;
  const scope = target.scope;

  // ── the text, and what the host holds of it ─────────────────────────────────────────────────
  const initial = useMemo(() => startingPoint(sessionId, target, live), []); // eslint-disable-line react-hooks/exhaustive-deps -- read once, on open
  const [title, setTitle] = useState(initial.title);
  const [body, setBody] = useState(initial.body);
  const [noteId, setNoteId] = useState<string | null>(target.noteId);
  const [status, setStatus] = useState<Status>(initial.conflict ? "conflict" : initial.dirty ? "dirty" : "idle");
  const [conflict, setConflict] = useState<Note | null>(initial.conflict);
  const [remote, setRemote] = useState<Note | null>(null);
  const [flash, setFlash] = useState<"agent" | "operator" | null>(null);
  const [mode, setMode] = useState<EditorMode>(!wide && target.mode === "split" ? "write" : target.mode);
  const [uploads, setUploads] = useState<Upload[]>([]);
  const [popup, setPopup] = useState<Popup | null>(null);
  const [dropping, setDropping] = useState(false);
  const [closing, setClosing] = useState(false);
  const [viewer, setViewer] = useState<{ items: ViewerItem[]; index: number } | null>(null);
  const [preview, setPreview] = useState<PreviewSource | null>(null);
  const [colorAnchor, setColorAnchor] = useState<HTMLElement | null>(null);

  const latest = useRef({ title, body });
  latest.current = { title, body };
  const saved = useRef(initial.saved);
  const base = useRef<number | null>(initial.base);
  const idRef = useRef<string | null>(target.noteId);
  const conflictRef = useRef<Note | null>(initial.conflict);
  conflictRef.current = conflict;
  const timer = useRef<number | undefined>(undefined);
  const inflight = useRef<Promise<void> | null>(null);
  const field = useRef<HTMLTextAreaElement>(null);
  const titleField = useRef<HTMLInputElement>(null);
  const dialog = useRef<HTMLDivElement>(null);
  const reading = useRef<HTMLDivElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const keyOf = () => draftKey(idRef.current, sessionId, scope);

  const dirty = () => latest.current.title !== saved.current.title || latest.current.body !== saved.current.body;

  const save = useCallback(async (): Promise<boolean> => {
    window.clearTimeout(timer.current);
    while (inflight.current) await inflight.current;
    if (conflictRef.current) return false;
    const sent = { ...latest.current };
    if (idRef.current === null && !sent.title.trim() && !sent.body.trim()) return true;
    if (idRef.current !== null && !dirty()) return true;
    let ok = false;
    const run = (async () => {
      setStatus("saving");
      try {
        const note = idRef.current === null
          ? await notesApi.create(sessionId, { scope, title: sent.title, body: sent.body })
          : await notesApi.save(idRef.current, { title: sent.title, body: sent.body, version: base.current ?? 0 });
        if (idRef.current === null) {
          dropDraft(draftKey(null, sessionId, scope));
          idRef.current = note.id;
          setNoteId(note.id);
        }
        base.current = note.version;
        saved.current = sent;
        props.onSaved(note);
        ok = true;
        if (dirty()) {
          setStatus("dirty");
          writeDraft(keyOf(), { ...latest.current, base: note.version, at: Date.now() });
          timer.current = window.setTimeout(() => void save(), SAVE_MS);
        } else {
          dropDraft(keyOf());
          setStatus("saved");
        }
      } catch (e) {
        const theirs = conflictOf(e);
        if (theirs) {
          setConflict(theirs);
          setStatus("conflict");
        } else {
          setStatus("offline");
          // The words are on this device already; the host is asked again until it answers.
          timer.current = window.setTimeout(() => void save(), RETRY_MS);
          if (!navigator.onLine) return;
          toast(errorText(e));
        }
      }
    })();
    inflight.current = run;
    await run;
    inflight.current = null;
    return ok;
  }, [sessionId, scope]); // eslint-disable-line react-hooks/exhaustive-deps -- reads the rest through refs

  const changed = (next: { title?: string; body?: string }, now = false) => {
    const merged = { ...latest.current, ...next };
    latest.current = merged;
    if (next.title !== undefined) setTitle(next.title);
    if (next.body !== undefined) setBody(next.body);
    writeDraft(keyOf(), { ...merged, base: base.current ?? 0, at: Date.now() });
    if (conflictRef.current) return;
    setStatus("dirty");
    window.clearTimeout(timer.current);
    if (now) void save();
    else timer.current = window.setTimeout(() => void save(), SAVE_MS);
  };

  // A draft found on open that was typed on the version the host still has is simply saved now.
  useEffect(() => {
    if (initial.dirty && !initial.conflict) {
      toast(t("notes.draft.restored"));
      void save();
    }
    return () => {
      window.clearTimeout(timer.current);
      // Taken off the screen some other way than closing (the chat changed, the panel shut): what is
      // unsaved still goes to the host, and the draft stays until it is there.
      if (dirty() && !conflictRef.current) void save();
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps -- once, on open

  // A hidden tab or a closed window saves what is there; the draft covers what the save cannot.
  useEffect(() => {
    const away = () => {
      if (document.visibilityState === "hidden") void save();
    };
    document.addEventListener("visibilitychange", away);
    window.addEventListener("pagehide", away);
    return () => {
      document.removeEventListener("visibilitychange", away);
      window.removeEventListener("pagehide", away);
    };
  }, [save]);

  // ── the note changing elsewhere ─────────────────────────────────────────────────────────────
  const liveVersion = live?.version ?? null;
  const wasLive = useRef(live !== null);
  useEffect(() => {
    if (live) wasLive.current = true;
    if (!live) {
      if (wasLive.current && idRef.current) setStatus("gone");
      return;
    }
    if (base.current === null || live.version <= base.current || inflight.current) return;
    if (!dirty()) {
      // Nothing of ours is unsaved: take the new words in place, keeping the caret where it was.
      const caret = field.current ? [field.current.selectionStart, field.current.selectionEnd] : null;
      setTitle(live.title);
      setBody(live.body);
      latest.current = { title: live.title, body: live.body };
      saved.current = { title: live.title, body: live.body };
      base.current = live.version;
      setRemote(null);
      setFlash(live.updated_by);
      if (caret) requestAnimationFrame(() => field.current?.setSelectionRange(Math.min(caret[0], live.body.length), Math.min(caret[1], live.body.length)));
    } else setRemote(live);
  }, [liveVersion]); // eslint-disable-line react-hooks/exhaustive-deps -- the version is what moves
  useEffect(() => {
    if (!flash) return;
    const done = window.setTimeout(() => setFlash(null), 2600);
    return () => window.clearTimeout(done);
  }, [flash]);

  // ── closing ─────────────────────────────────────────────────────────────────────────────────
  const closingRef = useRef(false);
  const requestClose = useCallback(async () => {
    if (closingRef.current) return;
    closingRef.current = true;
    try {
      const ok = await save();
      if (conflictRef.current) {
        if (!(await confirmDialog({ title: t("notes.close.conflict"), body: t("notes.close.conflict.body"), action: t("notes.close.later") }))) return;
      } else if (!ok && dirty()) {
        if (!(await confirmDialog({ title: t("notes.close.unsaved"), body: t("notes.close.unsaved.body"), action: t("notes.close.later") }))) return;
      }
      setClosing(true);
      window.setTimeout(props.onClose, matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 160);
    } finally {
      closingRef.current = false;
    }
  }, [save, props.onClose]); // eslint-disable-line react-hooks/exhaustive-deps -- dirty reads refs
  useLayer(() => void requestClose());

  // Focus: a new or empty note starts in its words (or its title), an open one on the reading view.
  useEffect(() => {
    if (mode === "read") dialog.current?.focus();
    else if (!initial.title && !initial.body) titleField.current?.focus();
    else field.current?.focus();
    const opener = document.activeElement as HTMLElement | null;
    return () => opener?.focus?.();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps -- once, on open

  // A link to a heading opens the note scrolled to it.
  useEffect(() => {
    if (target.heading) requestAnimationFrame(() => scrollToHeading(reading.current, target.heading!));
  }, [target.heading]);

  const pickMode = (next: EditorMode) => {
    setMode(next);
    rememberMode(next);
    if (next !== "read") requestAnimationFrame(() => field.current?.focus());
  };

  // ── text commands ───────────────────────────────────────────────────────────────────────────
  const apply = (make: (text: string, start: number, end: number) => Edit | null) => {
    const el = field.current;
    if (!el) return false;
    const edit = make(el.value, el.selectionStart, el.selectionEnd);
    if (!edit) return false;
    applyEdit(el, edit, (text) => changed({ body: text }));
    return true;
  };
  const prefix = (kind: LineKind) => apply((text, s, e) => linePrefix(text, s, e, kind));
  const block = (snippet: string, select?: [number, number]) => apply((text, s, e) => insertBlock(text, s, e, snippet, select));

  const tools: ({ id: string; icon: IconName; label: string; keys?: string; run: () => void } | "-")[] = [
    { id: "heading", icon: "heading", label: t("notes.tool.heading"), run: () => prefix("h2") },
    { id: "bold", icon: "bold", label: t("notes.tool.bold"), keys: `${MOD}+B`, run: () => apply((text, s, e) => wrap(text, s, e, "**", "**", t("notes.tool.bold.sample"))) },
    { id: "italic", icon: "italic", label: t("notes.tool.italic"), keys: `${MOD}+I`, run: () => apply((text, s, e) => wrap(text, s, e, "_", "_", t("notes.tool.italic.sample"))) },
    { id: "strike", icon: "strike", label: t("notes.tool.strike"), keys: `${MOD}+Shift+X`, run: () => apply((text, s, e) => wrap(text, s, e, "~~", "~~", t("notes.tool.strike.sample"))) },
    { id: "code", icon: "code", label: t("notes.tool.code"), keys: `${MOD}+E`, run: () => apply((text, s, e) => wrap(text, s, e, "`", "`", "code")) },
    "-",
    { id: "ul", icon: "list", label: t("notes.tool.ul"), keys: `${MOD}+Shift+8`, run: () => prefix("ul") },
    { id: "ol", icon: "olist", label: t("notes.tool.ol"), keys: `${MOD}+Shift+7`, run: () => prefix("ol") },
    { id: "task", icon: "tasks", label: t("notes.tool.task"), keys: `${MOD}+Shift+9`, run: () => prefix("task") },
    { id: "quote", icon: "quote", label: t("notes.tool.quote"), run: () => prefix("quote") },
    "-",
    { id: "link", icon: "link", label: t("notes.tool.link"), keys: `${MOD}+K`, run: () => apply((text, s, e) => link(text, s, e, t("notes.tool.link.sample"))) },
    { id: "table", icon: "table", label: t("notes.tool.table"), run: () => block(TABLE, [2, 8]) },
    { id: "codeblock", icon: "braces", label: t("notes.tool.codeblock"), run: () => block("```\n\n```", [4, 4]) },
    "-",
    { id: "upload", icon: "image", label: t("notes.tool.upload"), run: () => fileInput.current?.click() },
    { id: "ref", icon: "attach", label: t("notes.tool.ref"), keys: "[[", run: () => openPicker() },
  ];

  const openPicker = () => {
    const el = field.current;
    if (!el) return;
    if (mode === "read") pickMode("write");
    apply((text, s, e) => insertText(text, s, e, "[["));
    setPopup({ kind: "file", from: el.selectionStart - 2, query: "", index: 0 });
  };

  // ── uploads ─────────────────────────────────────────────────────────────────────────────────
  const upload = async (incoming: File[]) => {
    const list = nameIncoming(incoming);
    if (!list.length) return;
    if (mode === "read") pickMode("write");
    const el = field.current;
    const jobs = list.map((file) => ({ file, key: ++uploadSeq, token: `${file.type.startsWith("image/") ? "!" : ""}[${t("notes.uploading", { name: file.name.replace(/[[\]]/g, "") })}](uploading:${uploadSeq})` }));
    // The stand-ins go in at the caret at once, each on its own line when there are several.
    const joined = jobs.map((job) => job.token).join(jobs.length > 1 ? "\n" : "");
    if (el) applyEdit(el, insertText(el.value, el.selectionStart, el.selectionEnd, joined), (text) => changed({ body: text }));
    else changed({ body: `${latest.current.body}${latest.current.body ? "\n\n" : ""}${joined}` });
    setUploads((now) => [...now, ...jobs.map((job) => ({ key: job.key, name: job.file.name, progress: 0 }))]);
    await Promise.all(jobs.map(async (job) => {
      try {
        const { files: made } = await notesApi.upload(sessionId, scope, [job.file], (progress) => setUploads((now) => now.map((u) => (u.key === job.key ? { ...u, progress } : u))));
        const file = made[0];
        seedFileUrl(file.id, job.file);
        props.onUploaded(made);
        replaceToken(job.token, reference(file));
      } catch (e) {
        replaceToken(job.token, "");
        toast(t("notes.upload.failed", { name: job.file.name, error: errorText(e) }));
      } finally {
        setUploads((now) => now.filter((u) => u.key !== job.key));
      }
    }));
  };
  const replaceToken = (token: string, withText: string) => {
    const current = latest.current.body;
    const at = current.indexOf(token);
    if (at < 0) return;
    const next = current.slice(0, at) + withText + current.slice(at + token.length);
    const el = field.current;
    if (el && document.activeElement === el && el.value === current) {
      // Put in as typed, with the operator's caret moved by exactly what changed before it: they may
      // be several words further on by the time the upload answers.
      const shift = (n: number) => (n >= at + token.length ? n + withText.length - token.length : Math.min(n, at));
      applyEdit(el, { text: next, start: shift(el.selectionStart), end: shift(el.selectionEnd) }, (text) => changed({ body: text }));
      return;
    }
    changed({ body: next });
  };

  // ── the field's keys, paste and the menus at the caret ──────────────────────────────────────
  const syncPopup = (el: HTMLTextAreaElement) => {
    const found = trigger(el.value, el.selectionStart);
    if (!found || el.selectionStart !== el.selectionEnd) return setPopup(null);
    setPopup((now) => (now && now.kind === found.kind && now.from === found.from ? { ...now, query: found.query, index: now.query === found.query ? now.index : 0 } : { ...found, index: 0 }));
  };
  const slashCommands = slashCommandsFor({ prefix, block, apply, upload: () => fileInput.current?.click(), picker: () => openPicker() });
  const popupItems = usePopupItems(popup, props.pickable, slashCommands);
  const choose = (index: number) => {
    if (!popup) return;
    const item = popupItems[index];
    if (!item) return;
    const el = field.current;
    if (!el) return;
    const caret = el.selectionStart;
    setPopup(null);
    if (item.kind === "file") {
      applyEdit(el, insertText(el.value, popup.from, caret, reference(item.file)), (text) => changed({ body: text }));
      return;
    }
    if (item.kind === "upload") {
      applyEdit(el, insertText(el.value, popup.from, caret, ""), (text) => changed({ body: text }));
      fileInput.current?.click();
      return;
    }
    // A command takes the place of what was typed for it, then does its work at the caret.
    applyEdit(el, insertText(el.value, popup.from, caret, ""), (text) => changed({ body: text }));
    item.run();
  };
  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    const mod = isMac ? e.metaKey : e.ctrlKey;
    if (popup && popupItems.length) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        const step = e.key === "ArrowDown" ? 1 : popupItems.length - 1;
        setPopup({ ...popup, index: (popup.index + step) % popupItems.length });
        return;
      }
      if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault();
        choose(popup.index);
        return;
      }
    }
    if (mod && !e.altKey) {
      const key = e.key.toLowerCase();
      const run = (fn: () => unknown) => {
        e.preventDefault();
        fn();
      };
      if (key === "b" && !e.shiftKey) return run(() => apply((text, s, end) => wrap(text, s, end, "**", "**", t("notes.tool.bold.sample"))));
      if (key === "i" && !e.shiftKey) return run(() => apply((text, s, end) => wrap(text, s, end, "_", "_", t("notes.tool.italic.sample"))));
      if (key === "k" && !e.shiftKey) return run(() => apply((text, s, end) => link(text, s, end, t("notes.tool.link.sample"))));
      if (key === "e" && !e.shiftKey) return run(() => apply((text, s, end) => wrap(text, s, end, "`", "`", "code")));
      if (key === "x" && e.shiftKey) return run(() => apply((text, s, end) => wrap(text, s, end, "~~", "~~", t("notes.tool.strike.sample"))));
      if (key === "s") return run(() => void save());
      if (e.shiftKey && (e.code === "Digit7" || e.code === "Digit8" || e.code === "Digit9")) return run(() => prefix(e.code === "Digit7" ? "ol" : e.code === "Digit8" ? "ul" : "task"));
    }
    if (mod && e.altKey && e.code === "KeyP") {
      e.preventDefault();
      return pickMode(mode === "write" ? (wide ? "split" : "read") : mode === "split" ? "read" : "write");
    }
    if (e.key === "Enter" && !e.shiftKey && !mod && !e.nativeEvent.isComposing) {
      if (apply((text, s, end) => continueList(text, s, end))) e.preventDefault();
      return;
    }
    if (e.key === "Tab" && !mod && !e.altKey) {
      e.preventDefault();
      apply((text, s, end) => indent(text, s, end, e.shiftKey));
    }
  };
  const onPaste = (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const pasted = pastedFiles(e.clipboardData);
    if (pasted.length) {
      e.preventDefault();
      void upload(pasted);
      return;
    }
    // A link pasted over selected words makes them its text, as in every editor that knows Markdown.
    const text = e.clipboardData.getData("text/plain").trim();
    const el = e.currentTarget;
    if (/^https?:\/\/\S+$/.test(text) && el.selectionStart !== el.selectionEnd) {
      e.preventDefault();
      const words = el.value.slice(el.selectionStart, el.selectionEnd);
      applyEdit(el, insertText(el.value, el.selectionStart, el.selectionEnd, `[${words}](${text})`), (next) => changed({ body: next }));
    }
  };
  const onDrop = (e: React.DragEvent) => {
    if (!e.dataTransfer.files.length) return;
    e.preventDefault();
    setDropping(false);
    void upload(Array.from(e.dataTransfer.files));
  };

  // The split view scrolls the drawing with the source, by the share scrolled.
  const previewPane = useRef<HTMLDivElement>(null);
  const onFieldScroll = () => {
    const el = field.current;
    const pane = previewPane.current;
    syncTint();
    if (popup) setPopup({ ...popup });
    if (!el || !pane || mode !== "split") return;
    const share = el.scrollTop / Math.max(1, el.scrollHeight - el.clientHeight);
    pane.scrollTop = share * (pane.scrollHeight - pane.clientHeight);
  };

  // ── the conflict prompt's three ways out ─────────────────────────────────────────────────────
  const keepMine = async () => {
    if (!conflict || !idRef.current) return;
    const theirs = conflict;
    setConflict(null);
    conflictRef.current = null;
    setStatus("saving");
    try {
      const note = await notesApi.save(idRef.current, { ...latest.current, version: theirs.version, force: true });
      base.current = note.version;
      saved.current = { ...latest.current };
      dropDraft(keyOf());
      props.onSaved(note);
      setRemote(null);
      setStatus("saved");
    } catch (e) {
      setConflict(theirs);
      setStatus("conflict");
      toast(errorText(e));
    }
  };
  const takeTheirs = (theirs: Note) => {
    setTitle(theirs.title);
    setBody(theirs.body);
    latest.current = { title: theirs.title, body: theirs.body };
    saved.current = { title: theirs.title, body: theirs.body };
    base.current = theirs.version;
    dropDraft(keyOf());
    setConflict(null);
    setRemote(null);
    setStatus("saved");
  };
  const keepBoth = async () => {
    if (!conflict) return;
    const theirs = conflict;
    try {
      const copy = await notesApi.create(sessionId, { scope, title: `${latest.current.title || t("notes.untitled")} · ${t("notes.conflict.mine")}`, body: latest.current.body });
      props.onSaved(copy);
      takeTheirs(theirs);
      toast(t("notes.conflict.copied"));
    } catch (e) {
      toast(errorText(e));
    }
  };

  // ── what is drawn ───────────────────────────────────────────────────────────────────────────
  // The tinted source under the field follows the text at once (it is the text), and its scroll.
  const tinted = body.length <= TINT_MAX;
  const tintHtml = useMemo(() => (tinted ? tint(body) : ""), [body, tinted]);
  const syncTint = () => {
    const el = field.current;
    const under = el?.parentElement?.querySelector<HTMLElement>(".ne-tint");
    if (el && under) under.scrollTop = el.scrollTop;
  };
  useLayoutEffect(syncTint, [tintHtml]);
  const shownBody = useDeferredValue(body);
  const words = useMemo(() => wordCount(shownBody), [shownBody]);
  const note: Note | null = noteId && live ? { ...live, title, body } : null;
  const openImage = useCallback((images: NoteFile[], index: number) => setViewer({ index, items: images.map(viewerItem) }), []);
  const openFile = useCallback((file: NoteFile) => {
    if (isImage(file)) return setViewer({ index: 0, items: [viewerItem(file)] });
    setPreview({ base: fileBase(file.id), path: file.name, mime: file.mime });
  }, []);
  const onToggleTask = useCallback((line: number) => changed({ body: toggleTask(latest.current.body, line) }, true), []); // eslint-disable-line react-hooks/exhaustive-deps -- changed reads refs
  const onHeading = useCallback((slug: string) => {
    if (idRef.current) props.onHeadingLink(idRef.current, slug);
  }, [props.onHeadingLink]); // eslint-disable-line react-hooks/exhaustive-deps

  // What leaves the editor (a copy, a download, the composer, a duplicate) takes the words on screen,
  // saved first, so nothing typed in the last moment is left out of it.
  const act = (action: Parameters<NoteEditorProps["onAction"]>[0]) => {
    if (!note) return;
    if (action === "delete" || action === "pin") return props.onAction(action, note);
    void save().then(() => props.onAction(action, { ...note, ...latest.current }));
  };
  const menu: MenuItem[] = note ? [
    { label: t(note.pinned ? "notes.unpin" : "notes.pin"), icon: "pushpin", onSelect: () => act("pin") },
    { label: t("notes.copy"), icon: "copy", onSelect: () => act("copy") },
    { label: t("notes.download"), icon: "download", onSelect: () => act("download") },
    { label: t("notes.attach"), icon: "send", onSelect: () => act("attach") },
    "-",
    { label: t("notes.duplicate"), icon: "copy", onSelect: () => act("duplicate") },
    ...(props.moveTo ? [{ label: t("notes.move", { where: props.moveTo.label }), icon: "folder" as IconName, onSelect: () => act("move") }] : []),
    "-",
    { label: t("notes.delete"), icon: "trash", danger: true, onSelect: () => act("delete") },
  ] : [];

  const editing = mode !== "read";
  const color = live?.color ?? "";
  return (
    <Overlay>
      <div className={`ne-backdrop ${closing ? "closing" : ""}`} onMouseDown={(e) => { if (e.target === e.currentTarget) void requestClose(); }}>
        <div
          ref={dialog}
          className={`ne-dialog mode-${mode} ${closing ? "closing" : ""} ${color ? `tone-${color}` : ""}`}
          role="dialog"
          aria-modal="true"
          aria-label={title || t("notes.untitled")}
          tabIndex={-1}
          onKeyDown={(e) => {
            if (!e.defaultPrevented) trapDialogTab(e);
            if (!editing && e.key.toLowerCase() === "e" && !e.metaKey && !e.ctrlKey && !e.altKey && (e.target as HTMLElement).tagName !== "INPUT") {
              e.preventDefault();
              pickMode(wide ? "split" : "write");
            }
          }}
          onDragEnter={(e) => { if (e.dataTransfer.types.includes("Files")) setDropping(true); }}
          onDragOver={(e) => { if (e.dataTransfer.types.includes("Files")) e.preventDefault(); }}
          onDragLeave={(e) => { if (e.currentTarget === e.target) setDropping(false); }}
          onDrop={onDrop}
        >
          <header className="ne-head">
            <button type="button" className="ne-swatch" onClick={(e) => setColorAnchor(e.currentTarget)} disabled={!note} aria-label={t("notes.color")} title={t("notes.color")}><i /></button>
            <input
              ref={titleField}
              className="ne-title"
              value={title}
              placeholder={t("notes.untitled")}
              aria-label={t("notes.title")}
              maxLength={200}
              onChange={(e) => changed({ title: e.target.value })}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === "ArrowDown") {
                  e.preventDefault();
                  if (mode === "read") pickMode(wide ? "split" : "write");
                  else field.current?.focus();
                }
              }}
            />
            <SaveState status={status} flash={flash} />
            <div className="ne-modes" role="tablist" aria-label={t("notes.mode")}>
              {(wide ? (["write", "split", "read"] as const) : (["write", "read"] as const)).map((m) => (
                <button key={m} type="button" role="tab" aria-selected={mode === m} className={mode === m ? "on" : ""} onClick={() => pickMode(m)} title={`${t(`notes.mode.${m}`)} (${MOD}+Alt+P)`}>
                  <Icon name={m === "write" ? "pen" : m === "split" ? "columns" : "eye"} size={14} />
                  <span>{t(`notes.mode.${m}`)}</span>
                </button>
              ))}
            </div>
            {note && <OverflowMenu items={menu} small label={t("notes.more")} />}
            <button type="button" className="iconbtn small" onClick={() => void requestClose()} aria-label={t("common.close")} title={t("common.close")}><Icon name="close" size={16} /></button>
          </header>
          {editing && (
            <div className="ne-tools" role="toolbar" aria-label={t("notes.toolbar")}>
              {tools.map((tool, i) => tool === "-" ? <span key={`s${i}`} className="ne-sep" aria-hidden="true" /> : (
                <button key={tool.id} type="button" className="ne-tool" onMouseDown={(e) => e.preventDefault()} onClick={tool.run} aria-label={tool.keys ? `${tool.label} (${tool.keys})` : tool.label} title={tool.keys ? `${tool.label} (${tool.keys})` : tool.label}>
                  <Icon name={tool.icon} size={16} />
                </button>
              ))}
              <span className="ne-tools-hint">{t("notes.hint")}</span>
            </div>
          )}
          {(remote || status === "gone") && !conflict && (
            <div className="ne-banner" role="status">
              <Icon name="alert" size={14} />
              <span>{status === "gone" ? t("notes.gone") : t(remote?.updated_by === "agent" ? "notes.remote.agent" : "notes.remote.other")}</span>
              {status === "gone"
                ? <button type="button" className="btn small" onClick={() => noteId && void notesApi.restore(noteId).then((n) => { props.onSaved(n); setStatus("dirty"); void save(); }, (e) => toast(errorText(e)))}>{t("notes.restore")}</button>
                : <button type="button" className="btn small" onClick={() => remote && setConflict(remote)}>{t("notes.remote.compare")}</button>}
            </div>
          )}
          <div className="ne-body">
            {editing && (
              <div className="ne-write">
                {tinted && <RawHtml className="ne-tint" html={tintHtml} />}
                <textarea
                  ref={field}
                  className={`ne-field ${tinted ? "tinted" : ""}`}
                  value={body}
                  placeholder={t("notes.placeholder")}
                  aria-label={t("notes.body")}
                  spellCheck
                  onChange={(e) => { changed({ body: e.target.value }); syncPopup(e.target); }}
                  onKeyDown={onKeyDown}
                  onKeyUp={(e) => { if (e.key.startsWith("Arrow") && !popup) syncPopup(e.currentTarget); }}
                  onClick={(e) => syncPopup(e.currentTarget)}
                  onBlur={() => { window.setTimeout(() => setPopup(null), 120); if (dirty()) void save(); }}
                  onPaste={onPaste}
                  onScroll={onFieldScroll}
                />
                {popup && field.current && (
                  <CaretMenu field={field.current} popup={popup} items={popupItems} onPick={choose} onHover={(index) => setPopup({ ...popup, index })} onClose={() => setPopup(null)} />
                )}
              </div>
            )}
            {mode !== "write" && (
              <div ref={(el) => { previewPane.current = el; reading.current = el; }} className="ne-read">
                {shownBody.trim()
                  ? <NoteView body={shownBody} files={files} onToggleTask={onToggleTask} onImage={openImage} onFile={openFile} onHeading={onHeading} />
                  : <div className="ne-empty-read" onDoubleClick={() => pickMode("write")}>{t("notes.read.empty")}</div>}
              </div>
            )}
            {dropping && <div className="ne-drop" aria-hidden="true"><Icon name="attach" size={22} /><span>{t("notes.drop")}</span></div>}
          </div>
          <footer className="ne-foot">
            <span className="ne-scope">{props.scopeLabel}</span>
            <span className="ne-count">{plural("notes.words", words)} · {t("notes.minutes", { n: readingMinutes(words) })}</span>
            {uploads.map((u) => (
              <span key={u.key} className="ne-upload" title={u.name}>
                <span className="ne-upload-name">{u.name}</span>
                <span className="ne-upload-bar"><i style={{ transform: `scaleX(${Math.max(0.04, u.progress)})` }} /></span>
              </span>
            ))}
            {live && <span className="ne-edited">{t(live.updated_by === "agent" ? "notes.edited.agent" : "notes.edited.you", { when: relTime(live.updated_at) })}</span>}
          </footer>
          {conflict && <ConflictPrompt mine={latest.current} theirs={conflict} onMine={() => void keepMine()} onTheirs={() => takeTheirs(conflict)} onBoth={() => void keepBoth()} />}
          <input ref={fileInput} type="file" multiple hidden onChange={(e) => { void upload(Array.from(e.target.files ?? [])); e.target.value = ""; }} />
        </div>
      </div>
      {colorAnchor && note && (
        <Popover anchor={colorAnchor} onClose={() => setColorAnchor(null)} className="ne-colors" label={t("notes.color")}>
          {(["", ...COLORS] as const).map((c) => (
            <button key={c || "none"} type="button" className={`ne-color tone-${c || "none"} ${color === c ? "on" : ""}`} aria-label={t(`notes.color.${c || "none"}`)} title={t(`notes.color.${c || "none"}`)} aria-pressed={color === c}
              onClick={() => { setColorAnchor(null); void notesApi.meta(note.id, { color: c }).then(props.onSaved, (e) => toast(errorText(e))); }}>
              <i />
            </button>
          ))}
        </Popover>
      )}
      {viewer && <MediaViewer items={viewer.items} start={viewer.index} onClose={() => setViewer(null)} />}
      {preview && <FilePreview src={preview} onClose={() => setPreview(null)} />}
    </Overlay>
  );
}

const TABLE = "| Column | Column |\n| --- | --- |\n|  |  |";

/** Where the editor starts: the note as the host has it, or a draft this device kept of it. A draft
 *  typed on the version the host still has is the operator's latest words and is taken; one typed on
 *  an older version is the operator's words against someone else's newer ones, and opens the prompt. */
function startingPoint(sessionId: string, target: EditorTarget, live: Note | null): { title: string; body: string; saved: { title: string; body: string }; base: number | null; dirty: boolean; conflict: Note | null } {
  const draft = readDraft(draftKey(target.noteId, sessionId, target.scope));
  if (target.noteId && live) {
    const saved = { title: live.title, body: live.body };
    if (draft && (draft.title !== live.title || draft.body !== live.body)) {
      if (draft.base === live.version) return { title: draft.title, body: draft.body, saved, base: live.version, dirty: true, conflict: null };
      return { title: draft.title, body: draft.body, saved, base: draft.base, dirty: true, conflict: live };
    }
    return { ...saved, saved, base: live.version, dirty: false, conflict: null };
  }
  const start = draft ?? target.template ?? { title: "", body: "" };
  return { title: start.title, body: start.body, saved: { title: "", body: "" }, base: null, dirty: !!draft && !!(draft.title || draft.body), conflict: null };
}

function viewerItem(file: NoteFile): ViewerItem {
  return { key: file.id, kind: "image", name: file.name, filename: file.name, source: { base: fileBase(file.id), path: file.name, mime: file.mime }, download: downloadHref(fileBase(file.id), file.name) };
}

function SaveState({ status, flash }: { status: Status; flash: "agent" | "operator" | null }) {
  if (flash) return <span className="ne-state flash" role="status"><Icon name={flash === "agent" ? "bolt" : "reload"} size={13} />{t(flash === "agent" ? "notes.flash.agent" : "notes.flash.other")}</span>;
  if (status === "saving" || status === "dirty") return <span className="ne-state busy" role="status"><i className="ne-spin" aria-hidden="true" />{t("notes.saving")}</span>;
  if (status === "saved") return <span className="ne-state ok" role="status"><svg className="ne-tick" viewBox="0 0 16 16" width="13" height="13" aria-hidden="true"><path d="M3 8.5l3.2 3L13 4.5" /></svg>{t("notes.saved")}</span>;
  if (status === "offline") return <span className="ne-state warn" role="status"><Icon name="offline" size={13} />{t("notes.offline")}</span>;
  if (status === "conflict") return <span className="ne-state warn" role="status"><Icon name="alert" size={13} />{t("notes.conflict.short")}</span>;
  return <span className="ne-state" aria-hidden="true" />;
}

// ── the menus at the caret ──────────────────────────────────────────────────────────────────

type PopupItem = { kind: "file"; file: NoteFile; group: string } | { kind: "upload" } | { kind: "command"; id: string; icon: IconName; label: string; hint: string; run: () => void };

function usePopupItems(popup: Popup | null, groups: PickGroup[], commands: PopupItem[]): PopupItem[] {
  return useMemo(() => {
    if (!popup) return [];
    const q = popup.query.toLowerCase();
    if (popup.kind === "file") {
      const found: PopupItem[] = [];
      for (const group of groups) for (const file of group.files) if (!q || file.name.toLowerCase().includes(q)) found.push({ kind: "file", file, group: group.label });
      return [...found.slice(0, 40), { kind: "upload" }];
    }
    return commands.filter((item) => item.kind === "command" && (!q || item.id.includes(q) || item.label.toLowerCase().includes(q)));
  }, [popup?.kind, popup?.query, groups, commands]); // eslint-disable-line react-hooks/exhaustive-deps
}

function slashCommandsFor(run: { prefix: (kind: LineKind) => void; block: (snippet: string, select?: [number, number]) => void; apply: (make: (text: string, s: number, e: number) => Edit | null) => boolean; upload: () => void; picker: () => void }) {
  const cmd = (id: string, icon: IconName, go: () => void, hint = ""): PopupItem => ({ kind: "command", id, icon, label: t(`notes.slash.${id}`), hint, run: go });
  return [
    cmd("h1", "heading", () => run.prefix("h1"), "#"),
    cmd("h2", "heading", () => run.prefix("h2"), "##"),
    cmd("h3", "heading", () => run.prefix("h3"), "###"),
    cmd("todo", "tasks", () => run.prefix("task"), "- [ ]"),
    cmd("bullet", "list", () => run.prefix("ul"), "-"),
    cmd("numbered", "olist", () => run.prefix("ol"), "1."),
    cmd("quote", "quote", () => run.prefix("quote"), ">"),
    cmd("code", "braces", () => run.block("```\n\n```", [4, 4]), "```"),
    cmd("table", "table", () => run.block(TABLE, [2, 8])),
    cmd("mermaid", "chart", () => run.block("```mermaid\nflowchart LR\n  A[Idea] --> B[Draft] --> C[Done]\n```", [24, 58])),
    cmd("flow", "split", () => run.block(`:::flow ${t("notes.slash.flow.title")}\n💡 ${t("notes.slash.flow.a")}\n↓\n✅ ${t("notes.slash.flow.b")}\n:::`)),
    cmd("divider", "hr", () => run.block("---"), "---"),
    cmd("date", "calendar", () => run.apply((text, s, e) => insertText(text, s, e, new Date().toLocaleDateString(undefined, { year: "numeric", month: "long", day: "numeric" })))),
    cmd("image", "image", run.upload),
    cmd("file", "attach", run.picker, "[["),
  ];
}

function CaretMenu({ field, popup, items, onPick, onHover, onClose }: { field: HTMLTextAreaElement; popup: Popup; items: PopupItem[]; onPick: (index: number) => void; onHover: (index: number) => void; onClose: () => void }) {
  useLayer(onClose);
  const box = useRef<HTMLDivElement>(null);
  const [place, setPlace] = useState<{ top: number; left: number; up: boolean } | null>(null);
  useLayoutEffect(() => {
    const point = caretPoint(field, popup.from);
    const host = field.parentElement as HTMLElement;
    const width = Math.min(320, host.clientWidth - 16);
    const left = Math.max(8, Math.min(field.offsetLeft + point.left, host.clientWidth - width - 8));
    const below = field.offsetTop + point.top + point.height + 6;
    const up = below + 260 > host.clientHeight && field.offsetTop + point.top > 200;
    setPlace({ top: up ? field.offsetTop + point.top - 6 : below, left, up });
  }, [field, popup.from, popup.query]);
  useEffect(() => {
    box.current?.querySelector(".on")?.scrollIntoView?.({ block: "nearest" });
  }, [popup.index]);
  if (!place) return null;
  let lastGroup = "";
  const rows: ReactNode[] = [];
  items.forEach((item, i) => {
    if (item.kind === "file" && item.group !== lastGroup) {
      lastGroup = item.group;
      rows.push(<div key={`g${item.group}`} className="ne-menu-group">{item.group}</div>);
    }
    rows.push(
      <button key={item.kind === "file" ? item.file.id : item.kind === "command" ? item.id : "upload"} type="button" role="option" aria-selected={popup.index === i} className={`ne-menu-item ${popup.index === i ? "on" : ""}`} onMouseDown={(e) => e.preventDefault()} onMouseEnter={() => onHover(i)} onClick={() => onPick(i)}>
        {item.kind === "file" ? <FileGlyph file={item.file} /> : <span className="ne-menu-ico"><Icon name={item.kind === "upload" ? "plus" : item.icon} size={15} /></span>}
        <span className="ne-menu-label">{item.kind === "file" ? item.file.name : item.kind === "upload" ? t("notes.picker.upload") : item.label}</span>
        {item.kind === "command" && item.hint && <kbd>{item.hint}</kbd>}
      </button>,
    );
  });
  return (
    <div ref={box} className={`ne-menu ${place.up ? "up" : ""}`} role="listbox" aria-label={popup.kind === "file" ? t("notes.picker") : t("notes.slash")} style={{ top: place.top, left: place.left }}>
      {items.length === (popup.kind === "file" ? 1 : 0) && <div className="ne-menu-empty">{popup.kind === "file" ? t("notes.picker.none") : t("notes.slash.none")}</div>}
      {rows}
    </div>
  );
}

/** A file's small square: its picture for an image, its kind for anything else. */
export function FileGlyph({ file, size = "sm" }: { file: NoteFile; size?: "sm" | "lg" }) {
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    if (!isImage(file)) return;
    let gone = false;
    fileUrl(file.id).then((u) => !gone && setUrl(u), () => undefined);
    return () => {
      gone = true;
    };
  }, [file.id, file.mime]); // eslint-disable-line react-hooks/exhaustive-deps
  if (isImage(file)) return <span className={`note-glyph ${size} pic`}>{url && <img src={url} alt="" />}</span>;
  const ext = (/\.([A-Za-z0-9]{1,4})$/.exec(file.name)?.[1] ?? file.mime.split("/")[1] ?? "").slice(0, 4).toUpperCase();
  return <span className={`note-glyph ${size}`} data-kind={fileFamily(file)}>{ext}</span>;
}

// ── the conflict prompt ─────────────────────────────────────────────────────────────────────

function ConflictPrompt({ mine, theirs, onMine, onTheirs, onBoth }: { mine: { title: string; body: string }; theirs: Note; onMine: () => void; onTheirs: () => void; onBoth: () => void }) {
  const rows = useMemo(() => lineDiff(theirs.body, mine.body), [theirs.body, mine.body]);
  const changedRows = rows.filter((row) => row.kind !== "same").length;
  return (
    <div className="ne-conflict-wrap">
      <div className="ne-conflict" role="alertdialog" aria-labelledby="ne-conflict-title">
        <div className="ne-conflict-head">
          <Icon name="alert" size={18} />
          <div>
            <h3 id="ne-conflict-title">{t("notes.conflict.title")}</h3>
            <p>{t(theirs.updated_by === "agent" ? "notes.conflict.agent" : "notes.conflict.other", { when: relTime(theirs.updated_at) })}</p>
          </div>
        </div>
        <div className="ne-diff" aria-label={t("notes.conflict.diff")}>
          {mine.title !== theirs.title && (
            <>
              <div className="ne-diff-row theirs"><span>{t("notes.conflict.theirs")}</span>{theirs.title || t("notes.untitled")}</div>
              <div className="ne-diff-row mine"><span>{t("notes.conflict.mine")}</span>{mine.title || t("notes.untitled")}</div>
            </>
          )}
          {rows.map((row, i) => row.kind === "same" ? <div key={i} className="ne-diff-row same">{row.text || " "}</div> : <div key={i} className={`ne-diff-row ${row.kind}`}><span>{t(row.kind === "mine" ? "notes.conflict.mine" : "notes.conflict.theirs")}</span>{row.text || " "}</div>)}
          {!changedRows && mine.title === theirs.title && <div className="ne-diff-row same">{t("notes.conflict.same")}</div>}
        </div>
        <div className="ne-conflict-actions">
          <button type="button" className="btn" onClick={onTheirs}>{t("notes.conflict.take")}</button>
          <button type="button" className="btn" onClick={onBoth}>{t("notes.conflict.both")}</button>
          <button type="button" className="btn primary" onClick={onMine}>{t("notes.conflict.keep")}</button>
        </div>
      </div>
    </div>
  );
}
