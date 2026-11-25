// A growing full-width field above one row of controls. Drafts belong to the session;
// actions are supplied by the parent so the card also works inside the voice page.

import { forwardRef, type ReactNode, useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState } from "react";
import { api, ApiError, AsrStatus, AutopilotStatus, ModelFallback, PendingAnswered, Question, type SessionTranslation, SkillEntry, SlashCommand } from "./api";
import { clipboardImages, nameIncoming, pastedFiles } from "./attachfiles";
import { Banner } from "./ui/phone";
import { MenuRow, Popover, Sheet } from "./ui/primitives";
import { useOffline } from "./store";
import { Icon } from "./icons";
import { fileGlyph, previewKind, canPreview } from "./preview";
import { enterSends, errorText, fmtBytes, fmtTok, haptic } from "./ui";
import { AccountChoice, ModelChoice, ModelSelect } from "./modelselect";
import { AccessSelect } from "./toggles";
import type { AgentLangChoice } from "./agentlang";
import { ApprovalCard } from "./approvalcard";
import { MicButton, VoiceBar, VoiceNoteFailed, useVoiceNote } from "./voicebar";
import { landWords } from "./voicenote";
import { MeetingCard, MeetingOffer, isAudio, useMeeting } from "./meeting";
import { loadDraftFiles, saveDraftFiles } from "./project/draftfiles";
import { MarkupButton, markable } from "./annotate/markup";
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
import { useDropTarget } from "./filedrop";
import { CodeField, type CodeFieldHandle } from "./codefield";
import { btwCommand, btwQuestion } from "./btw/btw";
import type { Answer } from "./questions/ask";
import { clearQuoteReplies, removeQuoteReply, replyPreview, serialiseReplies, useQuoteReplies } from "./quotereply";
import { ReplyCard, ReplyDialog } from "./replyselect";

export type { Answer } from "./questions/ask";

export type ComposerHandle = {
  focus: () => void;
  /** Put text into the field, after whatever is there. `focus: false` leaves the keyboard where it is:
   *  a phone's panel sheet covers the field, and focusing it there raised a keyboard under the sheet. */
  insert: (text: string, focus?: boolean) => void;
  /** Attach files from outside the pill: a drop on the conversation. */
  addFiles: (files: Iterable<File>) => void;
  /** Open the model list, from wherever "change the model" is offered. `anchor` is the control that asked:
   *  the list hangs from it and gives the keyboard back to it. Without one it hangs from the composer's
   *  own model pill. A second call from the same control closes the list again. */
  openModel: (anchor?: HTMLElement | null) => void;
  /** Put text in the field instead of whatever is there: the prompt coach's prompt replaces the draft it improved. */
  replace: (text: string) => void;
};

export type ComposerProps = {
  sessionId: string;
  status: ComposerStatus;
  /** Send the text and the files; `queue` says a run is on, and `midRun` what the message becomes then. Rejects on failure. */
  onSend: (text: string, files: File[], queue: boolean, clientMessageId?: string, onProgress?: (fraction: number) => void, secrets?: string[]) => Promise<"sent" | "queued" | "steered">;
  /** What a message sent during a run becomes, which the button and the field say: held for the end
   *  of the turn (the app, where the card above offers to steer it instead), or read at the run's next
   *  step (the side panel, where a correction typed mid-run is what the operator means, and one that
   *  waited for the turn let six model calls go the wrong way). */
  midRun?: "queue" | "steer";
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
  /** The session's subscription account, chosen in the same picker as the effort. */
  account?: AccountChoice;
  /** The operator's likely next message, shown in the empty field and taken with Tab; empty for none. */
  suggestion?: string;
  /** The suggestion went into the field: the screen stops offering it. */
  onSuggestionUsed?: () => void;
  /** The session's YAGNI switch, in the access chip's menu; absent where the session offers no switches. */
  yagni?: boolean;
  onYagni?: (on: boolean) => void;
  /** The session's autopilot switch, beside YAGNI in the access menu. */
  autopilot?: AutopilotStatus | null;
  onAutopilot?: (on: boolean) => void;
  /** The session's "Bypass all checks": the access chip says "Full access" while it is on. */
  bypass?: boolean;
  onBypass?: (on: boolean) => void;
  /** The language the agent works in here, and the settings' default for it; a row of the access menu
   *  only where the session can be told one (`onAgentLang`). */
  translation?: SessionTranslation;
  translationDefault?: string;
  onAgentLang?: (choice: AgentLangChoice) => void;
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
  /** Take a waiting message back into the field to change it: a withdrawal whose words come back. */
  onEdit?: (message: QueuedMessage) => void;
  /** Hand a waiting follow-up to the run now, before its next model call. */
  onSteer?: (message: QueuedMessage) => void;
  questions?: Question[] | null;
  onAnswer?: (answers: Answer[]) => Promise<void>;
  /** The operator's answer to the waiting question, taken by the host and still with the translator: the
   *  card shows it as translating instead of offering the options again. */
  answerTranslating?: { text: string; to: string } | null;
  /** The waiting question was settled elsewhere: drawn as answered, with who and what, in place of
   *  options whose send could only be refused. */
  answered?: PendingAnswered | null;
  approval?: Approval | null;
  onApprove?: (a: Approval) => void;
  /** What "Allow similar" grants for this request (``npm test*``), when the host offers a family. */
  similar?: string | null;
  onApproveSimilar?: (a: Approval) => void;
  /** The MCP server "Allow all from <server>" grants for a span of this session, when the request is one of its tools. */
  server?: string | null;
  onApproveServer?: (a: Approval, hours: number) => void;
  onDeny?: (a: Approval) => void;
  /** A file waiting in the pill, opened before it goes. */
  onPreviewFile?: (file: File) => void;
  phone: boolean;
  toast: (text: string) => void;
  /** Lines over the field that belong to the conversation rather than to the draft (the orchestrator's
   *  goal line, the quote a message answers): inside the composer so they take the field's width at
   *  every size instead of copying its padding rule by rule. */
  above?: ReactNode;
  /** A control of the conversation's own beside the access chip: the approval mode of a conversation in
   *  the operator's Chrome. */
  beside?: ReactNode;
  /** Open the prompt coach with the draft as it stands; absent where there is no coach (inside one).
   *  It and the side question are the "Helpers" of the + menu. */
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
  // The control outside the composer that opened the model list, or null for the composer's own pill.
  // The panel's strip once called the bare opener and its list appeared beside the composer pill, at
  // the far side of the window from the chip that was pressed.
  const [modelAnchor, setModelAnchor] = useState<HTMLElement | null>(null);
  const [plusOpen, setPlusOpen] = useState(false);
  // The operator's secrets on this draft, by name: the value went to the host when the form was sent.
  const [secrets, setSecrets] = useState<AttachedSecret[]>([]);
  const [secretOpen, setSecretOpen] = useState(false);
  // Replies to passages of the agent's answers, added from the pill over a selection; kept per chat
  // beside the draft, and sent ahead of the words in the field.
  const replies = useQuoteReplies(sessionId);
  const [replyOpen, setReplyOpen] = useState<string | null>(null);
  const openReply = replies.find((reply) => reply.id === replyOpen) ?? null;
  const offline = useOffline();
  const textarea = useRef<CodeFieldHandle>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const photoInput = useRef<HTMLInputElement>(null);
  const photosInput = useRef<HTMLInputElement>(null);
  const plusButton = useRef<HTMLButtonElement>(null);
  const root = useRef<HTMLDivElement>(null);

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
  // A file dragged over the composer is the message's: the composer, and nothing around it, is where
  // a drop attaches to the chat. Not while the files of another conversation are still being restored.
  useDropTarget(root, fileReadySession === sessionId ? addFiles : null);

  useImperativeHandle(
    ref,
    () => ({
      focus: () => textarea.current?.focus(),
      insert: (text, focus = true) => {
        setDraft(draft.trim() ? `${draft.trimEnd()}\n\n${text}` : text);
        if (focus) textarea.current?.focus();
      },
      addFiles,
      openModel: (anchor) => {
        const from = anchor ?? null;
        if (modelOpen && modelAnchor === from) { setModelOpen(false); return; }
        setModelAnchor(from);
        setModelOpen(true);
      },
      replace: (text) => {
        setDraft(text);
        textarea.current?.focus();
      },
    }),
    [draft, setDraft, addFiles, modelOpen, modelAnchor],
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
  const { action, enabled } = primaryAction({ status, hasDraft: !!draft.trim() || replies.length > 0, hasFiles: files.length > 0 || secrets.length > 0, asking, sending });
  async function send(override?: string) {
    const text = (override ?? draft).trim();
    const going = files;
    const handed = secrets.map((secret) => secret.name);
    const quoted = replies;
    if (sending || fileReadySession !== sessionId || (!text && going.length === 0 && handed.length === 0 && quoted.length === 0)) return;
    if (sideQuestion(text)) return;
    const queue = status === "running";
    // A command is a line of its own: with replies in the pill the words are a message about them.
    if (quoted.length === 0 && text.startsWith("/") && going.length === 0 && commands.some((c) => c.name === text.slice(1).split(" ")[0].toLowerCase())) {
      await runCommand(text);
      return;
    }
    setSending(true);
    const body = serialiseReplies(quoted, text);
    const fingerprint = JSON.stringify({ text: body, files: going.map((file) => [file.name, file.size, file.lastModified, file.type]), secrets: handed, target: currentTarget, queue });
    const clientMessageId = sendIntent(sessionId, fingerprint);
    // Keep the complete draft until the server confirms it. A dropped reply may be an unknown
    // outcome; the same client message id is used if the operator explicitly retries.
    if (going.length) setProgress(0);
    try {
      await onSend(body, going, queue, clientMessageId, going.length ? setProgress : undefined, handed.length ? handed : undefined);
      clearSendIntent(sessionId);
      setDraftState("");
      clearDraft(sessionId);
      setFiles([]);
      setSecrets([]);
      clearQuoteReplies(sessionId);
      setSavedTarget(null);
      writeDraftTarget(sessionId, null);
      if (fileInput.current) fileInput.current.value = "";
      haptic("light");
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
  function onKeyDown(e: React.KeyboardEvent<HTMLDivElement>) {
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
      setModelAnchor(null);
      setModelOpen((o) => !o);
    } else if (intent === "stop") {
      if (status === "running") {
        e.preventDefault();
        onStop();
      }
    }
  }
  // ⌘M and ⌘⇧S reach the composer from anywhere on the screen; y / n answer the card when nobody is typing.
  // A call the project's rule answered from the card is put away here at once: the screen's own answers
  // mark their call answered, this one goes past them, and the card stayed until the host's next word.
  const [settled, setSettled] = useState<string | null>(null);
  const approval = props.approval && props.approval.callId !== settled ? props.approval : null;
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // Ctrl+M is Enter in a terminal and Ctrl+Shift+S nothing the composer should hear.
      if (insideTerminal(e.target)) return;
      const target = e.target as HTMLElement | null;
      const typing = !!target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable);
      if (typing && target === textarea.current?.element) return;
      if (typing) return;
      const intent = composerKey(e, { enterSends: false, paletteOpen: false });
      if (intent === "model") {
        e.preventDefault();
        setModelAnchor(null);
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
  const steers = props.midRun === "steer";
  const primaryLabel = action === "stop" ? t("session.stop") : action === "reply" ? t("composer.reply") : action === "queue" ? t(steers ? "composer.steer.send" : "composer.queue") : t("session.send");
  const fieldWords = steers && status === "running" ? t("session.composer.running.steer") : t(placeholderKey(status, asking));
  const place = composerContext(props.place);

  // A phone draws the composer as the desktop's card at a finger's sizes (ui/parts/composer.css): the
  // field over one row of 44 px controls, the same shape at rest and while typing. It used to rest as
  // one row and grow a toolbar when the field took the reader, and the composer then changed height
  // under the finger that had pressed it. The circle is Send, greyed while the field is empty, and
  // Stop while a run is on.
  const shape = phone ? "card" : undefined;
  const useCurrent = () => { setSavedTarget(currentTarget); writeDraftTarget(sessionId, currentTarget); clearSendIntent(sessionId); };
  const moved = draftChange(savedTarget, currentTarget);

  return (
    <div ref={root} className="composer" data-primary={action} data-shape={shape} data-typed={phone && draft.trim() ? "" : undefined}>
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
                {replyPreview(s.text)}
              </span>
              <span className="steer-hint sub">{t(s.kind === "follow_up" ? "composer.queued.hint" : "composer.steered.hint")}</span>
              {s.kind === "follow_up" && props.onSteer && status === "running" && (
                <button type="button" className="btn small steer-now" onClick={() => props.onSteer!(s)} title={t("composer.steer.now.title")}>{t("composer.steer.now")}</button>
              )}
              {props.onEdit && (
                <button type="button" className="iconbtn small steer-edit" onClick={() => props.onEdit!(s)} aria-label={t("composer.steer.edit")} title={t("composer.steer.edit.title")}>
                  <Icon name="pen" size={13} />
                </button>
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
      {approval && (
        <ApprovalCard sessionId={sessionId} approval={approval} phone={phone} similar={props.similar} server={props.server} toast={toast}
          onApprove={props.onApprove} onApproveSimilar={props.onApproveSimilar} onApproveServer={props.onApproveServer} onDeny={props.onDeny}
          // A refusal is where the operator says what to do instead: the field comes back with the keyboard.
          onAnswered={(how) => {
            if (how === "settled") setSettled(approval.callId);
            if (how === "deny" && !phone) window.setTimeout(() => textarea.current?.focus(), 0);
          }} />
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
        <AskCard key={questionsKey(questions)} ref={ask} questions={questions!} phone={phone} onSend={props.onAnswer} toast={toast} translateTo={props.translation?.active ? props.translation.agent : undefined} held={props.answerTranslating ?? null} onFiles={fileReadySession === sessionId ? addFiles : undefined} />
      )}
      {paletteItems.length + skillItems.length > 0 && !approval && (
        <div className="palette" role="listbox" aria-label={t("composer.palette")}>
          {paletteItems.slice(0, 8).map((c, i) => (
            <button key={c.name} type="button" role="option" aria-selected={i === 0} className={`palette-item ${i === 0 ? "active" : ""}`} onClick={() => pickCommand(c)}>
              <span className="palette-name mono">/{c.name}{c.args && <span className="sub"> {c.args}</span>}</span>
              <span className="palette-desc">{c.description}</span>
            </button>
          ))}
          {skillItems.map((s, i) => (
            <button key={`skill:${s.id}`} type="button" role="option" aria-selected={paletteItems.length === 0 && i === 0} className={`palette-item palette-skill ${paletteItems.length === 0 && i === 0 ? "active" : ""}`} onClick={() => pickSkill(s)}>
              <span className="palette-name mono">{s.name} <span className="sub">{t("composer.skill.kind")}</span></span>
              <span className="palette-desc">{s.description}</span>
            </button>
          ))}
        </div>
      )}
      <VoiceNoteFailed note={note} />
      <MeetingCard meeting={meeting} />
      {meetingFile && !voiceBar && <MeetingOffer file={meetingFile} onStart={startMeeting} />}
      {/* A tap anywhere on the phone's pill that is not a control is a tap on its field. The box stays
          mounted under an approval card, hidden, so the draft and its height survive the question. */}
      <div className={`composer-box ${voiceBar ? "voicing" : ""}`} hidden={!!approval} onClick={phone ? (e) => { if (e.target === e.currentTarget) textarea.current?.focus(); } : undefined}>
        {voiceBar && <VoiceBar note={note} />}
        <span className="drop-hint" aria-hidden="true"><Icon name="attach" size={18} />{t("composer.drop")}</span>
        {progress !== null && <div className="sub upload-progress" role="status">{t("upload.progress", { percent: Math.floor(progress * 100) })}</div>}
        {!phone && fileStorageError && files.length > 0 && <div className="sub upload-progress" role="status">{t("composer.attachments.unsaved")}</div>}
        {fileReadySession !== sessionId && <div className="sub upload-progress" role="status">{t("composer.attachments.restoring")}</div>}
        {!phone && needsReview && <div className="sub upload-progress" role="status">{t("composer.draft.targetChanged")}{moved ? ` ${moved}.` : ""} <button type="button" className="btn small" onClick={useCurrent}>{t("composer.draft.useCurrent")}</button></div>}
        {(files.length > 0 || secrets.length > 0 || replies.length > 0) && !voiceBar && (
          <div className="attachments" aria-label={t("session.attachments")}>
            {replies.map((reply) => <ReplyCard key={reply.id} reply={reply} onOpen={() => setReplyOpen(reply.id)} onRemove={() => { if (!sending) removeQuoteReply(sessionId, reply.id); }} />)}
            {files.map((f, i) => (
              <AttachmentCard key={`${f.name}-${f.size}-${f.lastModified}-${i}`} file={f} onOpen={() => props.onPreviewFile?.(f)} onRemove={() => { if (!sending) setFiles((p) => p.filter((_, j) => j !== i)); }}
                onReplace={(marked) => { if (!sending) setFiles((p) => p.map((held) => (held === f ? marked : held))); }} />
            ))}
            {secrets.map((secret) => <SecretChip key={secret.id} name={secret.name} scope={secret.scope} onRemove={() => { if (!sending) void unattachSecret(secret); }} />)}
          </div>
        )}
        <CodeField
          hidden={voiceBar}
          className={ghost ? "ghost" : undefined}
          suggestion={ghost || undefined}
          ref={textarea}
          value={draft}
          disabled={sending || fileReadySession !== sessionId}
          onChange={setDraft}
          placeholder={ghost ? ghost : props.idlePlaceholder && placeholderKey(status, asking) === "session.composer.idle" ? props.idlePlaceholder : fieldWords}
          onPaste={onPaste}
          onKeyDown={onKeyDown}
          ariaLabel={ghost ? t("composer.suggest.label", { text:ghost }) : props.idlePlaceholder && placeholderKey(status, asking) === "session.composer.idle" ? props.idlePlaceholder : fieldWords}
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
          {plusOpen && (
            // One menu at every size (a sheet on a phone): what goes into the message, then the helpers —
            // the prompt coach and the side question had a wand button of their own beside the field.
            <Popover anchor={plusButton.current} onClose={() => setPlusOpen(false)} className="attach-menu" label={t("composer.plus")}>
              <MenuRow role="menuitem" icon="attach" label={t("session.attach")} data={{ action: "files" }} onSelect={() => { setPlusOpen(false); fileInput.current?.click(); }} />
              {phone && <MenuRow role="menuitem" icon="camera" label={t("composer.photo")} data={{ action: "photo" }} onSelect={() => { setPlusOpen(false); photoInput.current?.click(); }} />}
              {phone && <MenuRow role="menuitem" icon="image" label={t("ph.plus.photos")} data={{ action: "photos" }} onSelect={() => { setPlusOpen(false); photosInput.current?.click(); }} />}
              {props.asr?.configured && <MenuRow role="menuitem" icon="mic" label={t("session.mic.menu")} disabled={!note.supported || note.state.phase !== "idle"} data={{ action: "record" }} onSelect={() => { setPlusOpen(false); void note.start(); }} />}
              <MenuRow role="menuitem" icon="paste" label={t("composer.paste")} data={{ action: "paste" }} onSelect={() => void pasteFromClipboard()} />
              <MenuRow role="menuitem" icon="lock" label={t("composer.secret")} sub={t("composer.secret.hint")} data={{ action: "secret" }} onSelect={() => { setPlusOpen(false); setSecretOpen(true); }} />
              {(commands.length > 0 || (props.skills?.length ?? 0) > 0) && <MenuRow role="menuitem" icon="bolt" label={t("ph.plus.commands")} kbd="/" data={{ action: "commands" }} onSelect={() => { setPlusOpen(false); setDraft("/"); window.setTimeout(() => textarea.current?.focus(), 0); }} />}
              {(props.onCoach || props.onBtw) && <div className="menu-group" role="presentation">{t("helpers.group")}</div>}
              {props.onCoach && <MenuRow role="menuitem" icon="wand" label={t("coach.open")} sub={t("coach.open.hint")} data={{ action: "coach" }} onSelect={() => { setPlusOpen(false); props.onCoach!(draft); }} />}
              {props.onBtw && <MenuRow role="menuitem" icon="ask" label={t("btw.open")} sub={t("btw.open.hint")} kbd="/btw" data={{ action: "btw" }} onSelect={() => { setPlusOpen(false); props.onBtw!(""); }} />}
              {place.length > 0 && <div className="menu-sep" role="separator" />}
              {place.length > 0 && <MenuRow icon="folder" label={t("ph.plus.place")} aside={place.map((chip) => chip.name).join(" · ")} data={{ place: "" }} />}
            </Popover>
          )}
          {openReply && <ReplyDialog key={openReply.id} sessionId={sessionId} reply={openReply} phone={phone} onClose={() => setReplyOpen(null)} />}
          {secretOpen && <SecretSheet sessionId={sessionId} phone={phone} attached={secrets.map((secret) => secret.name)} onClose={() => setSecretOpen(false)}
            onAttach={(secret) => setSecrets((list) => [...list.filter((other) => other.name !== secret.name), secret])}
            onChanged={(id, name) => setSecrets((list) => (name === null ? list.filter((other) => other.id !== id) : list.map((other) => (other.id === id ? { ...other, name } : other))))} />}
          <AccessSelect yagni={!!props.yagni} onYagni={props.onYagni} autopilot={props.autopilot} onAutopilot={props.onAutopilot} bypass={props.bypass} onBypass={props.onBypass}
            translation={props.translation} translationDefault={props.translationDefault} onAgentLang={props.onAgentLang} compact={phone} />
          {props.beside}
          {phone && offline && <span className="grow ph-hint">{t("ph.offline.kept")}</span>}
          <div className="composer-tools">
            {/* A running phone composer has no room for the chip, but the list still opens from the panel's
                strip: without the component mounted that chip did nothing at all while a turn ran. */}
            <ModelSelect model={props.model} fallback={props.fallback} open={modelOpen} onOpenChange={(o) => { if (o) setModelAnchor(null); setModelOpen(o); }} onChoose={props.onChooseModel}
              effort={props.reasoningEffort} thinking={props.thinking} onChooseEffort={props.onChooseEffort} account={props.account} anchor={modelAnchor} pill={!phone || status !== "running"} />
            {/* The context in use, as a number: it opens the panel at its Context section. A ring beside
                the number read as a spinner at rest, and the number alone says the same. */}
            {pct !== null && ctx && (
              <button type="button" className={`ctx-ring ${pct >= 90 ? "bad" : pct >= 60 ? "attn" : ""}`} onClick={props.onContext} disabled={!props.onContext}
                title={t("composer.context", { pct, used: fmtTok(ctx.tokens), window: fmtTok(ctx.window), n: fmtInt(ctx.messages) })} aria-label={`${t("composer.context.label")}: ${pct}%`}>
                <span className="ctx-pct">{`${pct}%`}</span>
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
 * Whether the start page's composer on a phone has the reader, for its open shape. Leaving it closes the shape only
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
  const onFocus = useCallback((event: React.FocusEvent<HTMLElement>) => { if (event.target instanceof HTMLElement && event.target.classList.contains("composer-field")) setFocused(true); }, []);
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
 * The start page's "+" on a phone: three tiles for what is attached most (a photo now, a picture from
 * the gallery, a file) and the page's own rows. The chat's composer has one + menu at every size.
 */
export function PlusSheet({ onClose, onPhoto, onPhotos, onFiles, onRecord, extra }: {
  onClose: () => void; onPhoto: () => void; onPhotos?: () => void; onFiles: () => void; onRecord?: () => void; extra?: ReactNode;
}) {
  const run = (fn: () => void) => () => { onClose(); fn(); };
  return (
    <Sheet onClose={onClose} className="ph-sheet ph-plus-sheet" ariaLabel={t("composer.plus")}>
      <div className="ph-tiles">
        <button type="button" className="ph-tile" onClick={run(onPhoto)}><Icon name="camera" size={22} /><span>{t("composer.photo")}</span></button>
        <button type="button" className="ph-tile" onClick={run(onPhotos ?? onFiles)}><Icon name="image" size={22} /><span>{t("ph.plus.photos")}</span></button>
        <button type="button" className="ph-tile" onClick={run(onFiles)}><Icon name="attach" size={22} /><span>{t("session.attach")}</span></button>
      </div>
      {onRecord && <MenuRow icon="mic" label={t("session.mic")} onSelect={run(onRecord)} />}
      {extra}
    </Sheet>
  );
}

/** `onReplace`, where the holder can take a file back, puts a pencil on a picture that opens the markup editor. */
export function AttachmentCard({ file, onOpen, onRemove, onReplace }: { file: File; onOpen?: () => void; onRemove: () => void; onReplace?: (file: File) => void }) {
  const isImage = file.type.startsWith("image/") || previewKind(file.name) === "image";
  const pencil = onReplace !== undefined && markable(file);
  // A video shows its first frame, as a picture does itself: a clapperboard glyph said only "video".
  const isVideo = file.type.startsWith("video/");
  const url = useMemo(() => (isImage || isVideo ? URL.createObjectURL(file) : null), [file, isImage, isVideo]);
  useEffect(() => () => { if (url) URL.revokeObjectURL(url); }, [url]);
  return (
    <div className={`attachment ${isImage || isVideo ? "image" : ""} ${pencil ? "markable" : ""}`}>
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
      {pencil && <MarkupButton file={file} onReplace={onReplace} />}
    </div>
  );
}
