// A growing full-width field above one row of controls. Drafts belong to the session;
// actions are supplied by the parent so the card also works inside the voice page.

import { forwardRef, type ReactNode, useCallback, useEffect, useImperativeHandle, useLayoutEffect, useMemo, useRef, useState } from "react";
import { api, ApiError, AsrStatus, ModelFallback, Question, SkillEntry, SlashCommand } from "./api";
import { Popover } from "./ui/dialogs";
import { Icon } from "./icons";
import { fileGlyph, previewKind, canPreview } from "./preview";
import { enterSends, errorText, fmtBytes, fmtTok, haptic } from "./ui";
import { ModelChoice, ModelSelect } from "./modelselect";
import { ModeInfo, ModeSelect } from "./modeselect";
import { MicButton, VoiceBar, VoiceNoteFailed, useVoiceNote } from "./voicebar";
import { landWords } from "./voicenote";
import { loadDraftFiles, saveDraftFiles } from "./project/draftfiles";
import { useReply } from "./project/chat";
import {
  Approval,
  ComposerStatus,
  QueuedSteer,
  answersComplete,
  questionsKey,
  clearDraft,
  clearSendIntent,
  draftTargetChanged,
  composerKey,
  dockKey,
  fieldHeight,
  composerContext,
  ComposerPlace,
  placeholderKey,
  primaryAction,
  readDraft,
  readDraftTarget,
  sendIntent,
  writeDraft,
  writeDraftTarget,
  type DraftTarget,
} from "./composer";
import { fmtInt } from "./ui/components";
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
  onSend: (text: string, files: File[], intent: "send" | "steer" | "queue", clientMessageId?: string, onProgress?: (fraction: number) => void) => Promise<"sent" | "steered" | "queued">;
  onStop: () => void;
  commands: SlashCommand[];
  /** Installed skills, offered in the same palette: picking one asks the agent to use it. */
  skills?: SkillEntry[];
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
  targetProject?: string;
  targetWorkspace?: string;
  /** What the empty field says while nothing runs, when the conversation is with someone in particular:
   *  "Write to the orchestrator…". Running and waiting keep their own words. */
  idlePlaceholder?: string;
  context?: { tokens: number; window: number; messages: number } | null;
  onContext?: () => void;
  asr?: AsrStatus | null;
  /** The voice page, when the installation has one. */
  steers: QueuedSteer[];
  onWithdraw?: (steer: QueuedSteer) => void;
  questions?: Question[] | null;
  onAnswer?: (answers: Answer[]) => Promise<void>;
  approval?: Approval | null;
  onApprove?: (a: Approval) => void;
  /** What "Allow similar" grants for this request (``npm test*``), when the host offers a family. */
  similar?: string | null;
  onApproveSimilar?: (a: Approval) => void;
  onDeny?: (a: Approval) => void;
  /** A file waiting in the pill, opened before it goes. */
  onPreviewFile?: (file: File) => void;
  phone: boolean;
  toast: (text: string) => void;
  /** Lines over the field that belong to the conversation rather than to the draft (the orchestrator's
   *  goal line, the quote a message answers): inside the composer so they take the field's width at
   *  every size instead of copying its padding rule by rule. */
  above?: ReactNode;
};

export const Composer = forwardRef<ComposerHandle, ComposerProps>(function Composer(props, ref) {
  const { sessionId, status, onSend, onStop, commands, onCommand, phone, toast } = props;
  const replyTarget = useReply(sessionId);
  const [draft, setDraftState] = useState(() => readDraft(sessionId));
  const [savedTarget, setSavedTarget] = useState<DraftTarget | null>(() => readDraftTarget(sessionId));
  const [intent, setIntent] = useState<"send" | "steer" | "queue">(() => readDraftTarget(sessionId)?.intent ?? "send");
  const [sendOpen, setSendOpen] = useState(false);
  const [files, setFiles] = useState<File[]>([]);
  const [fileReadySession, setFileReadySession] = useState<string | null>(null);
  const [fileStorageError, setFileStorageError] = useState(false);
  const [sending, setSending] = useState(false);
  const [progress, setProgress] = useState<number | null>(null);
  const [modelOpen, setModelOpen] = useState(false);
  const [plusOpen, setPlusOpen] = useState(false);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const photoInput = useRef<HTMLInputElement>(null);
  const plusButton = useRef<HTMLButtonElement>(null);
  const sendButton = useRef<HTMLButtonElement>(null);
  const dock = useRef<HTMLDivElement>(null);

  // The draft is the session's: leaving and coming back finds it, another session does not.
  useEffect(() => {
    setDraftState(readDraft(sessionId));
    const saved = readDraftTarget(sessionId);
    setSavedTarget(saved);
    setIntent(saved?.intent ?? "send");
    setFileReadySession(null);
    setFileStorageError(false);
    let active = true;
    void loadDraftFiles(sessionId).then((saved) => {
      if (!active) return;
      setFiles(saved);
      setFileReadySession(sessionId);
    }).catch(() => {
      if (!active) return;
      setFiles([]);
      setFileStorageError(true);
      setFileReadySession(sessionId);
    });
    return () => { active = false; };
  }, [sessionId]);
  const currentTarget: DraftTarget = { session: sessionId, project: props.targetProject ?? "", workspace: props.targetWorkspace ?? "", model: props.model, mode: props.mode ?? "", effort: props.reasoningEffort ?? "", reply: replyTarget ? `${replyTarget.seq}:${replyTarget.excerpt}` : "" };
  const needsReview = draftTargetChanged(savedTarget, currentTarget);
  const rememberTarget = (nextIntent = intent) => {
    const target = { ...(savedTarget ?? currentTarget), intent: nextIntent };
    setSavedTarget(target);
    writeDraftTarget(sessionId, target);
  };
  useEffect(() => {
    if (fileReadySession !== sessionId) return;
    void saveDraftFiles(sessionId, files).then(() => setFileStorageError(false)).catch(() => setFileStorageError(true));
  }, [sessionId, fileReadySession, files]);
  useEffect(() => {
    if (fileReadySession === sessionId && !draft.trim() && files.length === 0) {
      setSavedTarget(null);
      writeDraftTarget(sessionId, null);
    }
  }, [sessionId, fileReadySession, draft, files]);
  const setDraft = useCallback(
    (next: string) => {
      setDraftState(next);
      writeDraft(sessionId, next);
      if (next.trim() || files.length) rememberTarget();
      else { setSavedTarget(null); writeDraftTarget(sessionId, null); }
    },
    [sessionId, files, savedTarget, currentTarget, intent],
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
    if (named.length) {
      rememberTarget();
      setFiles((p) => [...p, ...named]);
    }
  }, [rememberTarget]);

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
  const skillItems = useMemo(() => paletteQuery === null ? [] : (props.skills ?? []).filter((s) => s.name.toLowerCase().includes(paletteQuery)).slice(0, Math.max(0, 8 - paletteItems.length)),
    [props.skills, paletteQuery, paletteItems.length]);
  // The run is told in words which skill to load; the Skill tool does the loading, so no new command exists.
  const pickSkill = (s: SkillEntry) => {
    setDraft(t("composer.skill.use", { name: s.name }));
    textarea.current?.focus();
  };
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
  // A fresh set of blanks for new questions only; the same questions read again keep what was picked.
  const asked = questionsKey(questions);
  useEffect(() => {
    setAnswers((questions ?? []).map(() => ({ selected: [], custom: "" })));
    // `asked` stands for `questions`: the array is new on every read of the session.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [asked]);
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
  async function send(override?: string, selectedIntent = intent) {
    const text = (override ?? draft).trim();
    const going = files;
    if (sending || fileReadySession !== sessionId || (!text && going.length === 0)) return;
    if (needsReview || (status === "running" ? selectedIntent === "send" : selectedIntent !== "send")) return;
    if (text.startsWith("/") && going.length === 0 && commands.some((c) => c.name === text.slice(1).split(" ")[0].toLowerCase())) {
      await runCommand(text);
      return;
    }
    setSending(true);
    const fingerprint = JSON.stringify({ text, files: going.map((file) => [file.name, file.size, file.lastModified, file.type]), target: currentTarget, intent: selectedIntent });
    const clientMessageId = sendIntent(sessionId, fingerprint);
    // Keep the complete draft until the server confirms it. A dropped reply may be an unknown
    // outcome; the same client message id is used if the operator explicitly retries.
    if (going.length) setProgress(0);
    try {
      const delivery = await onSend(text, going, selectedIntent, clientMessageId, going.length ? setProgress : undefined);
      clearSendIntent(sessionId);
      setDraftState("");
      clearDraft(sessionId);
      setFiles([]);
      setSavedTarget(null);
      writeDraftTarget(sessionId, null);
      if (fileInput.current) fileInput.current.value = "";
      haptic("light");
      toast(t(`composer.delivery.${delivery}`));
    } catch (e) {
      // A conflict has an authoritative answer from the host. The next deliberate send is a new
      // attempt; an unconfirmed network failure keeps its original identity for safe retry.
      if (e instanceof ApiError && e.status === 409) clearSendIntent(sessionId);
      toast(errorText(e));
    } finally {
      setSending(false);
      setProgress(null);
    }
  }
  function primary() {
    if (action === "stop") onStop();
    else if (action === "reply") void reply();
    else void send();
  }
  function chooseIntent(next: "send" | "steer" | "queue") {
    setIntent(next);
    rememberTarget(next);
    setSendOpen(false);
  }

  // ── the voice note ──
  // Read at the moment the words arrive, not when the recording began: the draft may have been
  // restored from another tab or edited while a note was on its way.
  const draftNow = useRef(draft);
  draftNow.current = draft;
  const note = useVoiceNote({
    sessionId,
    asr: props.asr,
    onWords: (words, go) => {
      const landed = landWords(draftNow.current, words, go);
      if (landed.send) void send(landed.text);
      else {
        setDraft(landed.text);
        window.setTimeout(() => textarea.current?.focus(), 0);
      }
    },
    onAttach: (file) => addFiles([file]),
    toast,
  });
  const voiceBar = note.state.phase === "recording" || note.state.phase === "transcribing";

  // ── keys ──
  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.nativeEvent.isComposing || e.nativeEvent.keyCode === 229) return;
    const intent = composerKey(e, { enterSends: enterSends(), paletteOpen: paletteItems.length + skillItems.length > 0 });
    if (intent === "complete") {
      e.preventDefault();
      if (paletteItems.length) pickCommand(paletteItems[0]);
      else pickSkill(skillItems[0]);
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
      if (typing) return;
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
  const primaryLabel = action === "stop" ? t("session.stop") : action === "reply" ? t("composer.reply") : t(intent === "queue" ? "composer.queue" : intent === "steer" ? "composer.steer" : "session.send");
  const intentFits = status === "running" ? intent !== "send" : intent === "send";
  const place = composerContext(props.place);

  return (
    <div className="composer" data-primary={action}>
      {props.above}
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
            {props.similar && props.onApproveSimilar && (
              <button type="button" className="btn small" data-action="allow-similar" title={t("composer.approve.similar.title")} onClick={() => props.onApproveSimilar?.(approval)}>
                {t("composer.approve.similar", { label: props.similar })}
              </button>
            )}
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
      {paletteItems.length + skillItems.length > 0 && (
        <div className="palette" role="listbox">
          {paletteItems.slice(0, 8).map((c) => (
            <button key={c.name} type="button" role="option" aria-selected={false} className="palette-item" onClick={() => pickCommand(c)}>
              <span className="mono">/{c.name} <span className="sub">{c.args}</span></span>
              <span className="sub">{c.description}</span>
            </button>
          ))}
          {skillItems.map((s) => (
            <button key={`skill:${s.id}`} type="button" role="option" aria-selected={false} className="palette-item palette-skill" onClick={() => pickSkill(s)}>
              <span className="mono">{s.name} <span className="sub">{t("composer.skill.kind")}</span></span>
              <span className="sub">{s.description}</span>
            </button>
          ))}
        </div>
      )}
      <VoiceNoteFailed note={note} />
      <div className={`composer-box ${voiceBar ? "voicing" : ""}`}>
        {voiceBar && <VoiceBar note={note} />}
        {!phone && !voiceBar && place.length > 0 && (
          <div className="composer-place" aria-label={t("composer.place")}>
            {place.map((chip) => <span key={chip.kind} className="composer-place-chip" title={t(`composer.place.${chip.kind}`, { name: chip.name })}>
              <Icon name="folder" size={12} /><span className="truncate">{chip.name}</span>
            </span>)}
          </div>
        )}
        {progress !== null && <div className="sub upload-progress" role="status">{t("upload.progress", { percent: Math.floor(progress * 100) })}</div>}
        {fileStorageError && files.length > 0 && <div className="sub upload-progress" role="status">{t("composer.attachments.unsaved")}</div>}
        {fileReadySession !== sessionId && <div className="sub upload-progress" role="status">{t("composer.attachments.restoring")}</div>}
        {needsReview && <div className="sub upload-progress" role="status">{t("composer.draft.targetChanged")} <button type="button" className="btn small" onClick={() => { const next = { ...currentTarget, intent }; setSavedTarget(next); writeDraftTarget(sessionId, next); clearSendIntent(sessionId); }}>{t("composer.draft.useCurrent")}</button></div>}
        {files.length > 0 && !voiceBar && (
          <div className="attachments" aria-label={t("session.attachments")}>
            {files.map((f, i) => (
              <AttachmentCard key={`${f.name}-${f.size}-${f.lastModified}-${i}`} file={f} onOpen={() => props.onPreviewFile?.(f)} onRemove={() => { if (!sending) setFiles((p) => p.filter((_, j) => j !== i)); }} />
            ))}
          </div>
        )}
        <textarea
          hidden={voiceBar}
          ref={textarea}
          value={draft}
          disabled={sending || fileReadySession !== sessionId}
          onChange={(e) => setDraft(e.target.value)}
          placeholder={props.idlePlaceholder && placeholderKey(status, asking) === "session.composer.idle" ? props.idlePlaceholder : t(placeholderKey(status, asking))}
          rows={1}
          onPaste={onPaste}
          onKeyDown={onKeyDown}
          aria-label={props.idlePlaceholder && placeholderKey(status, asking) === "session.composer.idle" ? props.idlePlaceholder : t(placeholderKey(status, asking))}
        />
        <div className="composer-row" hidden={voiceBar}>
          <input ref={fileInput} type="file" multiple hidden onChange={(e) => { addFiles(e.target.files ?? []); e.target.value = ""; }} />
          <input ref={photoInput} type="file" accept="image/*" capture="environment" hidden onChange={(e) => { addFiles(e.target.files ?? []); e.target.value = ""; }} />
          <button ref={plusButton} type="button" className={`iconbtn flat plus ${plusOpen ? "on" : ""}`} disabled={sending || fileReadySession !== sessionId} onClick={() => setPlusOpen((o) => !o)} aria-label={t("composer.plus")} title={t("composer.plus")} aria-haspopup="menu" aria-expanded={plusOpen}>
            <Icon name="plus" />
          </button>
          {plusOpen && (
            <Popover anchor={plusButton.current} onClose={() => setPlusOpen(false)} className="plus-menu" label={t("composer.plus")}>
              <button type="button" role="menuitem" onClick={() => { setPlusOpen(false); fileInput.current?.click(); }}><Icon name="attach" size={16} />{t("session.attach")}</button>
              {phone && <button type="button" role="menuitem" onClick={() => { setPlusOpen(false); photoInput.current?.click(); }}><Icon name="image" size={16} />{t("composer.photo")}</button>}
              {props.asr?.configured && <button type="button" role="menuitem" disabled={!note.supported || note.state.phase !== "idle"} onClick={() => { setPlusOpen(false); void note.start(); }}><Icon name="mic" size={16} />{t("session.mic")}</button>}
              <button type="button" role="menuitem" onClick={() => void pasteFromClipboard()}><Icon name="copy" size={16} />{t("composer.paste")}</button>
            </Popover>
          )}
          {props.onChooseMode && props.onYagni
            ? <ModeSelect mode={props.mode ?? ""} modes={props.modes ?? []} yagni={!!props.yagni} onChooseMode={props.onChooseMode} onYagni={props.onYagni} sheet={phone} />
            : <span className="composer-mode">{t("composer.mode.agent")}</span>}
          <div className="composer-tools">
            {(!phone || status !== "running") && <ModelSelect model={props.model} fallback={props.fallback} open={modelOpen} onOpenChange={setModelOpen} onChoose={props.onChooseModel} sheet={phone}
              effort={props.reasoningEffort} thinking={props.thinking} onChooseEffort={props.onChooseEffort} />}
            {pct !== null && ctx && (
              <button type="button" className={`ctx-ring ${pct >= 90 ? "bad" : pct >= 60 ? "attn" : ""}`} onClick={props.onContext} title={t("composer.context", { pct, used: fmtTok(ctx.tokens), window: fmtTok(ctx.window), n: fmtInt(ctx.messages) })} aria-label={t("composer.context.label")}>
                <Ring pct={pct} />
                {/* The number beside the ring: a partial circle alone, at rest, read as a spinner. */}
                <span className="ctx-pct" aria-hidden>{`${pct}%`}</span>
              </button>
            )}
            {props.asr?.configured && <MicButton note={note} />}
            <button ref={sendButton} type="button" className={`roundbtn primary ${action}`} onClick={primary} disabled={!enabled || fileReadySession !== sessionId || needsReview || (action !== "stop" && action !== "reply" && !intentFits)} aria-label={primaryLabel} title={primaryLabel} data-action={action}>
              <Icon name={action === "stop" ? "stop" : action === "reply" ? "send" : "up"} />
            </button>
            {(draft.trim() || files.length > 0) && <button type="button" className="iconbtn flat" onClick={() => setSendOpen((open) => !open)} disabled={sending || fileReadySession !== sessionId} aria-label={t("composer.draft.actions")} aria-haspopup="menu" aria-expanded={sendOpen}><Icon name="chevron" size={14} /></button>}
            {sendOpen && <Popover anchor={sendButton.current} onClose={() => setSendOpen(false)} className="plus-menu" label={t("composer.draft.actions")}>
              {status === "running" ? <>
                <button type="button" role="menuitem" onClick={() => chooseIntent("steer")}>{t("composer.steer")}</button>
                <button type="button" role="menuitem" onClick={() => chooseIntent("queue")}>{t("composer.queue")}</button>
              </> : <button type="button" role="menuitem" onClick={() => chooseIntent("send")}>{t("session.send")}</button>}
            </Popover>}
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

export function AttachmentCard({ file, onOpen, onRemove }: { file: File; onOpen: () => void; onRemove: () => void }) {
  const isImage = file.type.startsWith("image/") || previewKind(file.name) === "image";
  // A video shows its first frame, as a picture does itself: a clapperboard glyph said only "video".
  const isVideo = file.type.startsWith("video/");
  const url = useMemo(() => (isImage || isVideo ? URL.createObjectURL(file) : null), [file, isImage, isVideo]);
  useEffect(() => () => { if (url) URL.revokeObjectURL(url); }, [url]);
  return (
    <div className={`attachment ${isImage || isVideo ? "image" : ""}`}>
      <button type="button" className="attachment-open" onClick={onOpen} title={canPreview(file.name) ? t("preview.open") : file.name}>
        {url && isVideo ? <video src={`${url}#t=0.1`} muted playsInline preload="metadata" aria-hidden /> : url ? <img src={url} alt={file.name} /> : <span className="attachment-glyph" aria-hidden>{fileGlyph(file.name)}</span>}
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
