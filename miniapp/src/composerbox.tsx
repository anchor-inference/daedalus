// A growing full-width field above one row of controls. Drafts belong to the session;
// actions are supplied by the parent so the card also works inside the voice page.

import { forwardRef, type ReactNode, useCallback, useEffect, useImperativeHandle, useLayoutEffect, useMemo, useRef, useState } from "react";
import { api, ApiError, AsrStatus, AutopilotStatus, ModelFallback, PendingAnswered, Question, SkillEntry, SlashCommand } from "./api";
import { clipboardImages, nameIncoming, pastedFiles } from "./attachfiles";
import { Popover } from "./ui/dialogs";
import { Banner, BottomSheet, SheetRow } from "./ui/phone";
import { useOffline } from "./store";
import { Icon } from "./icons";
import { fileGlyph, previewKind, canPreview } from "./preview";
import { enterSends, errorText, fmtBytes, fmtTok, haptic } from "./ui";
import { ModelChoice, ModelSelect } from "./modelselect";
import { TogglesSelect } from "./toggles";
import { MicButton, VoiceBar, VoiceNoteFailed, useVoiceNote } from "./voicebar";
import { landWords } from "./voicenote";
import { MeetingCard, MeetingOffer, isAudio, useMeeting } from "./meeting";
import { loadDraftFiles, saveDraftFiles } from "./project/draftfiles";
import { useReply } from "./project/chat";
import { AttachedSecret, SecretChip, SecretSheet } from "./secrets";
import {
  Approval,
  ComposerStatus,
  QueuedMessage,
  questionsKey,
  clearDraft,
  clearSendIntent,
  draftTargetChanged,
  composerKey,
  dockKey,
  fieldHeight,
  composerContext,
  ComposerPlace,
  answeredTitle,
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
import { raiseFromError } from "./settinglink";
import { AskCard, type AskCardHandle } from "./questions/AskCard";
import { btwCommand, btwQuestion } from "./btw/btw";
import type { Answer } from "./questions/ask";

export type { Answer } from "./questions/ask";

export type ComposerHandle = {
  focus: () => void;
  /** Put text into the field, after whatever is there. `focus: false` leaves the keyboard where it is:
   *  a phone's panel sheet covers the field, and focusing it there raised a keyboard under the sheet. */
  insert: (text: string, focus?: boolean) => void;
  /** Attach files from outside the pill: a drop on the conversation. */
  addFiles: (files: Iterable<File>) => void;
  /** Open the model list, from wherever "change the model" is offered. */
  openModel: () => void;
  /** Put text in the field instead of whatever is there: the prompt coach's prompt replaces the draft it improved. */
  replace: (text: string) => void;
};

export type ComposerProps = {
  sessionId: string;
  status: ComposerStatus;
  /** Send the text and the files; with `queue` (a run is on) the host holds it for the end of the turn. Rejects on failure. */
  onSend: (text: string, files: File[], queue: boolean, clientMessageId?: string, onProgress?: (fraction: number) => void, secrets?: string[]) => Promise<"sent" | "queued">;
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
  /** The operator's likely next message, shown in the empty field and taken with Tab; empty for none. */
  suggestion?: string;
  /** The suggestion went into the field: the screen stops offering it. */
  onSuggestionUsed?: () => void;
  /** The session's YAGNI switch; absent where the composer offers no switches and names the agent only. */
  yagni?: boolean;
  onYagni?: (on: boolean) => void;
  /** The session's autopilot switch, beside YAGNI in the toggles menu. */
  autopilot?: AutopilotStatus | null;
  onAutopilot?: (on: boolean) => void;
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
  /** What the host holds for the running agent, drawn as cards above the field. */
  queued: QueuedMessage[];
  onWithdraw?: (message: QueuedMessage) => void;
  /** Hand a waiting follow-up to the run now, before its next model call. */
  onSteer?: (message: QueuedMessage) => void;
  questions?: Question[] | null;
  onAnswer?: (answers: Answer[]) => Promise<void>;
  /** The waiting question was settled elsewhere: drawn as answered, with who and what, in place of
   *  options whose send could only be refused. */
  answered?: PendingAnswered | null;
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
  /** A control of the conversation's own beside the mode chip: the approval mode of a conversation in
   *  the operator's Chrome. */
  beside?: ReactNode;
  /** Open the prompt coach with the draft as it stands; absent where there is no coach (inside one). */
  onCoach?: (draft: string) => void;
  /** Ask a side question about the chat (`/btw`, or the helpers' menu): the question, or "" to open its
   *  window on an empty field. Never a message to the chat, so never a steer while a run is on. */
  onBtw?: (question: string) => void;
};

export const Composer = forwardRef<ComposerHandle, ComposerProps>(function Composer(props, ref) {
  const { sessionId, status, onSend, onStop, commands, onCommand, phone, toast } = props;
  const replyTarget = useReply(sessionId);
  const [draft, setDraftState] = useState(() => readDraft(sessionId));
  const [savedTarget, setSavedTarget] = useState<DraftTarget | null>(() => readDraftTarget(sessionId));
  const [files, setFiles] = useState<File[]>([]);
  const [fileReadySession, setFileReadySession] = useState<string | null>(null);
  const [fileStorageError, setFileStorageError] = useState(false);
  const [sending, setSending] = useState(false);
  const [progress, setProgress] = useState<number | null>(null);
  const [modelOpen, setModelOpen] = useState(false);
  const [plusOpen, setPlusOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  // The operator's secrets on this draft, by name: the value went to the host when the form was sent.
  const [secrets, setSecrets] = useState<AttachedSecret[]>([]);
  const [secretOpen, setSecretOpen] = useState(false);
  const focus = useComposerFocus(phone);
  const focused = focus.focused;
  const offline = useOffline();
  const textarea = useRef<HTMLTextAreaElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const photoInput = useRef<HTMLInputElement>(null);
  const photosInput = useRef<HTMLInputElement>(null);
  const plusButton = useRef<HTMLButtonElement>(null);
  const helpButton = useRef<HTMLButtonElement>(null);
  const dock = useRef<HTMLDivElement>(null);

  // The draft is the session's: leaving and coming back finds it, another session does not.
  useEffect(() => {
    setDraftState(readDraft(sessionId));
    setSavedTarget(readDraftTarget(sessionId));
    setSecrets([]);
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
  const currentTarget: DraftTarget = { session: sessionId, project: props.targetProject ?? "", workspace: props.targetWorkspace ?? "", model: props.model, effort: props.reasoningEffort ?? "", reply: replyTarget ? `${replyTarget.seq}:${replyTarget.excerpt}` : "" };
  const needsReview = draftTargetChanged(savedTarget, currentTarget);
  const rememberTarget = () => {
    const target = savedTarget ?? currentTarget;
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
    [sessionId, files, savedTarget, currentTarget],
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

  // A secret this draft made goes when its chip does: nothing else asked for it. One attached again from
  // the chat's list stays where it was.
  async function unattachSecret(secret: AttachedSecret) {
    setSecrets((list) => list.filter((other) => other.id !== secret.id));
    if (!secret.fresh) return;
    try {
      await api.delete(`/api/secrets/${secret.id}`);
    } catch (e) {
      toast(errorText(e));
    }
  }

  const addFiles = useCallback((incoming: Iterable<File>) => {
    const named = nameIncoming(incoming);
    if (named.length) {
      rememberTarget();
      setFiles((p) => [...p, ...named]);
    }
  }, [rememberTarget]);

  useImperativeHandle(
    ref,
    () => ({
      focus: () => textarea.current?.focus(),
      insert: (text, focus = true) => {
        setDraft(draft.trim() ? `${draft.trimEnd()}\n\n${text}` : text);
        if (focus) textarea.current?.focus();
      },
      addFiles,
      openModel: () => setModelOpen(true),
      replace: (text) => {
        setDraft(text);
        textarea.current?.focus();
      },
    }),
    [draft, setDraft, addFiles],
  );

  // ── the slash palette ──
  const paletteQuery = draft.startsWith("/") && !draft.includes("\n") && !draft.includes(" ") ? draft.slice(1).toLowerCase() : null;
  // The app's own /btw beside the host's commands: it is answered here, by the side question's window.
  const offered = useMemo(() => (props.onBtw ? [btwCommand(), ...commands.filter((c) => c.name !== "btw")] : commands), [commands, props.onBtw]);
  const paletteItems = useMemo(() => (paletteQuery === null ? [] : offered.filter((c) => c.name.startsWith(paletteQuery))), [offered, paletteQuery]);
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
      // An error that names its setting goes to the corner with the button to the row.
      if (!raiseFromError(e)) toast(errorText(e));
    }
  }

  // ── the agent's question ──
  const answered = props.answered ?? null;
  const questions = answered ? null : props.questions ?? null;
  const asking = !!questions && questions.length > 0;
  const ask = useRef<AskCardHandle>(null);
  // The Reply circle does what the card's own button does: the next question, the review, or the send.
  function reply() {
    ask.current?.go();
  }

  // ── the side question ──
  // Taken before anything else a send would do: while a run is on, a sent line becomes a steer, and a
  // side question must never reach the run it is asked beside. Files and secrets stay in the pill for
  // the next real message.
  function sideQuestion(text: string): boolean {
    const question = props.onBtw ? btwQuestion(text) : null;
    if (question === null) return false;
    setDraft("");
    props.onBtw!(question);
    return true;
  }

  // ── send / stop / queue ──
  const { action, enabled } = primaryAction({ status, hasDraft: !!draft.trim(), hasFiles: files.length > 0 || secrets.length > 0, asking, sending });
  async function send(override?: string) {
    const text = (override ?? draft).trim();
    const going = files;
    const handed = secrets.map((secret) => secret.name);
    if (sending || fileReadySession !== sessionId || (!text && going.length === 0 && handed.length === 0)) return;
    if (sideQuestion(text)) return;
    const queue = status === "running";
    if (text.startsWith("/") && going.length === 0 && commands.some((c) => c.name === text.slice(1).split(" ")[0].toLowerCase())) {
      await runCommand(text);
      return;
    }
    setSending(true);
    const fingerprint = JSON.stringify({ text, files: going.map((file) => [file.name, file.size, file.lastModified, file.type]), secrets: handed, target: currentTarget, queue });
    const clientMessageId = sendIntent(sessionId, fingerprint);
    // Keep the complete draft until the server confirms it. A dropped reply may be an unknown
    // outcome; the same client message id is used if the operator explicitly retries.
    if (going.length) setProgress(0);
    try {
      const delivery = await onSend(text, going, queue, clientMessageId, going.length ? setProgress : undefined, handed.length ? handed : undefined);
      clearSendIntent(sessionId);
      setDraftState("");
      clearDraft(sessionId);
      setFiles([]);
      setSecrets([]);
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
    if (sideQuestion(draft)) return;
    if (action === "stop") onStop();
    else if (action === "reply") void reply();
    else void send();
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
    // A long recording the operator turned into a meeting: notes, not dictation. On the start page
    // there is no session to keep them in yet, so the recording is attached instead.
    onMeeting: sessionId ? (file) => void meeting.start(file, false) : undefined,
    toast,
  });
  const meeting = useMeeting(sessionId, toast);
  // An audio file in the pill can be meeting notes instead of an attachment; the first one is offered.
  const meetingFile = sessionId && !meeting.state ? files.find(isAudio) : undefined;
  const startMeeting = (keep: boolean) => {
    if (!meetingFile) return;
    setFiles((current) => current.filter((file) => file !== meetingFile));
    void meeting.start(meetingFile, keep);
  };
  const voiceBar = note.state.phase === "recording" || note.state.phase === "transcribing";

  // The operator's likely next message, as the field's placeholder while it is empty and nothing
  // else is asked of them. Never sent by itself: Tab (or the hint, on a phone) puts it in the field,
  // and typing anything replaces it.
  const ghost = props.suggestion && draft === "" && files.length === 0 && !voiceBar && !asking && !props.approval && (status === "idle" || status === "done" || status === "failed") ? props.suggestion : "";
  const acceptGhost = () => {
    if (!ghost) return;
    setDraft(ghost);
    props.onSuggestionUsed?.();
    requestAnimationFrame(() => {
      const field = textarea.current;
      if (field) { field.focus({ preventScroll:true }); field.setSelectionRange(ghost.length, ghost.length); }
    });
  };

  // ── keys ──
  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.nativeEvent.isComposing || e.nativeEvent.keyCode === 229) return;
    const intent = composerKey(e, { enterSends: enterSends(), paletteOpen: paletteItems.length + skillItems.length > 0, ghost: !!ghost });
    if (intent === "accept") {
      e.preventDefault();
      acceptGhost();
    } else if (intent === "complete") {
      e.preventDefault();
      if (paletteItems.length) pickCommand(paletteItems[0]);
      else pickSkill(skillItems[0]);
    } else if (intent === "escape") {
      // A layer above the chat (the side question's window, a sheet) took this press already.
      if (e.defaultPrevented) return;
      if (paletteQuery !== null) {
        e.preventDefault();
        setDraft("");
      } else if (draft === "") e.currentTarget.blur();
    } else if (intent === "send") {
      e.preventDefault();
      if (sideQuestion(draft)) return;
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
  // The agent's ask for the operator's Chrome is a switch of this conversation, not a call let through
  // once: it names that Chrome and carries the consent summary the panel's first switch shows.
  const approvalTitle = !approval ? "" : approval.chrome ? t("composer.chrome.title", { name: approval.chrome.name }) : t("composer.approval.title", { tool: approval.tool || "tool" });
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
    const pasted = pastedFiles(e.clipboardData);
    if (!pasted.length) return;
    // Text pasted alongside (rich-text editors add an HTML rendering of the image) is not wanted.
    e.preventDefault();
    addFiles(pasted);
    haptic("light");
  }
  async function pasteFromClipboard() {
    setPlusOpen(false);
    try {
      const got = await clipboardImages();
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
  const primaryLabel = action === "stop" ? t("session.stop") : action === "reply" ? t("composer.reply") : action === "queue" ? t("composer.queue") : t("session.send");
  const place = composerContext(props.place);

  // A phone draws the composer in two shapes (ui/phone.css): one 48 px row at rest, and the text over
  // a toolbar once the field has the reader or holds anything. The white circle is Send, greyed while
  // the field is empty, and Stop while a run is on. It used to open Voice mode while the field was
  // empty; that is a beta with a chat of its own, and an ordinary chat offers only dictation.
  const empty = !draft.trim() && files.length === 0 && secrets.length === 0;
  // A question or a permission request does not open the field: they sit in the tray above it, and
  // the field stays the one 48 px row the reader can answer in with words of their own.
  const shape = !phone ? undefined : focused || !empty || progress !== null || needsReview ? "open" : "idle";
  const useCurrent = () => { setSavedTarget(currentTarget); writeDraftTarget(sessionId, currentTarget); clearSendIntent(sessionId); };
  const moved = draftChange(savedTarget, currentTarget);

  return (
    <div className="composer" data-primary={action} data-shape={shape} data-typed={phone && draft.trim() ? "" : undefined}
      onFocus={phone ? focus.onFocus : undefined}
      onBlur={phone ? focus.onBlur : undefined}>
      {props.above}
      {phone && needsReview && (
        <Banner tone="warn" icon="alert" sub={moved} action={<button type="button" className="ph-btn sm" onClick={useCurrent}>{t("composer.draft.useCurrent")}</button>}>
          {t("composer.draft.targetChanged")}
        </Banner>
      )}
      {phone && fileStorageError && files.length > 0 && <Banner tone="bad" icon="alert">{t("composer.attachments.unsaved")}</Banner>}
      {props.queued.length > 0 && (
        <div className="steers" aria-label={t("composer.steers")}>
          {phone && <div className="ph-tray-head"><Icon name="clock" size={14} /><span>{t("composer.tray.queued", { n: props.queued.length })}</span></div>}
          {props.queued.map((s) => (
            <div key={s.id} className="steer" data-steer={s.id} data-kind={s.kind}>
              <Icon name="forward" size={14} />
              <span className="steer-text clamp-2">
                {s.files.length > 0 && <span className="steer-files" title={t("composer.queued.files", { names: s.files.join(", ") })}><Icon name="attach" size={12} />{s.files.join(", ")}</span>}
                {s.text}
              </span>
              <span className="steer-hint sub">{t(s.kind === "follow_up" ? "composer.queued.hint" : "composer.steered.hint")}</span>
              {s.kind === "follow_up" && props.onSteer && status === "running" && (
                <button type="button" className="btn small steer-now" onClick={() => props.onSteer!(s)} title={t("composer.steer.now.title")}>{t("composer.steer.now")}</button>
              )}
              {props.onWithdraw && (
                <button type="button" className="iconbtn small steer-x" onClick={() => props.onWithdraw!(s)} aria-label={t("composer.steer.withdraw")} title={t("composer.steer.withdraw")}>
                  <Icon name="close" size={13} />
                </button>
              )}
            </div>
          ))}
        </div>
      )}
      {approval && phone && (
        // The phone's tray: the same place and shape as the queue above the field, the narrowest
        // grant first and white, the session-wide grant apart from it, and refusal in red words so a
        // tired thumb does not take it for the primary.
        <div ref={dock} className="dock approval ph-tray" role="group" aria-label={approvalTitle} data-kind={approval.chrome ? "chrome" : "policy"}>
          <div className="ph-tray-head warn"><Icon name="shield" size={14} /><span>{t("composer.tray.permission")}</span></div>
          <div className="ph-tray-title">{approvalTitle}</div>
          {approval.detail && <div className={`ph-tray-detail ${approval.chrome ? "" : "mono"}`}>{approval.detail}</div>}
          {approval.chrome && <div className="ph-tray-detail sub" data-chrome-consent>{t("uc.switch.consent.body")}</div>}
          <div className="ph-tray-actions">
            <button type="button" className="ph-btn primary" onClick={() => props.onApprove?.(approval)}>{t(approval.chrome ? "composer.chrome.allow" : "composer.approve")}</button>
            {props.similar && props.onApproveSimilar && !approval.chrome && (
              <button type="button" className="ph-btn" data-action="allow-similar" title={t("composer.approve.similar.title")} onClick={() => props.onApproveSimilar?.(approval)}>
                <span className="truncate">{t("composer.approve.similar", { label: props.similar })}</span>
              </button>
            )}
            <button type="button" className="ph-btn ph-deny" onClick={() => props.onDeny?.(approval)}>{t(approval.chrome ? "composer.chrome.deny" : "composer.deny")}</button>
          </div>
        </div>
      )}
      {approval && !phone && (
        <div ref={dock} className="dock approval" role="group" aria-label={approvalTitle} data-kind={approval.chrome ? "chrome" : "policy"}>
          <div className="dock-title">
            <Icon name={approval.chrome ? "shield" : "wrench"} size={16} />
            <span className="grow">{approvalTitle}</span>
            <kbd className="sub">{t("composer.approval.hint")}</kbd>
          </div>
          {approval.detail && (approval.chrome
            ? <div className="dock-detail">{approval.detail}</div>
            : <div className="dock-detail mono truncate" title={approval.detail}>{approval.detail}</div>)}
          {approval.chrome && <div className="dock-detail sub" data-chrome-consent>{t("uc.switch.consent.body")}</div>}
          <div className="dock-actions">
            <button type="button" className="btn small primary" onClick={() => props.onApprove?.(approval)}>{t(approval.chrome ? "composer.chrome.allow" : "composer.approve")}</button>
            {props.similar && props.onApproveSimilar && !approval.chrome && (
              <button type="button" className="btn small" data-action="allow-similar" title={t("composer.approve.similar.title")} onClick={() => props.onApproveSimilar?.(approval)}>
                {t("composer.approve.similar", { label: props.similar })}
              </button>
            )}
            <button type="button" className="btn small" onClick={() => props.onDeny?.(approval)}>{t(approval.chrome ? "composer.chrome.deny" : "composer.deny")}</button>
          </div>
        </div>
      )}
      {answered && !approval && (
        <div className="dock answered" role="status" data-answered-by={answered.by}>
          <div className="dock-title">
            <Icon name="question" size={16} />
            <span className="grow">{t(answeredTitle(answered.by))}</span>
          </div>
          {answered.text && <div className="dock-detail">{answered.text}</div>}
        </div>
      )}
      {asking && !approval && props.onAnswer && (
        <AskCard key={questionsKey(questions)} ref={ask} questions={questions!} phone={phone} onSend={props.onAnswer} toast={toast} />
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
      <MeetingCard meeting={meeting} />
      {meetingFile && !voiceBar && <MeetingOffer file={meetingFile} onStart={startMeeting} />}
      {/* A tap anywhere on the phone's pill that is not a control is a tap on its field: the field is
          36 px inside a 48 px pill, and the pill is the target the reader sees. */}
      <div className={`composer-box ${voiceBar ? "voicing" : ""}`} onClick={phone ? (e) => { if (e.target === e.currentTarget) textarea.current?.focus(); } : undefined}>
        {voiceBar && <VoiceBar note={note} />}
        {!phone && !voiceBar && place.length > 0 && (
          <div className="composer-place" aria-label={t("composer.place")}>
            {place.map((chip) => <span key={chip.kind} className="composer-place-chip" title={t(`composer.place.${chip.kind}`, { name: chip.name })}>
              <Icon name="folder" size={12} /><span className="truncate">{chip.name}</span>
            </span>)}
          </div>
        )}
        {progress !== null && <div className="sub upload-progress" role="status">{t("upload.progress", { percent: Math.floor(progress * 100) })}</div>}
        {!phone && fileStorageError && files.length > 0 && <div className="sub upload-progress" role="status">{t("composer.attachments.unsaved")}</div>}
        {fileReadySession !== sessionId && <div className="sub upload-progress" role="status">{t("composer.attachments.restoring")}</div>}
        {!phone && needsReview && <div className="sub upload-progress" role="status">{t("composer.draft.targetChanged")}{moved ? ` ${moved}.` : ""} <button type="button" className="btn small" onClick={useCurrent}>{t("composer.draft.useCurrent")}</button></div>}
        {(files.length > 0 || secrets.length > 0) && !voiceBar && (
          <div className="attachments" aria-label={t("session.attachments")}>
            {files.map((f, i) => (
              <AttachmentCard key={`${f.name}-${f.size}-${f.lastModified}-${i}`} file={f} onOpen={() => props.onPreviewFile?.(f)} onRemove={() => { if (!sending) setFiles((p) => p.filter((_, j) => j !== i)); }} />
            ))}
            {secrets.map((secret) => <SecretChip key={secret.id} name={secret.name} scope={secret.scope} onRemove={() => { if (!sending) void unattachSecret(secret); }} />)}
          </div>
        )}
        <textarea
          hidden={voiceBar}
          className={ghost ? "ghost" : undefined}
          data-suggestion={ghost || undefined}
          ref={textarea}
          value={draft}
          disabled={sending || fileReadySession !== sessionId}
          onChange={(e) => setDraft(e.target.value)}
          placeholder={ghost ? ghost : phone && approval ? t("composer.tray.instead") : props.idlePlaceholder && placeholderKey(status, asking) === "session.composer.idle" ? props.idlePlaceholder : t(placeholderKey(status, asking))}
          rows={1}
          onPaste={onPaste}
          onKeyDown={onKeyDown}
          aria-label={ghost ? t("composer.suggest.label", { text:ghost }) : props.idlePlaceholder && placeholderKey(status, asking) === "session.composer.idle" ? props.idlePlaceholder : t(placeholderKey(status, asking))}
        />
        {ghost && <button type="button" className="composer-ghost-accept" onClick={acceptGhost} title={t("composer.suggest.accept")}>
          {phone ? t("composer.suggest.use") : <kbd>Tab</kbd>}
        </button>}
        <div className="composer-row" hidden={voiceBar}>
          <input ref={fileInput} type="file" multiple hidden onChange={(e) => { addFiles(e.target.files ?? []); e.target.value = ""; }} />
          <input ref={photoInput} type="file" accept="image/*" capture="environment" hidden onChange={(e) => { addFiles(e.target.files ?? []); e.target.value = ""; }} />
          {phone && <input ref={photosInput} type="file" accept="image/*,video/*" multiple hidden onChange={(e) => { addFiles(e.target.files ?? []); e.target.value = ""; }} />}
          <button ref={plusButton} type="button" className={`iconbtn flat plus ${plusOpen ? "on" : ""}`} disabled={sending || fileReadySession !== sessionId} onClick={() => setPlusOpen((o) => !o)} aria-label={t("composer.plus")} title={t("composer.plus")} aria-haspopup="menu" aria-expanded={plusOpen}>
            <Icon name="plus" />
          </button>
          {plusOpen && phone && (
            <PlusSheet onClose={() => setPlusOpen(false)}
              onPhoto={() => photoInput.current?.click()}
              onPhotos={() => photosInput.current?.click()}
              onFiles={() => fileInput.current?.click()}
              onPaste={() => void pasteFromClipboard()}
              onRecord={props.asr?.configured && note.supported && note.state.phase === "idle" ? () => void note.start() : undefined}
              onCommands={commands.length || props.skills?.length ? () => { setDraft("/"); window.setTimeout(() => textarea.current?.focus(), 0); } : undefined}
              onSecret={() => setSecretOpen(true)}
              onCoach={props.onCoach ? () => props.onCoach!(draft) : undefined}
              onBtw={props.onBtw ? () => props.onBtw!("") : undefined}
              commandsHint={commands.length || props.skills?.length ? t("ph.plus.commands.hint", { n: commands.length + (props.skills?.length ?? 0) }) : undefined}
              place={place.map((chip) => chip.name).join(" · ") || undefined} />
          )}
          {plusOpen && !phone && (
            <Popover anchor={plusButton.current} onClose={() => setPlusOpen(false)} className="plus-menu" label={t("composer.plus")}>
              <button type="button" role="menuitem" onClick={() => { setPlusOpen(false); fileInput.current?.click(); }}><Icon name="attach" size={16} />{t("session.attach")}</button>
              {phone && <button type="button" role="menuitem" onClick={() => { setPlusOpen(false); photoInput.current?.click(); }}><Icon name="image" size={16} />{t("composer.photo")}</button>}
              {props.asr?.configured && <button type="button" role="menuitem" disabled={!note.supported || note.state.phase !== "idle"} onClick={() => { setPlusOpen(false); void note.start(); }}><Icon name="mic" size={16} />{t("session.mic")}</button>}
              <button type="button" role="menuitem" onClick={() => void pasteFromClipboard()}><Icon name="copy" size={16} />{t("composer.paste")}</button>
              <button type="button" role="menuitem" onClick={() => { setPlusOpen(false); setSecretOpen(true); }}><Icon name="lock" size={16} />{t("composer.secret")}</button>
            </Popover>
          )}
          {/* The helpers — the prompt coach and the side question — are one button beside the field on a
              desktop, a menu of two when both are there; a phone's row has no room for it, and they wait
              in the + sheet there instead. */}
          {!phone && (props.onCoach || props.onBtw) && (
            <button ref={helpButton} type="button" className={`iconbtn flat coach-open ${helpOpen ? "on" : ""}`}
              onClick={() => { if (props.onCoach && props.onBtw) setHelpOpen((o) => !o); else if (props.onCoach) props.onCoach(draft); else props.onBtw!(""); }}
              aria-label={props.onCoach && props.onBtw ? t("helpers.open") : props.onCoach ? t("coach.open") : t("btw.open")}
              title={props.onCoach && props.onBtw ? t("helpers.open") : props.onCoach ? t("coach.open") : t("btw.open")}
              aria-haspopup={props.onCoach && props.onBtw ? "menu" : undefined} aria-expanded={props.onCoach && props.onBtw ? helpOpen : undefined}>
              <Icon name="wand" />
            </button>
          )}
          {helpOpen && !phone && (
            <Popover anchor={helpButton.current} onClose={() => setHelpOpen(false)} className="plus-menu helpers-menu" label={t("helpers.open")}>
              {props.onCoach && <button type="button" role="menuitem" data-action="coach" onClick={() => { setHelpOpen(false); props.onCoach!(draft); }}><Icon name="wand" size={16} />{t("coach.open")}</button>}
              {props.onBtw && <button type="button" role="menuitem" data-action="btw" onClick={() => { setHelpOpen(false); props.onBtw!(""); }}><Icon name="ask" size={16} />{t("btw.open")}<kbd className="menu-kbd">/btw</kbd></button>}
            </Popover>
          )}
          {secretOpen && <SecretSheet sessionId={sessionId} phone={phone} attached={secrets.map((secret) => secret.name)} onClose={() => setSecretOpen(false)}
            onAttach={(secret) => setSecrets((list) => [...list.filter((other) => other.name !== secret.name), secret])} />}
          {props.onYagni
            ? <TogglesSelect yagni={!!props.yagni} onYagni={props.onYagni} autopilot={props.autopilot} onAutopilot={props.onAutopilot} sheet={phone} />
            : <span className="composer-mode">{t("composer.mode.agent")}</span>}
          {props.beside}
          {phone && offline && shape === "open" && <span className="grow ph-hint">{t("ph.offline.kept")}</span>}
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
            {/* A written pill turns the circle into Queue, and Stop must not go with it: the run is
                still on, and the shortcut alone is not a way to stop it on a phone. */}
            {action === "queue" && <button type="button" className="iconbtn flat stop-aside" onClick={onStop} aria-label={t("session.stop")} title={t("session.stop")}><Icon name="stop" size={16} /></button>}
            <button type="button" className={`roundbtn primary ${action}`} onClick={primary} disabled={!enabled || fileReadySession !== sessionId || (phone && offline && action !== "stop")} aria-label={primaryLabel} title={primaryLabel} data-action={action}>
              <Icon name={action === "stop" ? "stop" : action === "reply" ? "send" : "up"} />
            </button>
          </div>
        </div>
      </div>
    </div>
  );
});

/**
 * Whether the phone's composer has the reader, for its open shape. Leaving it closes the shape only
 * once the finger is up: closing on the blur that a press causes shrank the composer by a toolbar's
 * height under that finger, everything above it slid down, and the tap landed on whatever moved
 * into its place (the newest-message button was missed every time). And only the field opens it:
 * opened by a press on a line inside the composer (the goal line, a queued card), it grew under that
 * press and the press landed on the field instead of on the line.
 */
export function useComposerFocus(enabled: boolean) {
  const [focused, setFocused] = useState(false);
  const held = useRef(false);
  useEffect(() => {
    if (!enabled) return;
    const down = () => { held.current = true; };
    const up = () => { held.current = false; };
    window.addEventListener("pointerdown", down, true);
    window.addEventListener("pointerup", up, true);
    window.addEventListener("pointercancel", up, true);
    return () => {
      window.removeEventListener("pointerdown", down, true);
      window.removeEventListener("pointerup", up, true);
      window.removeEventListener("pointercancel", up, true);
    };
  }, [enabled]);
  const onFocus = useCallback((event: React.FocusEvent<HTMLElement>) => { if (event.target instanceof HTMLTextAreaElement) setFocused(true); }, []);
  const onBlur = useCallback((event: React.FocusEvent<HTMLElement>) => {
    const root = event.currentTarget;
    if (root.contains(event.relatedTarget as Node | null)) return;
    // A sheet the toolbar opened (the model, the mode, the +) holds the focus without leaving the
    // composer: the toolbar stays open behind it, and focus comes back to it when the sheet closes.
    const settle = () => window.setTimeout(() => {
      const active = document.activeElement;
      if (!root.contains(active) && !active?.closest(".overlay-root")) setFocused(false);
    }, 0);
    if (held.current) window.addEventListener("pointerup", settle, { once: true });
    else settle();
  }, []);
  return { focused, onFocus, onBlur };
}

/** Where the chat works now, when that moved under a draft: "Now: research". */
function draftChange(saved: DraftTarget | null, now: DraftTarget): string {
  if (!saved || (saved.workspace === now.workspace && saved.project === now.project)) return "";
  const where = now.workspace.split("/").filter(Boolean).pop() || now.project || "—";
  return t("ph.draft.workspace", { name: where });
}

/**
 * The composer's "+" on a phone: three tiles for what is attached most (a photo now, a picture from the
 * gallery, a file) and rows for the rest. Each command is offered only where the composer has it.
 */
export function PlusSheet({ onClose, onPhoto, onPhotos, onFiles, onPaste, onRecord, onCommands, commandsHint, onSecret, onCoach, onBtw, place, extra }: {
  onClose: () => void; onPhoto: () => void; onPhotos?: () => void; onFiles: () => void; onPaste?: () => void; onRecord?: () => void;
  onCommands?: () => void; commandsHint?: string; onSecret?: () => void; onCoach?: () => void; onBtw?: () => void; place?: string; extra?: ReactNode;
}) {
  const run = (fn: () => void) => () => { onClose(); fn(); };
  return (
    <BottomSheet onClose={onClose} className="ph-plus-sheet" label={t("composer.plus")}>
      <div className="ph-tiles">
        <button type="button" className="ph-tile" onClick={run(onPhoto)}><Icon name="camera" size={22} /><span>{t("composer.photo")}</span></button>
        <button type="button" className="ph-tile" onClick={run(onPhotos ?? onFiles)}><Icon name="image" size={22} /><span>{t("ph.plus.photos")}</span></button>
        <button type="button" className="ph-tile" onClick={run(onFiles)}><Icon name="attach" size={22} /><span>{t("session.attach")}</span></button>
      </div>
      {onPaste && <SheetRow icon="paste" label={t("composer.paste")} hint={t("ph.plus.paste.hint")} onClick={onPaste} />}
      {onRecord && <SheetRow icon="mic" label={t("session.mic")} onClick={run(onRecord)} />}
      {onCommands && <SheetRow icon="bolt" label={t("ph.plus.commands")} hint={commandsHint} chevron onClick={run(onCommands)} />}
      {onSecret && <SheetRow icon="lock" label={t("composer.secret")} hint={t("composer.secret.hint")} onClick={run(onSecret)} />}
      {onCoach && <SheetRow icon="wand" label={t("coach.open")} hint={t("coach.open.hint")} onClick={run(onCoach)} data={{ action: "coach" }} />}
      {onBtw && <SheetRow icon="ask" label={t("btw.open")} hint={t("btw.open.hint")} onClick={run(onBtw)} data={{ action: "btw" }} />}
      {place && <SheetRow icon="folder" label={t("ph.plus.place")} hint={t("ph.plus.place.hint")} value={place} />}
      {extra}
    </BottomSheet>
  );
}

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

export function AttachmentCard({ file, onOpen, onRemove }: { file: File; onOpen?: () => void; onRemove: () => void }) {
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
