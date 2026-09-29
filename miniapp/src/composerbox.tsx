// A growing full-width field above one row of controls. Drafts belong to the session;
// actions are supplied by the parent so the card also works inside the voice page.

import { forwardRef, useCallback, useEffect, useImperativeHandle, useLayoutEffect, useMemo, useRef, useState } from "react";
import { api, AsrStatus, ModelFallback, Question, SlashCommand } from "./api";
import { Popover } from "./dialogs";
import { Icon } from "./icons";
import { fileGlyph, previewKind, canPreview } from "./preview";
import { enterSends, errorText, fmtBytes, fmtTok, haptic } from "./ui";
import { ModelChoice, ModelSelect } from "./modelselect";
import { EffortSelect } from "./effortselect";
import { ModeInfo, ModeSelect } from "./modeselect";
import { blobToWav } from "./wav";
import {
  Approval,
  ComposerStatus,
  DRAFT_DEBOUNCE_MS,
  QueuedSteer,
  answersComplete,
  clearDraft,
  composerKey,
  dockKey,
  fieldHeight,
  composerContext,
  ComposerPlace,
  placeholderKey,
  primaryAction,
  readDraft,
  writeDraft,
} from "./composer";
import { fmtInt } from "./components";
import { t } from "./i18n";
import { insideTerminal } from "./terminal/keys";

export type Answer = { question: string; selected: string[]; custom: string | null };

export type ComposerHandle = {
  focus: () => void;
  /** Put text into the field, after whatever is there. */
  insert: (text: string) => void;
  /** Attach files from outside the pill: a drop on the conversation. */
  addFiles: (files: Iterable<File>) => void;
  /** Open the model list, from wherever "change the model" is offered. */
  openModel: () => void;
};

export type ComposerProps = {
  sessionId: string;
  status: ComposerStatus;
  /** Send the text and the files; while a run is on, the host queues it as a steer. Rejects on failure. */
  onSend: (text: string, files: File[], clientMessageId?: string) => Promise<void>;
  onStop: () => void;
  commands: SlashCommand[];
  /** Run a slash command. Rejects on failure, and the draft comes back. */
  onCommand: (line: string) => Promise<void>;
  model: string;
  fallback: ModelFallback | null;
  onChooseModel: (choice: ModelChoice) => void;
  /** Effective thinking for this session (override or the preset). */
  thinking?: boolean;
  reasoningEffort?: string;
  onChooseEffort?: (effort: string) => void;
  /** The session's mode (empty: the plain agent), the configured ones, and the YAGNI switch. */
  mode?: string;
  modes?: ModeInfo[];
  yagni?: boolean;
  onChooseMode?: (mode: string) => void;
  onYagni?: (on: boolean) => void;
  place?: ComposerPlace;
  /** What the empty field says while nothing runs, when the conversation is with someone in particular:
   *  "Write to the orchestrator…". Running and waiting keep their own words. */
  idlePlaceholder?: string;
  context?: { tokens: number; window: number; messages: number } | null;
  onContext?: () => void;
  asr?: AsrStatus | null;
  /** The voice page, when the installation has one. */
  onVoice?: () => void;
  steers: QueuedSteer[];
  onWithdraw?: (steer: QueuedSteer) => void;
  questions?: Question[] | null;
  onAnswer?: (answers: Answer[]) => Promise<void>;
  approval?: Approval | null;
  onApprove?: (a: Approval) => void;
  onDeny?: (a: Approval) => void;
  /** A file waiting in the pill, opened before it goes. */
  onPreviewFile?: (file: File) => void;
  phone: boolean;
  toast: (text: string) => void;
};

export const Composer = forwardRef<ComposerHandle, ComposerProps>(function Composer(props, ref) {
  const { sessionId, status, onSend, onStop, commands, onCommand, phone, toast } = props;
  const [draft, setDraftState] = useState(() => readDraft(sessionId));
  const [files, setFiles] = useState<File[]>([]);
  const [sending, setSending] = useState(false);
  const [modelOpen, setModelOpen] = useState(false);
  const [plusOpen, setPlusOpen] = useState(false);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const photoInput = useRef<HTMLInputElement>(null);
  const plusButton = useRef<HTMLButtonElement>(null);
  const dock = useRef<HTMLDivElement>(null);
  const draftTimer = useRef(0);
  const retryId = useRef<string | null>(null);

  // The draft is the session's: leaving and coming back finds it, another session does not.
  useEffect(() => {
    setDraftState(readDraft(sessionId));
    setFiles([]);
    return () => {
      window.clearTimeout(draftTimer.current);
    };
  }, [sessionId]);
  const setDraft = useCallback(
    (next: string) => {
      setDraftState(next);
      window.clearTimeout(draftTimer.current);
      draftTimer.current = window.setTimeout(() => writeDraft(sessionId, next), DRAFT_DEBOUNCE_MS);
    },
    [sessionId],
  );

  // Measure the placeholder too, and refit when a panel or viewport changes the width.
  const fit = useCallback(() => {
    const el = textarea.current;
    if (!el) return;
    el.style.height = "auto";
    const cs = getComputedStyle(el);
    const line = parseFloat(cs.lineHeight);
    const pad = parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom);
    el.style.height = `${fieldHeight(el.scrollHeight, line, pad, 1, phone ? 5 : undefined)}px`;
  }, [phone]);
  useLayoutEffect(fit, [draft, fit, status, props.questions]);
  useEffect(() => {
    const el = textarea.current;
    if (!el) return;
    let width = 0;
    const observer = new ResizeObserver(([entry]) => {
      if (entry.contentRect.width === width) return;
      width = entry.contentRect.width;
      fit();
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, [fit]);

  const addFiles = useCallback((incoming: Iterable<File>) => {
    const named = Array.from(incoming).map((f) => {
      // A pasted screenshot arrives as "image.png" every time: give each one a name of its own.
      if (!/^(image|blob|file)(\.[a-z0-9]+)?$/i.test(f.name)) return f;
      const ext = f.name.includes(".") ? f.name.slice(f.name.lastIndexOf(".")) : f.type.startsWith("image/") ? `.${f.type.slice(6).replace("jpeg", "jpg")}` : "";
      const stamp = new Date().toISOString().slice(0, 19).replace(/[-:]/g, "").replace("T", "-");
      return new File([f], `${f.type.startsWith("image/") ? "screenshot" : "pasted"}-${stamp}${ext}`, { type: f.type, lastModified: f.lastModified });
    });
    if (named.length) setFiles((p) => [...p, ...named]);
  }, []);

  useImperativeHandle(
    ref,
    () => ({
      focus: () => textarea.current?.focus(),
      insert: (text) => {
        setDraft(draft.trim() ? `${draft.trimEnd()}\n\n${text}` : text);
        textarea.current?.focus();
      },
      addFiles,
      openModel: () => setModelOpen(true),
    }),
    [draft, setDraft, addFiles],
  );

  // ── the slash palette ──
  const paletteQuery = draft.startsWith("/") && !draft.includes("\n") && !draft.includes(" ") ? draft.slice(1).toLowerCase() : null;
  const paletteItems = useMemo(() => (paletteQuery === null ? [] : commands.filter((c) => c.name.startsWith(paletteQuery))), [commands, paletteQuery]);
  const pickCommand = (c: SlashCommand) => {
    if (c.args) {
      setDraft(`/${c.name} `);
      textarea.current?.focus();
    } else void runCommand(`/${c.name}`);
  };
  async function runCommand(line: string) {
    setDraft("");
    try {
      await onCommand(line);
    } catch (e) {
      setDraft(line);
      toast(errorText(e));
    }
  }

  // ── the agent's question ──
  const questions = props.questions ?? null;
  const asking = !!questions && questions.length > 0;
  const [answers, setAnswers] = useState<{ selected: string[]; custom: string }[]>([]);
  useEffect(() => {
    setAnswers((questions ?? []).map(() => ({ selected: [], custom: "" })));
  }, [questions]);
  const complete = asking && answersComplete(questions!, answers);
  async function reply() {
    if (!questions || !props.onAnswer) return;
    if (!complete) {
      dock.current?.querySelector<HTMLElement>("button, input")?.focus();
      return;
    }
    try {
      await props.onAnswer(questions.map((q, i) => ({ question: q.question, selected: answers[i].selected, custom: answers[i].custom || null })));
    } catch (e) {
      toast(errorText(e));
    }
  }

  // ── send / stop / queue ──
  const { action, enabled } = primaryAction({ status, hasDraft: !!draft.trim(), hasFiles: files.length > 0, asking, sending });
  async function send() {
    const text = draft.trim();
    const going = files;
    if (sending || (!text && going.length === 0)) return;
    if (text.startsWith("/") && going.length === 0 && commands.some((c) => c.name === text.slice(1).split(" ")[0].toLowerCase())) {
      await runCommand(text);
      return;
    }
    setSending(true);
    const clientMessageId = retryId.current ?? crypto.randomUUID();
    retryId.current = clientMessageId;
    setDraftState("");
    window.clearTimeout(draftTimer.current);
    clearDraft(sessionId);
    setFiles([]);
    if (fileInput.current) fileInput.current.value = "";
    try {
      await onSend(text, going, clientMessageId);
      retryId.current = null;
      haptic("light");
    } catch (e) {
      setDraft(text);
      setFiles(going);
      toast(errorText(e));
    } finally {
      setSending(false);
    }
  }
  function primary() {
    if (action === "stop") onStop();
    else if (action === "reply") void reply();
    else void send();
  }

  // ── keys ──
  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    const intent = composerKey(e, { enterSends: enterSends(), paletteOpen: paletteItems.length > 0 });
    if (intent === "complete") {
      e.preventDefault();
      pickCommand(paletteItems[0]);
    } else if (intent === "escape") {
      if (paletteQuery !== null) {
        e.preventDefault();
        setDraft("");
      } else if (draft === "") e.currentTarget.blur();
    } else if (intent === "send") {
      e.preventDefault();
      if (action === "reply") void reply();
      else void send();
    } else if (intent === "model") {
      e.preventDefault();
      setModelOpen((o) => !o);
    } else if (intent === "stop") {
      if (status === "running") {
        e.preventDefault();
        onStop();
      }
    }
  }
  // ⌘M and ⌘⇧S reach the composer from anywhere on the screen; y / n answer the dock when nobody is typing.
  const approval = props.approval ?? null;
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // Ctrl+M is Enter in a terminal and Ctrl+Shift+S nothing the composer should hear.
      if (insideTerminal(e.target)) return;
      const target = e.target as HTMLElement | null;
      const typing = !!target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable);
      if (typing && target === textarea.current) return;
      const intent = composerKey(e, { enterSends: false, paletteOpen: false });
      if (intent === "model") {
        e.preventDefault();
        setModelOpen((o) => !o);
        return;
      }
      if (intent === "stop" && status === "running") {
        e.preventDefault();
        onStop();
        return;
      }
      if (!approval) return;
      const which = dockKey(e, typing);
      if (which === "approve") props.onApprove?.(approval);
      else if (which === "deny") props.onDeny?.(approval);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [approval, status, onStop, props.onApprove, props.onDeny]);

  // ── paste and the + menu ──
  function onPaste(e: React.ClipboardEvent) {
    const items = Array.from(e.clipboardData?.items ?? []);
    const pasted = items.filter((it) => it.kind === "file").map((it) => it.getAsFile()).filter((f): f is File => !!f);
    if (!pasted.length) return;
    // Text pasted alongside (rich-text editors add an HTML rendering of the image) is not wanted.
    e.preventDefault();
    addFiles(pasted);
    haptic("light");
  }
  async function pasteFromClipboard() {
    setPlusOpen(false);
    try {
      const items = navigator.clipboard.read ? await navigator.clipboard.read() : [];
      const got: File[] = [];
      for (const item of items) {
        const type = item.types.find((x) => x.startsWith("image/"));
        if (!type) continue;
        const blob = await item.getType(type);
        got.push(new File([blob], `image.${type.slice(6).replace("jpeg", "jpg")}`, { type }));
      }
      if (got.length) {
        addFiles(got);
        return;
      }
      const text = await navigator.clipboard.readText();
      if (!text.trim()) throw new Error("empty");
      setDraft(draft ? `${draft.trimEnd()} ${text.trim()}` : text.trim());
      textarea.current?.focus();
    } catch {
      toast(t("composer.paste.none"));
    }
  }

  const ctx = props.context ?? null;
  const pct = ctx && ctx.window > 0 ? Math.round((100 * ctx.tokens) / ctx.window) : null;
  const primaryLabel = action === "stop" ? t("session.stop") : action === "queue" ? t("composer.queue") : action === "reply" ? t("composer.reply") : t("session.send");
  const place = composerContext(props.place);

  return (
    <div className="composer" data-primary={action}>
      {props.steers.length > 0 && (
        <div className="steers" aria-label={t("composer.steers")}>
          {props.steers.map((s) => (
            <div key={s.id} className="steer" data-steer={s.id}>
              <Icon name="forward" size={14} />
              <span className="steer-text clamp-2">{s.text}</span>
              <span className="steer-hint sub">{t("composer.queue.hint")}</span>
              {props.onWithdraw && (
                <button type="button" className="iconbtn small steer-x" onClick={() => props.onWithdraw!(s)} aria-label={t("composer.steer.withdraw")} title={t("composer.steer.withdraw")}>
                  <Icon name="close" size={13} />
                </button>
              )}
            </div>
          ))}
        </div>
      )}
      {approval && (
        <div ref={dock} className="dock approval" role="group" aria-label={t("composer.approval.title", { tool: approval.tool })}>
          <div className="dock-title">
            <Icon name="wrench" size={16} />
            <span className="grow">{t("composer.approval.title", { tool: approval.tool || "tool" })}</span>
            <kbd className="sub">{t("composer.approval.hint")}</kbd>
          </div>
          {approval.detail && <div className="dock-detail mono truncate" title={approval.detail}>{approval.detail}</div>}
          <div className="dock-actions">
            <button type="button" className="btn small primary" onClick={() => props.onApprove?.(approval)}>{t("composer.approve")}</button>
            <button type="button" className="btn small" onClick={() => props.onDeny?.(approval)}>{t("composer.deny")}</button>
          </div>
        </div>
      )}
      {asking && !approval && (
        <div ref={dock} className="dock question" role="group" aria-label={t("composer.question")}>
          {questions!.map((q, qi) => (
            <div key={qi} className="dock-q">
              <div className="dock-title">
                <Icon name="question" size={16} />
                <span className="grow">{q.header ? `${q.header} · ` : ""}{q.question}</span>
              </div>
              {(q.options ?? []).length > 0 && (
                <div className="dock-options">
                  {(q.options ?? []).map((o) => {
                    const on = answers[qi]?.selected.includes(o.label);
                    return (
                      <button key={o.label} type="button" className={`btn small option ${on ? "selected" : ""}`} aria-pressed={on} title={o.description ?? undefined} onClick={() => setAnswers((prev) => prev.map((a, i) => (i !== qi ? a : !q.multiSelect ? { ...a, selected: [o.label] } : { ...a, selected: on ? a.selected.filter((x) => x !== o.label) : [...a.selected, o.label] })))}>
                        {o.label}
                      </button>
                    );
                  })}
                </div>
              )}
              {(q.allow_custom || !(q.options ?? []).length) && (
                <input className="field" placeholder={t("session.answer.placeholder")} value={answers[qi]?.custom ?? ""} onChange={(e) => setAnswers((p) => p.map((a, i) => (i === qi ? { ...a, custom: e.target.value } : a)))} onKeyDown={(e) => { if (e.key === "Enter") void reply(); }} />
              )}
            </div>
          ))}
          <div className="dock-actions">
            <button type="button" className="btn small primary" disabled={!complete} onClick={() => void reply()}>{t("session.answer")}</button>
          </div>
        </div>
      )}
      {paletteItems.length > 0 && (
        <div className="palette" role="listbox">
          {paletteItems.slice(0, 8).map((c) => (
            <button key={c.name} type="button" role="option" aria-selected={false} className="palette-item" onClick={() => pickCommand(c)}>
              <span className="mono">/{c.name} <span className="sub">{c.args}</span></span>
              <span className="sub">{c.description}</span>
            </button>
          ))}
        </div>
      )}
      <div className="composer-box">
        {status === "running" && <div className="composer-steering">{t("composer.steering")}</div>}
        {!phone && place.length > 0 && (
          <div className="composer-place" aria-label={t("composer.place")}>
            {place.map((chip) => <span key={chip.kind} className="composer-place-chip" title={t(`composer.place.${chip.kind}`, { name: chip.name })}>
              <Icon name="folder" size={12} /><span className="truncate">{chip.name}</span>
            </span>)}
          </div>
        )}
        {files.length > 0 && (
          <div className="attachments" aria-label={t("session.attachments")}>
            {files.map((f, i) => (
              <AttachmentCard key={`${f.name}-${f.size}-${f.lastModified}-${i}`} file={f} onOpen={() => props.onPreviewFile?.(f)} onRemove={() => setFiles((p) => p.filter((_, j) => j !== i))} />
            ))}
          </div>
        )}
        <textarea
          ref={textarea}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder={props.idlePlaceholder && placeholderKey(status, asking) === "session.composer.idle" ? props.idlePlaceholder : t(placeholderKey(status, asking))}
          rows={1}
          onPaste={onPaste}
          onKeyDown={onKeyDown}
          aria-label={props.idlePlaceholder && placeholderKey(status, asking) === "session.composer.idle" ? props.idlePlaceholder : t(placeholderKey(status, asking))}
        />
        <div className="composer-row">
          <input ref={fileInput} type="file" multiple hidden onChange={(e) => { addFiles(e.target.files ?? []); e.target.value = ""; }} />
          <input ref={photoInput} type="file" accept="image/*" capture="environment" hidden onChange={(e) => { addFiles(e.target.files ?? []); e.target.value = ""; }} />
          <button ref={plusButton} type="button" className={`iconbtn flat plus ${plusOpen ? "on" : ""}`} onClick={() => setPlusOpen((o) => !o)} aria-label={t("composer.plus")} title={t("composer.plus")} aria-haspopup="menu" aria-expanded={plusOpen}>
            <Icon name="plus" />
          </button>
          {plusOpen && (
            <Popover anchor={plusButton.current} onClose={() => setPlusOpen(false)} className="plus-menu" label={t("composer.plus")}>
              <button type="button" role="menuitem" onClick={() => { setPlusOpen(false); fileInput.current?.click(); }}><Icon name="attach" size={16} />{t("session.attach")}</button>
              {phone && <button type="button" role="menuitem" onClick={() => { setPlusOpen(false); photoInput.current?.click(); }}><Icon name="image" size={16} />{t("composer.photo")}</button>}
              <button type="button" role="menuitem" onClick={() => void pasteFromClipboard()}><Icon name="copy" size={16} />{t("composer.paste")}</button>
            </Popover>
          )}
          {props.onChooseMode && props.onYagni
            ? <ModeSelect mode={props.mode ?? ""} modes={props.modes ?? []} yagni={!!props.yagni} onChooseMode={props.onChooseMode} onYagni={props.onYagni} sheet={phone} />
            : <span className="composer-mode">{t("composer.mode.agent")}</span>}
          {(!phone || status !== "running") && <ModelSelect model={props.model} fallback={props.fallback} open={modelOpen} onOpenChange={setModelOpen} onChoose={props.onChooseModel} sheet={phone}
            effort={phone ? props.reasoningEffort : undefined} thinking={props.thinking} onChooseEffort={phone ? props.onChooseEffort : undefined} />}
          <div className="composer-tools">
            {!phone && props.onChooseEffort && (
              <EffortSelect effort={props.reasoningEffort} thinking={props.thinking} onChoose={props.onChooseEffort} />
            )}
            {pct !== null && ctx && (
              <button type="button" className={`ctx-ring ${pct >= 90 ? "bad" : pct >= 60 ? "attn" : ""}`} onClick={props.onContext} title={t("composer.context", { pct, used: fmtTok(ctx.tokens), window: fmtTok(ctx.window), n: fmtInt(ctx.messages) })} aria-label={t("composer.context.label")}>
                <Ring pct={pct} />
                {/* The number beside the ring: a partial circle alone, at rest, read as a spinner. */}
                <span className="ctx-pct" aria-hidden>{`${pct}%`}</span>
              </button>
            )}
            {props.asr?.configured && !draft.trim() && <MicButton sessionId={sessionId} asr={props.asr} onText={(text) => { setDraft(draft.trim() ? `${draft.trimEnd()}\n\n${text}` : text); textarea.current?.focus(); }} onAutosend={(text) => onSend(text, [])} toast={toast} />}
            {props.onVoice && !draft.trim() && (
              <button type="button" className="iconbtn flat voice" onClick={props.onVoice} aria-label={t("composer.voice")} title={t("composer.voice")}>
                {/* A voice's wave, not a play triangle: beside the red stop of a live run a triangle
                    read as a second "start". */}
                <Icon name="wave" />
              </button>
            )}
            <button type="button" className={`roundbtn primary ${action}`} onClick={primary} disabled={!enabled} aria-label={primaryLabel} title={action === "queue" ? `${primaryLabel} — ${t("composer.queue.hint")}` : primaryLabel} data-action={action}>
              <Icon name={action === "stop" ? "stop" : action === "reply" ? "send" : "up"} />
            </button>
          </div>
        </div>
      </div>
    </div>
  );
});

/** The context in use, as an arc: full circle is the whole window. Numbers live in the tooltip. */
function Ring({ pct }: { pct: number }) {
  const r = 7;
  const c = 2 * Math.PI * r;
  const filled = Math.max(0, Math.min(100, pct)) / 100;
  return (
    <svg viewBox="0 0 18 18" width="18" height="18" aria-hidden="true">
      <circle cx="9" cy="9" r={r} fill="none" stroke="currentColor" strokeOpacity="0.25" strokeWidth="2" />
      <circle cx="9" cy="9" r={r} fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeDasharray={`${c * filled} ${c}`} transform="rotate(-90 9 9)" />
    </svg>
  );
}

function AttachmentCard({ file, onOpen, onRemove }: { file: File; onOpen: () => void; onRemove: () => void }) {
  const isImage = file.type.startsWith("image/") || previewKind(file.name) === "image";
  const url = useMemo(() => (isImage ? URL.createObjectURL(file) : null), [file, isImage]);
  useEffect(() => () => { if (url) URL.revokeObjectURL(url); }, [url]);
  return (
    <div className={`attachment ${isImage ? "image" : ""}`}>
      <button type="button" className="attachment-open" onClick={onOpen} title={canPreview(file.name) ? t("preview.open") : file.name}>
        {url ? <img src={url} alt={file.name} /> : <span className="attachment-glyph" aria-hidden>{fileGlyph(file.name)}</span>}
        <span className="attachment-meta">
          <span className="attachment-name">{file.name}</span>
          <span className="sub">{fmtBytes(file.size)}</span>
        </span>
      </button>
      <button type="button" className="attachment-x" onClick={onRemove} aria-label={t("common.remove")} title={t("common.remove")}>
        <Icon name="close" size={12} />
      </button>
    </div>
  );
}

/** Hold-free recording: one tap starts, the next stops and the words land in the field (or go straight out with autosend). */
function MicButton({ sessionId, asr, onText, onAutosend, toast }: { sessionId: string; asr: AsrStatus; onText: (text: string) => void; onAutosend: (text: string) => Promise<void>; toast: (t: string) => void }) {
  const [rec, setRec] = useState<MediaRecorder | null>(null);
  const [seconds, setSeconds] = useState(0);
  const [busy, setBusy] = useState(false);
  const chunks = useRef<Blob[]>([]);
  const startedAt = useRef(0);
  const supported = typeof MediaRecorder !== "undefined" && !!navigator.mediaDevices?.getUserMedia;
  useEffect(() => {
    if (!rec) return;
    const timer = setInterval(() => setSeconds(Math.round((Date.now() - startedAt.current) / 1000)), 500);
    return () => clearInterval(timer);
  }, [rec]);
  useEffect(() => () => rec?.stream.getTracks().forEach((tr) => tr.stop()), [rec]);
  async function transcribe(blob: Blob, took: number) {
    if (took > asr.max_seconds) {
      toast(t("session.transcribe.long", { n: took, max: asr.max_seconds }));
      return;
    }
    setBusy(true);
    try {
      const wav = await blobToWav(blob);
      const form = new FormData();
      form.append("audio", wav, "recording.wav");
      const res = await fetch(`/api/sessions/${sessionId}/transcribe`, { method: "POST", headers: api.authHeaders(), body: form });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail ?? `transcription failed (${res.status})`);
      const r = (await res.json()) as { transcript: string; text: string; autosend: boolean };
      if (r.autosend) {
        await onAutosend(r.text);
        toast(t("session.transcribe.sent", { text: r.transcript.slice(0, 80) }));
      } else {
        onText(r.text);
        haptic("success");
      }
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  async function start() {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const type = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"].find((x) => MediaRecorder.isTypeSupported(x));
      const r = new MediaRecorder(stream, type ? { mimeType: type } : undefined);
      chunks.current = [];
      r.ondataavailable = (e) => e.data.size && chunks.current.push(e.data);
      r.onstop = () => {
        stream.getTracks().forEach((tr) => tr.stop());
        const blob = new Blob(chunks.current, { type: r.mimeType || "audio/webm" });
        const took = Math.round((Date.now() - startedAt.current) / 1000);
        setRec(null);
        setSeconds(0);
        if (blob.size > 0 && took >= 1) void transcribe(blob, took);
      };
      startedAt.current = Date.now();
      r.start(250);
      setRec(r);
      haptic("light");
    } catch {
      setRec(null);
    }
  }
  if (rec) {
    return (
      <button type="button" className="chip recording" onClick={() => rec.stop()} title={t("session.mic.stop")} aria-label={t("session.mic.stop.label")}>
        <span className="rec-dot" /> {t("session.mic.seconds", { n: seconds })}
      </button>
    );
  }
  return (
    <button type="button" className="iconbtn flat mic" onClick={start} disabled={!supported || busy} title={t(!supported ? "session.mic.none" : busy ? "session.mic.busy" : "session.mic.title")} aria-label={t("session.mic")}>
      <Icon name={busy ? "dot" : "mic"} />
    </button>
  );
}
