// Quick Chat: the box the desktop's global shortcut opens over whatever the operator is doing.
//
// It is the app's own page (/app/quick) in a small frameless window of the desktop application, so it
// is signed in by the same cookie as the main window and sends through the same requests: words to
// the companion's endpoint, files through the ordinary upload, dictation through the host's speech
// recognition. `@` attaches a conversation, a project or a file as context, `$` asks for a skill,
// the chip at the top picks where Enter sends. Esc hides the window; the draft stays for next time.
// The shell's voice shortcut opens the box already dictating, and its area appshot attaches a
// rectangle dragged on the screen.

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { api, ApiError, type AsrStatus, type Project, type SessionList, type SkillEntry } from "../api";
import { Icon } from "../icons";
import { t, useLang } from "../i18n";
import { sessionPath } from "../router";
import { invalidate, useQuery } from "../store";
import { errorText } from "../ui";
import { MicButton, VoiceBar, VoiceNoteFailed, useVoiceNote } from "../voicebar";
import { landWords } from "../voicenote";
import type { AppshotResult, WindowChoice } from "./bridge";
import {
  appshotError,
  appshotFile,
  composeMessage,
  contextKey,
  contextLabel,
  defaultTarget,
  delivery,
  lastSession,
  mentionChoices,
  projectForNew,
  removeTrigger,
  skillChoices,
  trigger,
  type Companion,
  type ContextItem,
  type TargetKind,
  type Trigger,
} from "./quick";
import "./quick.css";

type Attached = { file: File; url: string };
type Picker = "target" | "windows" | null;

const bridge = () => window.daedalus?.quick;

export function QuickChat() {
  useLang();
  const [authed, setAuthed] = useState<boolean | null>(null);
  const [text, setText] = useState("");
  const [caret, setCaret] = useState(0);
  const [companion, setCompanion] = useState<Companion | null | undefined>(undefined);
  const [target, setTarget] = useState<TargetKind | null>(null);
  const [context, setContext] = useState<ContextItem[]>([]);
  const [skill, setSkill] = useState<SkillEntry | null>(null);
  const [files, setFiles] = useState<Attached[]>([]);
  const [preview, setPreview] = useState<string | null>(null);
  const [picker, setPicker] = useState<Picker>(null);
  const [windows, setWindows] = useState<WindowChoice[] | null>(null);
  const [highlight, setHighlight] = useState(0);
  const [fileHits, setFileHits] = useState<{ path: string; session: string; sessionTitle: string }[]>([]);
  const [sending, setSending] = useState(false);
  const [capturing, setCapturing] = useState(false);
  const [sent, setSent] = useState<{ to: string; path: string } | null>(null);
  const [error, setError] = useState("");
  // A refused appshot on a Mac offers the way to the permission, not only the sentence.
  const [needsPermission, setNeedsPermission] = useState(false);
  // The `@` or `$` the operator closed with Esc: the list stays shut until the text changes.
  const [dismissed, setDismissed] = useState<number | null>(null);
  const root = useRef<HTMLDivElement>(null);
  const field = useRef<HTMLTextAreaElement>(null);

  const sessions = useQuery<SessionList>(authed ? "/api/sessions" : null, { staleMs: 5000 });
  const projects = useQuery<Project[]>(authed ? "/api/projects" : null, { staleMs: 15000 });
  const skills = useQuery<SkillEntry[]>(authed ? "/api/skills" : null, { staleMs: 60000 });
  const asr = useQuery<AsrStatus>(authed ? "/api/asr" : null, { staleMs: 60000 });
  const list = sessions.data?.sessions ?? [];
  const last = useMemo(() => lastSession(list, companion?.session_id ?? ""), [list, companion]);
  const shown = target ?? (companion === undefined || sessions.loading ? null : defaultTarget(!!companion, last));

  useEffect(() => {
    api.get("/api/auth/me").then(() => setAuthed(true)).catch((e) => setAuthed(e instanceof ApiError && (e.status === 401 || e.status === 403) ? false : true));
  }, []);

  // An installation without the companion answers 404, and the companion is then no target at all.
  const loadCompanion = useCallback(() => {
    api.get<Companion>("/api/companion").then(setCompanion).catch(() => setCompanion(null));
  }, []);
  useEffect(() => {
    if (authed) loadCompanion();
  }, [authed, loadCompanion]);
  // A target that went away (the companion was removed, the last chat archived) falls back.
  useEffect(() => {
    if ((target === "companion" && companion === null) || (target === "last" && !last && !sessions.loading)) setTarget(null);
  }, [target, companion, last, sessions.loading]);

  const take = useCallback((shots: AppshotResult[]) => {
    for (const shot of shots) {
      // Esc over the area overlay: the operator changed their mind, and an error line would scold them.
      if (!shot.ok && shot.error === "dismissed") continue;
      if (shot.ok) {
        const file = appshotFile(shot);
        setFiles((now) => [...now, { file, url: URL.createObjectURL(file) }]);
        setError("");
        setNeedsPermission(false);
      } else {
        setError(appshotError(shot.error, shot.detail));
        setNeedsPermission(shot.error === "permission");
      }
    }
  }, []);

  // Each press of the voice shortcut since the page last looked; acted on once dictation's status is known.
  const [voiceWanted, setVoiceWanted] = useState(0);
  // The request is collected rather than read off quick:shown: a press that opened the window for the
  // first time sent the event before this page was listening for it.
  const collectVoice = useCallback((quick: NonNullable<ReturnType<typeof bridge>>) => {
    void quick.voice?.().then((asked) => { if (asked) setVoiceWanted((n) => n + 1); }).catch(() => undefined);
  }, []);

  const focus = () => window.setTimeout(() => field.current?.focus(), 0);
  useEffect(() => {
    const quick = bridge();
    if (!quick) return;
    void quick.pending().then(take).catch(() => undefined);
    collectVoice(quick);
    return quick.onShown(() => {
      setSent(null);
      invalidate("/api/sessions");
      loadCompanion();
      void quick.pending().then(take).catch(() => undefined);
      collectVoice(quick);
      focus();
    });
  }, [take, loadCompanion, collectVoice]);
  useEffect(() => void focus(), [authed]);

  // The window is as tall as what it shows: the box alone, or the box with a list open.
  useLayoutEffect(() => {
    const quick = bridge();
    const el = root.current;
    if (!quick || !el || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => quick.resize(Math.ceil(el.getBoundingClientRect().height)));
    observer.observe(el);
    return () => observer.disconnect();
  });

  const typed = picker ? null : trigger(text, caret);
  const found: Trigger | null = typed && typed.start !== dismissed ? typed : null;
  const mentions = useMemo(() => (found?.char === "@" ? mentionChoices(found.query, list, projects.data ?? [], fileHits) : []), [found?.char, found?.query, list, projects.data, fileHits]);
  const skillHits = useMemo(() => (found?.char === "$" ? skillChoices(found.query, skills.data ?? []) : []), [found?.char, found?.query, skills.data]);
  const options = found?.char === "@" ? mentions.length : found?.char === "$" ? skillHits.length : 0;
  useEffect(() => setHighlight(0), [found?.char, found?.query]);

  // Files are searched in the last conversation's folder, the one place a quick question is likely
  // to be about; two letters at least, and a pause in the typing.
  useEffect(() => {
    const query = found?.char === "@" ? found.query.trim() : "";
    if (query.length < 2 || !last) {
      setFileHits([]);
      return;
    }
    const timer = window.setTimeout(() => {
      api.get<{ results: { path: string; kind: string }[] }>(`/api/sessions/${encodeURIComponent(last.id)}/files/search?q=${encodeURIComponent(query)}&limit=4`)
        .then((r) => setFileHits(r.results.filter((hit) => hit.kind !== "dir").map((hit) => ({ path: hit.path, session: last.id, sessionTitle: last.title }))))
        .catch(() => setFileHits([]));
    }, 200);
    return () => window.clearTimeout(timer);
  }, [found?.char, found?.query, last]);

  const textNow = useRef(text);
  textNow.current = text;
  const note = useVoiceNote({
    sessionId: "",
    asr: asr.data,
    onWords: (words, go) => {
      const landed = landWords(textNow.current, words, go);
      if (landed.send) void send(landed.text);
      else {
        setText(landed.text);
        setCaret(landed.text.length);
        focus();
      }
    },
    onAttach: (file) => setFiles((now) => [...now, { file, url: "" }]),
    toast: setError,
  });
  const recording = note.state.phase === "recording" || note.state.phase === "transcribing";

  // The voice shortcut starts dictation, and stops it when pressed while the box is listening — the
  // same key both ways, so the hands stay where they were. It waits for the host to say whether it
  // can transcribe; a host that cannot is told plainly rather than the press being ignored.
  const noteNow = useRef(note);
  noteNow.current = note;
  useEffect(() => {
    if (!voiceWanted || authed === null) return;
    if (authed && asr.data === undefined && !asr.error) return;
    setVoiceWanted(0);
    if (!authed) return;
    if (!asr.data?.configured) {
      setError(t("quick.voice.unavailable"));
      return;
    }
    const current = noteNow.current;
    if (current.state.phase === "recording") current.stop(false);
    else if (current.state.phase === "idle") current.start();
  }, [voiceWanted, authed, asr.data, asr.error]);

  function edit(value: string, at: number) {
    setText(value);
    setCaret(at);
    setDismissed(null);
    if (sent) setSent(null);
  }

  // The caret after a completion is put in place in the same frame the text changes: set a moment
  // later, it landed after the first letters typed next and split the word.
  const placeCaret = useRef<number | null>(null);
  // The field grows with what is typed, up to a few lines, and the window grows with it.
  useLayoutEffect(() => {
    const el = field.current;
    if (!el) return;
    if (placeCaret.current !== null) {
      el.focus();
      el.setSelectionRange(placeCaret.current, placeCaret.current);
      placeCaret.current = null;
    }
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
  }, [text, recording]);

  function complete(item: ContextItem | SkillEntry) {
    if (!found) return;
    const next = removeTrigger(text, found, caret);
    edit(next.text, next.caret);
    if ("description" in item) setSkill(item);
    else setContext((now) => (now.some((x) => contextKey(x) === contextKey(item)) ? now : [...now, item]));
    placeCaret.current = next.caret;
  }

  function hide() {
    setPicker(null);
    bridge()?.hide();
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.nativeEvent.isComposing || e.nativeEvent.keyCode === 229) return;
    if (options && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
      e.preventDefault();
      setHighlight((h) => (h + (e.key === "ArrowDown" ? 1 : options - 1)) % options);
      return;
    }
    if (options && (e.key === "Enter" || e.key === "Tab") && !e.shiftKey) {
      e.preventDefault();
      complete(found?.char === "@" ? mentions[highlight] : skillHits[highlight]);
      return;
    }
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void send();
    }
  }

  // Esc closes what is open first — a list, the large preview — and hides the window last.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || e.defaultPrevented) return;
      e.preventDefault();
      if (preview) setPreview(null);
      else if (picker) setPicker(null);
      else if (found && options) setDismissed(found.start);
      else hide();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  async function appshot(windowId?: string) {
    const quick = bridge();
    if (!quick || capturing) return;
    setPicker(null);
    setCapturing(true);
    try {
      const shot = await quick.capture(windowId);
      if (shot) take([shot]);
    } finally {
      setCapturing(false);
      focus();
    }
  }

  async function appshotRegion() {
    const quick = bridge();
    if (!quick?.region || capturing) return;
    setPicker(null);
    setCapturing(true);
    try {
      const shot = await quick.region();
      if (shot) take([shot]);
    } finally {
      setCapturing(false);
      focus();
    }
  }

  async function chooseWindow() {
    const quick = bridge();
    if (!quick) return;
    setWindows(null);
    setPicker("windows");
    const answer = await quick.windows();
    if (!answer) return setPicker(null);
    if (!answer.ok) {
      setPicker(null);
      setError(appshotError(answer.error, answer.detail));
      return;
    }
    setWindows(answer.windows);
  }

  function removeFile(index: number) {
    setFiles((now) => {
      const gone = now[index];
      if (gone?.url) URL.revokeObjectURL(gone.url);
      return now.filter((_, i) => i !== index);
    });
  }

  function targetLabel(kind: TargetKind): string {
    if (kind === "companion") return companion?.name || t("quick.target.companion");
    if (kind === "last") return last?.title || t("quick.target.last");
    return t("quick.target.new");
  }

  async function send(override?: string) {
    const words = (override ?? text).trim();
    if (sending || !shown || (!words && !files.length)) return;
    const plan = delivery(shown, files.length, companion ?? null, last);
    if (!plan) return setError(t("quick.error.target"));
    const body = composeMessage(words, skill, context);
    const to = targetLabel(shown);
    setSending(true);
    setError("");
    try {
      let session = "";
      if (plan.kind === "companion") {
        const answer = await api.post<{ ok: boolean; session_id: string }>("/api/companion/message", { text: body, source: "quick_chat" });
        session = answer.session_id || companion?.session_id || "";
      } else {
        session = plan.kind === "new"
          ? (await api.post<{ id: string }>("/api/sessions", { autotitle: true, title: words.slice(0, 80) || t("quick.title.appshot"), project_id: projectForNew(context) })).id
          : plan.session;
        const clientMessageId = crypto.randomUUID();
        if (files.length) {
          const form = new FormData();
          form.append("text", body);
          form.append("client_message_id", clientMessageId);
          form.append("follow_up", "true");
          for (const item of files) form.append("files", item.file, item.file.name);
          await api.upload(`/api/sessions/${encodeURIComponent(session)}/upload`, form);
        } else {
          await api.post(`/api/sessions/${encodeURIComponent(session)}/messages`, { text: body, follow_up: true, client_message_id: clientMessageId });
        }
      }
      for (const item of files) if (item.url) URL.revokeObjectURL(item.url);
      setText("");
      setCaret(0);
      setFiles([]);
      setContext([]);
      setSkill(null);
      setSent({ to, path: session ? sessionPath(session) : "" });
      invalidate("/api/sessions");
    } catch (e) {
      setError(errorText(e));
    } finally {
      setSending(false);
      focus();
    }
  }

  function open(path: string) {
    const quick = bridge();
    if (quick) quick.open(path);
    else window.location.assign(path);
  }

  if (authed === false) {
    return (
      <div className="quick" ref={root}>
        <div className="quick-signedout" role="alert">
          <span>{t("quick.signedout")}</span>
          <button type="button" className="btn small primary" onClick={() => open("/app/")}>{t("quick.open.app")}</button>
        </div>
      </div>
    );
  }

  const targets: TargetKind[] = [...(companion ? ["companion" as const] : []), ...(last ? ["last" as const] : []), "new"];
  const canSend = !!shown && !sending && (!!text.trim() || files.length > 0);
  const hasBridge = !!bridge();
  const hasRegion = !!bridge()?.region;

  return (
    <div className="quick" ref={root}>
      <div className="quick-top">
        <button type="button" className="quick-target" aria-haspopup="listbox" aria-expanded={picker === "target"} onClick={() => setPicker(picker === "target" ? null : "target")}
          title={t("quick.target.title")} disabled={!shown}>
          <Icon name={shown === "companion" ? "bots" : shown === "new" ? "compose" : "inbox"} size={14} />
          <span className="quick-target-name">{shown ? targetLabel(shown) : t("common.loading")}</span>
          <Icon name="down" size={12} />
        </button>
        {companion?.paused && shown === "companion" && <span className="quick-paused">{t("quick.companion.paused")}</span>}
        <span className="quick-hint" aria-hidden>{t("quick.hint")}</span>
      </div>
      {picker === "target" && (
        <div className="quick-list" role="listbox" aria-label={t("quick.target.title")}>
          {targets.map((kind) => (
            <button key={kind} type="button" role="option" aria-selected={kind === shown} className={`quick-option ${kind === shown ? "on" : ""}`}
              onClick={() => { setTarget(kind); setPicker(null); focus(); }}>
              <span className="quick-option-kind">{t(`quick.target.${kind}`)}</span>
              {kind !== "new" && <span className="quick-option-sub">{targetLabel(kind)}</span>}
            </button>
          ))}
        </div>
      )}
      {(context.length > 0 || skill || files.length > 0) && (
        <div className="quick-chips" aria-label={t("quick.context.label")}>
          {skill && (
            <span className="quick-chip skill"><Icon name="skill" size={12} />{skill.name}
              <button type="button" onClick={() => setSkill(null)} aria-label={t("common.remove")}><Icon name="close" size={12} /></button>
            </span>
          )}
          {context.map((item) => (
            <span key={contextKey(item)} className={`quick-chip ${item.kind}`}>
              <Icon name={item.kind === "session" ? "inbox" : item.kind === "project" ? "folder" : "file"} size={12} />{contextLabel(item)}
              <button type="button" onClick={() => setContext((now) => now.filter((x) => contextKey(x) !== contextKey(item)))} aria-label={t("common.remove")}><Icon name="close" size={12} /></button>
            </span>
          ))}
          {files.map((item, index) => (
            <span key={`${item.file.name}:${index}`} className="quick-chip image">
              {item.url ? (
                <button type="button" className="quick-thumb" onClick={() => setPreview(preview === item.url ? null : item.url)} title={t("quick.appshot.preview")}>
                  <img src={item.url} alt={item.file.name} />
                </button>
              ) : <Icon name="attach" size={12} />}
              <span className="quick-chip-name">{item.file.name}</span>
              <button type="button" onClick={() => removeFile(index)} aria-label={t("common.remove")}><Icon name="close" size={12} /></button>
            </span>
          ))}
        </div>
      )}
      {preview && <button type="button" className="quick-preview" onClick={() => setPreview(null)} title={t("quick.appshot.preview.close")}><img src={preview} alt="" /></button>}
      <VoiceNoteFailed note={note} />
      <div className="quick-box">
        {recording ? <VoiceBar note={note} /> : (
          <textarea
            ref={field}
            className="quick-field"
            rows={1}
            value={text}
            placeholder={t("quick.placeholder")}
            aria-label={t("quick.placeholder")}
            onChange={(e) => edit(e.target.value, e.target.selectionStart ?? e.target.value.length)}
            onSelect={(e) => setCaret(e.currentTarget.selectionStart ?? 0)}
            onKeyDown={onKeyDown}
          />
        )}
        <div className="quick-actions">
          {asr.data?.configured && !recording && <MicButton note={note} />}
          {hasBridge && !recording && (
            <>
              <button type="button" className="iconbtn flat quick-appshot" onClick={() => void appshot()} disabled={capturing} title={t("quick.appshot.screen")} aria-label={t("quick.appshot.screen")}>
                <Icon name="camera" />
              </button>
              {hasRegion && (
                <button type="button" className="iconbtn flat small quick-appshot-region" onClick={() => void appshotRegion()} disabled={capturing} title={t("quick.appshot.region")} aria-label={t("quick.appshot.region")}>
                  <Icon name="region" size={16} />
                </button>
              )}
              <button type="button" className="iconbtn flat small quick-appshot-window" onClick={() => void chooseWindow()} disabled={capturing} title={t("quick.appshot.window")} aria-label={t("quick.appshot.window")}>
                <Icon name="apps" size={16} />
              </button>
            </>
          )}
          {!recording && (
            <button type="button" className="roundbtn primary quick-send" onClick={() => void send()} disabled={!canSend} title={t("quick.send")} aria-label={t("quick.send")}>
              <Icon name="up" />
            </button>
          )}
        </div>
      </div>
      {found && options > 0 && (
        <div className="quick-list" role="listbox" aria-label={t(found.char === "@" ? "quick.mention.title" : "quick.skill.title")}>
          {found.char === "@"
            ? mentions.map((item, index) => (
              <button key={contextKey(item)} type="button" role="option" aria-selected={index === highlight} className={`quick-option ${index === highlight ? "on" : ""}`}
                onMouseDown={(e) => e.preventDefault()} onClick={() => complete(item)}>
                <span className="quick-option-kind">{t(`quick.mention.${item.kind}`)}</span>
                <span className="quick-option-sub">{contextLabel(item)}</span>
              </button>
            ))
            : skillHits.map((item, index) => (
              <button key={item.id} type="button" role="option" aria-selected={index === highlight} className={`quick-option ${index === highlight ? "on" : ""}`}
                onMouseDown={(e) => e.preventDefault()} onClick={() => complete(item)}>
                <span className="quick-option-kind mono">{item.name}</span>
                <span className="quick-option-sub">{item.description}</span>
              </button>
            ))}
        </div>
      )}
      {picker === "windows" && (
        <div className="quick-windows" role="listbox" aria-label={t("quick.appshot.window")}>
          {windows === null ? <div className="sub">{t("common.loading")}</div>
            : windows.length === 0 ? <div className="sub">{t("quick.appshot.nowindows")}</div>
              : windows.map((item) => (
                <button key={item.id} type="button" role="option" aria-selected={false} className="quick-window" onClick={() => void appshot(item.id)} title={item.name}>
                  <img src={item.thumbnail} alt="" />
                  <span>{item.name}</span>
                </button>
              ))}
        </div>
      )}
      {(sent || error || sending) && (
        <div className={`quick-status ${error ? "bad" : ""}`} role="status">
          {sending ? <span>{t("quick.sending")}</span>
            : error ? (
              <>
                <span className="quick-status-text">{error}</span>
                {needsPermission && window.daedalus?.desk && <button type="button" className="btn small" onClick={() => window.daedalus?.desk?.openScreenSettings()}>{t("quick.appshot.permission.open")}</button>}
              </>
            )
              : sent && (
                <>
                  <Icon name="check" size={14} />
                  <span className="quick-status-text">{t("quick.sent", { to: sent.to })}</span>
                  {sent.path && <button type="button" className="btn small" onClick={() => open(sent.path)}>{t("quick.open")}</button>}
                </>
              )}
        </div>
      )}
    </div>
  );
}
