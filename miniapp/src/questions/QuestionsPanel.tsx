// The Questions tab: everything that waits for the operator, answered at their own pace and sent
// together. A card is a request's title, its text, its options as chips and one field that is the
// answer itself or a note beside a chosen option. Half-done cards are drafts kept on this device;
// Send carries every ready one at once, and the host answers each on its own — sent, answered
// elsewhere first, or refused with what to fix. What the orchestrator adds appears at once; what it
// takes back leaves with a word, and says so when a draft of the operator's went with it.
//
// A session's own side questions are the same list beside an ordinary chat. An answer to a question
// an agent asked may bring files — pasted into the field, picked with the paperclip, or dropped on the
// card; they are this device's only until Send, and go with the batch as one multipart request.

import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";
import { api, type QuestionOutcome, type WaitingQuestion } from "../api";
import { nameIncoming, pastedFiles } from "../attachfiles";
import { AttachmentCard } from "../composerbox";
import { useEvent } from "../events";
import { absTime, relTime } from "../format";
import { plural, t } from "../i18n";
import { Icon, type IconName } from "../icons";
import { renderMarkdown } from "../md";
import { RawHtml } from "../rawhtml";
import { answeredLine } from "../main/model";
import { projectHome } from "../router";
import { go } from "../shell";
import { invalidate } from "../store";
import { isMac } from "../terminal/keys";
import { errorText } from "../ui";
import { QUESTIONS_KEY, answerPath, dismissPath, useQuestions, type QuestionScope } from "./data";
import {
  EMPTY,
  batchOf,
  choosePermission,
  draftOf,
  fateOf,
  fitDrafts,
  folds,
  isBlank,
  isPermission,
  isReady,
  isSendKey,
  leaveMs,
  leaves,
  loadDrafts,
  projectGroups,
  pruneDrafts,
  revisionOf,
  saveDrafts,
  sections,
  setText,
  stepOption,
  takesFiles,
  takesText,
  textRole,
  toggleOption,
  withFiles,
  type CardFate,
  type Draft,
  type Drafts,
  permissionChoices,
} from "./model";

type Toast = (text: string) => void;

const KIND_ICON: Record<string, IconName> = { question: "question", permission: "shield", folder: "folder", project: "conductor" };

/**
 * The tab's body. `scope` is one project (its focus mode) or all of them (the main chat, grouped by
 * project). The list itself is the host's; what this adds is the operator's drafts, and the few
 * seconds a card stays on screen after it left the list, so it never vanishes without a word.
 */
export function QuestionsPanel({ scope, toast }: { scope: QuestionScope; toast: Toast }) {
  const { questions, error } = useQuestions(scope);
  const [drafts, setDrafts] = useState<Drafts>(() => loadDrafts());
  const [fates, setFates] = useState<Record<string, CardFate>>({});
  const [ghosts, setGhosts] = useState<Record<string, WaitingQuestion>>({});
  const [fresh, setFresh] = useState<Set<string>>(() => new Set());
  const [sending, setSending] = useState(false);
  // What the operator attached to each answer so far. Files cannot be kept with the drafts on this
  // device, so a reload loses them; the words of the draft stay.
  const [files, setFiles] = useState<Record<string, File[]>>({});
  const withdrawn = useRef(new Map<string, string>());
  const sent = useRef(new Set<string>());
  const seen = useRef<Map<string, WaitingQuestion> | null>(null);
  const draftsRef = useRef(drafts);
  draftsRef.current = drafts;
  const filesRef = useRef(files);
  filesRef.current = files;
  const root = useRef<HTMLDivElement>(null);

  useEffect(() => saveDrafts(drafts), [drafts]);

  // The one place a card leaves the list without this panel sending it: taken back by the
  // orchestrator, or answered in another window. It stays a moment with the word that says which.
  const depart = useCallback((q: WaitingQuestion, fate: CardFate) => {
    setGhosts((g) => ({ ...g, [q.id]: q }));
    setFates((f) => (f[q.id] ? f : { ...f, [q.id]: fate }));
    setDrafts((d) => {
      if (!(q.id in d)) return d;
      const next = { ...d };
      delete next[q.id];
      return next;
    });
    setFiles((f) => (q.id in f ? Object.fromEntries(Object.entries(f).filter(([k]) => k !== q.id)) : f));
  }, []);

  useEffect(() => {
    if (!questions) return;
    const now = new Map(questions.map((q) => [q.id, q]));
    const before = seen.current;
    if (before) {
      const added = questions.filter((q) => !before.has(q.id)).map((q) => q.id);
      if (added.length) setFresh((s) => new Set([...s, ...added]));
      for (const [id, q] of before) {
        if (now.has(id) || sent.current.has(id)) continue;
        const reason = withdrawn.current.get(id);
        const hadDraft = !isBlank(draftsRef.current[id]) || !!filesRef.current[id]?.length;
        depart(q, reason !== undefined ? { kind: "withdrawn", reason, hadDraft, agent: q.origin === "session" } : { kind: "elsewhere" });
      }
    }
    seen.current = now;
    // A draft of a question this list no longer holds is dropped — but only a draft of this list's
    // own questions: the main chat's list must not lose a project's drafts, nor a session's, nor the reverse.
    setDrafts((d) => fitDrafts(pruneDrafts(d, now.keys(), (draft) => draftOf(scope, draft)), questions));
  }, [questions, depart, scope]);

  // Taken back by the orchestrator, or by the agent that left a side question: the event carries the
  // reason, which the card then shows.
  useEvent(["ask.answered", "ask.side"], (event) => {
    const p = event.payload ?? {};
    const ids = event.type === "ask.side" ? (p.change === "withdrawn" && Array.isArray(p.ask_ids) ? p.ask_ids.map(String) : [])
      : p.via === "withdrawn" && typeof p.request_id === "string" ? [p.request_id] : [];
    for (const id of ids) {
      withdrawn.current.set(id, String(p.reason ?? ""));
      const q = seen.current?.get(id);
      if (q) depart(q, { kind: "withdrawn", reason: String(p.reason ?? ""), hadDraft: !isBlank(draftsRef.current[q.id]) || !!filesRef.current[q.id]?.length, agent: q.origin === "session" });
    }
  });

  // A card that left is taken off after its moment on screen.
  useEffect(() => {
    const timers = Object.entries(fates)
      .filter(([, fate]) => leaves(fate))
      .map(([id, fate]) =>
        window.setTimeout(() => {
          setGhosts((g) => {
            const next = { ...g };
            delete next[id];
            return next;
          });
          setFates((f) => {
            const next = { ...f };
            delete next[id];
            return next;
          });
        }, leaveMs(fate)),
      );
    return () => timers.forEach((timer) => window.clearTimeout(timer));
  }, [fates]);

  useEffect(() => {
    if (fresh.size === 0) return;
    const timer = window.setTimeout(() => setFresh(new Set()), 1400);
    return () => window.clearTimeout(timer);
  }, [fresh]);

  const shown = useMemo(() => {
    const byId = new Map<string, WaitingQuestion>();
    for (const q of questions ?? []) byId.set(q.id, q);
    for (const [id, q] of Object.entries(ghosts)) if (!byId.has(id)) byId.set(id, q);
    return [...byId.values()];
  }, [questions, ghosts]);
  const answerable = shown.filter((q) => !fates[q.id] || fates[q.id].kind === "refused");
  const batch = batchOf(answerable, drafts, files);
  const waiting = (questions ?? []).length;

  const change = useCallback((q: WaitingQuestion, update: (d: Draft) => Draft) => {
    const id = q.id;
    setDrafts((d) => ({ ...d, [id]: { ...update(d[id] ?? EMPTY), project: q.project_id, session: q.session_id ?? null } }));
    // Editing a refused answer is fixing it: the refusal has said what it had to.
    setFates((f) => (f[id]?.kind === "refused" ? Object.fromEntries(Object.entries(f).filter(([k]) => k !== id)) : f));
  }, []);

  const clear = useCallback((id: string) => {
    setDrafts((d) => {
      const next = { ...d };
      delete next[id];
      return next;
    });
    setFiles((f) => Object.fromEntries(Object.entries(f).filter(([k]) => k !== id)));
  }, []);

  const attach = useCallback((q: WaitingQuestion, incoming: Iterable<File>) => {
    const named = nameIncoming(incoming);
    if (!named.length || !takesFiles(q)) return;
    setFiles((f) => ({ ...f, [q.id]: [...(f[q.id] ?? []), ...named] }));
    setFates((f) => (f[q.id]?.kind === "refused" ? Object.fromEntries(Object.entries(f).filter(([k]) => k !== q.id)) : f));
  }, []);

  const unattach = useCallback((id: string, index: number) => {
    setFiles((f) => {
      const left = (f[id] ?? []).filter((_, i) => i !== index);
      const next = { ...f };
      if (left.length) next[id] = left;
      else delete next[id];
      return next;
    });
  }, []);

  // The operator puts a side question aside: it leaves the list, and the agent is not woken for it.
  const putAside = useCallback(async (q: WaitingQuestion) => {
    const path = dismissPath(scope, q.id);
    if (!path) return;
    sent.current.add(q.id);
    try {
      await api.post(path);
      depart(q, { kind: "aside" });
    } catch (e) {
      sent.current.delete(q.id);
      toast(errorText(e));
    } finally {
      invalidate(QUESTIONS_KEY);
    }
  }, [scope, depart, toast]);

  const dismiss = useCallback((id: string) => {
    setGhosts((g) => Object.fromEntries(Object.entries(g).filter(([k]) => k !== id)));
    setFates((f) => Object.fromEntries(Object.entries(f).filter(([k]) => k !== id)));
  }, []);

  async function send() {
    if (sending || batch.length === 0) return;
    setSending(true);
    const byId = new Map(shown.map((q) => [q.id, q]));
    for (const item of batch) sent.current.add(item.ask_id);
    try {
      const { items, uploads } = withFiles(batch, files);
      let result: { results: QuestionOutcome[] };
      if (uploads.length) {
        const form = new FormData();
        form.append("items", JSON.stringify(items));
        for (const file of uploads) form.append("files", file, file.name);
        result = await api.upload<{ results: QuestionOutcome[] }>(answerPath(scope), form);
      } else {
        result = await api.post<{ results: QuestionOutcome[] }>(answerPath(scope), items);
      }
      const outcomes = Array.isArray(result?.results) ? result.results : [];
      let answered = 0;
      let stale = 0;
      let lost = 0;
      const failed: string[] = [];
      const nextFates: Record<string, CardFate> = {};
      for (const outcome of outcomes) {
        const fate = fateOf(outcome, answeredLine);
        nextFates[outcome.ask_id] = fate;
        if (fate.kind === "sent") answered += 1;
        if (fate.kind === "stale") stale += 1;
        if (fate.kind === "sent" && fate.failed) failed.push(fate.failed);
        if (fate.kind === "conflict" || fate.kind === "withdrawn") lost += 1;
        if (fate.kind === "refused") sent.current.delete(outcome.ask_id);
      }
      const gone = outcomes.filter((o) => nextFates[o.ask_id].kind !== "refused").map((o) => o.ask_id);
      setGhosts((g) => ({ ...g, ...Object.fromEntries(gone.map((id) => [id, byId.get(id)!]).filter(([, q]) => q)) }));
      setFates((f) => ({ ...f, ...nextFates }));
      setDrafts((d) => Object.fromEntries(Object.entries(d).filter(([id]) => !gone.includes(id))));
      setFiles((f) => Object.fromEntries(Object.entries(f).filter(([id]) => !gone.includes(id))));
      toast(stale ? t("questions.fate.stale") : failed.length ? t("focus.ask.failed", { reason: failed[0] }) : lost ? t("questions.sent.some", { n: answered, lost }) : plural("questions.sent", answered));
    } catch (e) {
      for (const item of batch) sent.current.delete(item.ask_id);
      toast(errorText(e));
    } finally {
      setSending(false);
      invalidate(QUESTIONS_KEY);
      invalidate("/api/asks");
      invalidate("/api/main");
    }
  }

  const onKeyDown = (e: ReactKeyboardEvent) => {
    if (isSendKey(e.nativeEvent)) {
      e.preventDefault();
      void send();
    }
  };

  if (!questions && error) return <div className="empty"><b>{t("questions.unavailable")}</b><div>{error}</div></div>;
  if (!questions) return <div className="questions-panel"><div className="empty">{t("common.loading")}</div></div>;

  const card = (q: WaitingQuestion) => (
    <QuestionCard
      key={q.id}
      q={q}
      draft={drafts[q.id]}
      fate={fates[q.id]}
      fresh={fresh.has(q.id)}
      files={files[q.id] ?? []}
      onChange={(update) => change(q, update)}
      onClear={() => clear(q.id)}
      onDismiss={() => dismiss(q.id)}
      onAttach={(incoming) => attach(q, incoming)}
      onUnattach={(index) => unattach(q.id, index)}
      onPutAside={q.origin === "session" ? () => void putAside(q) : undefined}
    />
  );

  return (
    <div ref={root} className="questions-panel" onKeyDown={onKeyDown} aria-busy={sending}>
      <div className="questions-list">
        {shown.length === 0 ? (
          <div className="questions-empty">
            <span className="questions-empty-mark"><Icon name="check" size={18} /></span>
            <b>{t("questions.empty")}</b>
            <span>{t(scope === "all" ? "questions.empty.all" : "sessionId" in scope ? "questions.empty.session" : "questions.empty.sub")}</span>
          </div>
        ) : scope === "all" ? (
          projectGroups(shown).map((group) => (
            <section key={group.key} className="questions-group" data-project={group.projectId ?? ""} aria-label={group.name || t("main.ask.newproject")}>
              <header className="questions-group-head">
                <Icon name="conductor" size={14} />
                {group.projectId ? (
                  <a className="truncate" href={projectHome(group.projectId)} onClick={(e) => go(e, projectHome(group.projectId!))}>{group.name}</a>
                ) : (
                  <span className="truncate">{group.name || t("main.ask.newproject")}</span>
                )}
                <span className="questions-count num">{group.items.filter((q) => !fates[q.id]).length || ""}</span>
              </header>
              {sections(group.items).flatMap((s) => s.items).map(card)}
            </section>
          ))
        ) : (
          sections(shown).map((s) => (
            <section key={s.key} className={`questions-section ${s.key}`} aria-label={t(`questions.section.${s.key}`)}>
              <header className="questions-section-head">
                <span>{t(`questions.section.${s.key}`)}</span>
                <span className="questions-count num">{s.items.filter((q) => !fates[q.id]).length || ""}</span>
              </header>
              {s.items.map(card)}
            </section>
          ))
        )}
      </div>
      <footer className={`questions-send ${batch.length ? "ready" : ""}`}>
        <span className="questions-send-info">
          {batch.length ? plural("questions.ready", batch.length, { of: waiting }) : waiting ? plural("questions.waiting", waiting) : t("questions.nothing")}
          <span className="questions-keys" title={t("questions.keys")}><kbd>{isMac() ? "⌘" : "Ctrl"}</kbd><kbd>↵</kbd></span>
        </span>
        <button className="btn primary questions-send-btn" disabled={sending || batch.length === 0} onClick={() => void send()}>
          <Icon name="send" size={14} />
          {batch.length ? t("questions.send.n", { n: batch.length }) : t("questions.send")}
        </button>
      </footer>
    </div>
  );
}

// ── one card ─────────────────────────────────────────────────────────────────────────────────

type CardProps = {
  q: WaitingQuestion;
  draft: Draft | undefined;
  fate: CardFate | undefined;
  fresh: boolean;
  files: File[];
  onChange: (update: (d: Draft) => Draft) => void;
  onClear: () => void;
  onDismiss: () => void;
  onAttach: (files: Iterable<File>) => void;
  onUnattach: (index: number) => void;
  /** Put a side question aside unanswered; absent for every other request. */
  onPutAside?: () => void;
};

/** Who asks, in a few words: a member by name and what they want, or the orchestrator. */
function askerWords(q: WaitingQuestion): string {
  if (q.origin === "dispatcher") return t("questions.asker.main");
  if (q.origin === "session") return t(q.asker === "autopilot" ? "questions.asker.autopilot" : "questions.asker.agent");
  if (q.origin === "orchestrator") return t(q.kind === "folder" ? "questions.asker.folder" : "questions.asker.orchestrator");
  return t(q.kind === "permission" ? "questions.asker.permission" : "questions.asker.staff", { name: q.asker });
}

/** The text under the title. A request without a title is headed by its first line, which is then not
 *  repeated underneath — unless the heading had to cut that line short, when the whole text follows. */
function bodyOf(q: WaitingQuestion): string {
  if (q.title) return q.text;
  const lines = q.text.split("\n");
  const first = lines.findIndex((line) => line.trim());
  if (first < 0) return "";
  if (lines[first].trim() !== (q.heading ?? "").trim()) return q.text.trim();
  return lines.slice(first + 1).join("\n").trim();
}

function QuestionCard({ q, draft, fate, fresh, files, onChange, onClear, onDismiss, onAttach, onUnattach, onPutAside }: CardProps) {
  const d = draft ?? EMPTY;
  const filesOk = takesFiles(q);
  const picker = useRef<HTMLInputElement>(null);
  const [dropping, setDropping] = useState(false);
  const body = bodyOf(q);
  const long = folds(body);
  const [open, setOpen] = useState(false);
  const html = useMemo(() => (body ? renderMarkdown(body) : ""), [body]);
  const ready = isReady(q, draft, files.length);
  const blank = isBlank(draft) && files.length === 0;
  const gone = !!fate && fate.kind !== "refused";
  const field = useRef<HTMLTextAreaElement>(null);
  const permission = isPermission(q);
  const role = textRole(q, d);
  const state = fate ? fate.kind : ready ? "ready" : blank ? "idle" : "draft";
  const revised = revisionOf(q);

  // The field grows with what is written, up to a few lines, and then scrolls.
  useEffect(() => {
    const el = field.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
  }, [d.text, role]);

  const chips: { key: string; label: string; on: boolean; tone?: string; pick: () => void }[] = permission
    ? permissionChoices(q).map((c) => ({
        key: c,
        label: c === "server" ? t("questions.perm.server", { server: q.always_server ?? "" }) : t(`questions.perm.${c}`),
        on: d.choice === c,
        tone: c === "allow" || c === "always" || c === "server" ? "ok" : "bad",
        pick: () => {
          onChange((x) => choosePermission(x, c));
          if (c === "because" && d.choice !== "because") window.setTimeout(() => field.current?.focus(), 0);
        },
      }))
    : q.options.map((o) => ({ key: o, label: o, on: d.selected.includes(o), pick: () => onChange((x) => toggleOption(q, x, o)) }));
  const multi = !permission && q.multi;
  const chipsRef = useRef<HTMLDivElement>(null);
  const [focusAt, setFocusAt] = useState(() => Math.max(0, chips.findIndex((c) => c.on)));
  const onChipKey = (e: ReactKeyboardEvent, i: number) => {
    const next = stepOption(e.key, i, chips.length);
    if (next === null) return;
    e.preventDefault();
    setFocusAt(next);
    chipsRef.current?.querySelectorAll<HTMLButtonElement>(".q-chip")[next]?.focus();
  };

  return (
    <article
      className={`q-card k-${q.kind} s-${state} ${fresh ? "fresh" : ""} ${q.host ? "host" : ""} ${q.urgent ? "urgent" : ""} ${dropping ? "dropping" : ""}`}
      data-ask={q.short_id}
      data-state={state}
      aria-label={q.heading ?? q.title ?? q.short_id}
      // A card on its way out takes no input; one that lost to an answer given elsewhere keeps its
      // Dismiss, which is all that is left to do with it.
      inert={(fate && leaves(fate)) || undefined}
      style={fate && leaves(fate) ? ({ ["--q-leave" as string]: `${leaveMs(fate)}ms` }) : undefined}
      // A file dropped on a card that takes files is that answer's; the chat around the panel would
      // otherwise take it for the composer.
      onDragOver={filesOk && !gone ? (e) => { if (e.dataTransfer?.types.includes("Files")) { e.preventDefault(); e.stopPropagation(); setDropping(true); } } : undefined}
      onDragLeave={filesOk ? () => setDropping(false) : undefined}
      onDrop={filesOk && !gone ? (e) => { if (!e.dataTransfer?.files?.length) return; e.preventDefault(); e.stopPropagation(); setDropping(false); onAttach(e.dataTransfer.files); } : undefined}
    >
      <header className="q-head">
        <span className={`q-kind k-${q.kind}`} aria-hidden><Icon name={KIND_ICON[q.kind] ?? "question"} size={14} /></span>
        <div className="q-headline">
          <h4 className="q-title">{q.heading || q.title || q.text.split("\n")[0]}</h4>
          <div className="q-meta">
            <span className="truncate">{askerWords(q)}</span>
            <span aria-hidden>·</span>
            <span className="num">{relTime(q.created_at)}</span>
            {revised && (
              <span className="q-flag updated" data-revision={revised.revision} title={t("questions.updated.title", { n: revised.revision, t: revised.at ? absTime(revised.at) : "" })}>
                {revised.at ? t("questions.updated", { t: relTime(revised.at) }) : t("questions.updated.bare")}
              </span>
            )}
            {q.urgent && <span className="q-flag urgent">{t("questions.urgent")}</span>}
            {q.host && <span className="q-flag host" title={t("main.ask.host")}><Icon name="lock" size={11} />{t("questions.host")}</span>}
          </div>
        </div>
        <code className="q-short">{q.short_id}</code>
        {onPutAside && !gone && (
          <button type="button" className="iconbtn q-aside" onClick={onPutAside} aria-label={t("questions.aside")} title={t("questions.aside.title")}>
            <Icon name="close" size={14} />
          </button>
        )}
      </header>
      {body && (
        <>
          <RawHtml className={`q-text ${long && !open ? "folded" : ""}`} html={html} />
          {long && (
            <button type="button" className="q-more" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
              {t(open ? "questions.less" : "questions.more")}
              <Icon name="chevron" size={12} />
            </button>
          )}
        </>
      )}
      {q.suggestion && <div className="q-suggestion"><Icon name="conductor" size={12} />{t("main.ask.suggests", { text: q.suggestion })}</div>}
      {!gone && chips.length > 0 && (
        <div
          ref={chipsRef}
          className="q-chips"
          role={multi ? "group" : "radiogroup"}
          aria-label={t(permission ? "questions.perm.label" : multi ? "questions.options.multi" : "questions.options")}
        >
          {chips.map((c, i) => (
            <button
              key={c.key}
              type="button"
              className={`q-chip ${c.on ? "on" : ""} ${c.tone ? `tone-${c.tone}` : ""} ${multi ? "multi" : ""}`}
              role={multi ? "checkbox" : "radio"}
              aria-checked={c.on}
              data-option={c.key}
              tabIndex={i === Math.min(focusAt, chips.length - 1) ? 0 : -1}
              onFocus={() => setFocusAt(i)}
              onKeyDown={(e) => onChipKey(e, i)}
              onClick={c.pick}
            >
              {multi && <span className="q-check" aria-hidden>{c.on && <Icon name="check" size={11} />}</span>}
              <span className="q-chip-label">{c.label}</span>
            </button>
          ))}
        </div>
      )}
      {!gone && takesText(q, d) && (
        <textarea
          ref={field}
          className={`field q-field role-${role}`}
          rows={1}
          value={d.text}
          maxLength={4000}
          placeholder={t(`questions.field.${role}`)}
          aria-label={t(`questions.field.${role}`)}
          onChange={(e) => onChange((x) => setText(x, e.target.value))}
          onPaste={filesOk ? (e) => {
            const pasted = pastedFiles(e.clipboardData);
            if (!pasted.length) return;
            // The text a rich editor puts beside a pasted image is not part of the answer.
            e.preventDefault();
            onAttach(pasted);
          } : undefined}
        />
      )}
      {!gone && filesOk && (
        <div className="q-files">
          <button type="button" className="iconbtn q-attach" onClick={() => picker.current?.click()} aria-label={t("questions.attach")} title={t("questions.attach.title")}>
            <Icon name="attach" size={16} />
          </button>
          <input ref={picker} type="file" multiple hidden onChange={(e) => { onAttach(e.target.files ?? []); e.target.value = ""; }} />
          {files.length === 0 && <span className="q-files-hint">{t("questions.attach.hint")}</span>}
          {files.map((f, i) => <AttachmentCard key={`${f.name}-${f.size}-${f.lastModified}-${i}`} file={f} onRemove={() => onUnattach(i)} />)}
        </div>
      )}
      <footer className="q-foot" aria-live="polite">
        {fate ? (
          <FateLine fate={fate} onDismiss={onDismiss} />
        ) : !blank ? (
          <>
            <span className={`q-state ${ready ? "ready" : "draft"}`}>
              <i aria-hidden />
              {t(ready ? "questions.state.ready" : "questions.state.draft")}
              {!ready && <span className="q-why">{whyNotReady(q, d)}</span>}
            </span>
            <button type="button" className="q-clear" onClick={onClear}>{t("questions.clear")}</button>
          </>
        ) : null}
      </footer>
    </article>
  );
}

/** What a draft still lacks, in a few words. */
function whyNotReady(q: WaitingQuestion, d: Draft): string {
  if (isPermission(q)) return d.choice === "because" ? t("questions.why.reason") : "";
  return "";
}

function FateLine({ fate, onDismiss }: { fate: CardFate; onDismiss: () => void }) {
  if (fate.kind === "stale") return <span className="q-fate warn"><Icon name="alert" size={13} />{t("questions.fate.stale")}</span>;
  if (fate.kind === "sent") {
    return fate.failed ? (
      <span className="q-fate bad"><Icon name="alert" size={13} />{t("focus.ask.failed", { reason: fate.failed })}</span>
    ) : (
      <span className="q-fate ok"><Icon name="check" size={13} />{t("questions.fate.sent")}</span>
    );
  }
  if (fate.kind === "withdrawn") {
    return (
      <span className="q-fate faint">
        <Icon name="undo" size={13} />
        {t(`questions.fate.withdrawn${fate.agent ? ".agent" : ""}${fate.hadDraft ? ".draft" : ""}`)}
        {fate.reason && <span className="q-reason">{fate.reason}</span>}
      </span>
    );
  }
  if (fate.kind === "elsewhere") return <span className="q-fate faint"><Icon name="check" size={13} />{t("questions.fate.elsewhere")}</span>;
  if (fate.kind === "aside") return <span className="q-fate faint"><Icon name="close" size={13} />{t("questions.fate.aside")}</span>;
  if (fate.kind === "conflict") {
    return (
      <>
        <span className="q-fate warn"><Icon name="alert" size={13} />{t("focus.ask.conflict")}{fate.line && <span className="q-reason">{fate.line}</span>}</span>
        <button type="button" className="q-clear" onClick={onDismiss}>{t("questions.dismiss")}</button>
      </>
    );
  }
  return <span className="q-fate bad"><Icon name="alert" size={13} />{fate.error}</span>;
}

/**
 * The line in the chat that stands for the list: how many wait, and the way to them. The cards
 * themselves live in the tab; drawing them in the chat as well had the operator reading every
 * question twice.
 */
export function QuestionsLine({ count, onOpen }: { count: number; onOpen: () => void }) {
  if (count <= 0) return null;
  return (
    <button type="button" className="questions-line" onClick={onOpen} data-count={count}>
      <span className="questions-line-mark"><Icon name="ask" size={14} /></span>
      <span className="questions-line-text">{plural("questions.line", count)}</span>
      <span className="questions-line-open btn small">{t("questions.open")}<Icon name="forward" size={12} /></span>
    </button>
  );
}
