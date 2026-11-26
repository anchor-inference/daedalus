// The prompt coach's window: a floating panel over the chat on a desktop, a tall sheet on a phone. It
// embeds the coach's own session (drawn by the caller, so this file does not import the session screen
// that imports it) and, once the coach has saved a prompt, offers it for the chat's message.

import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { t } from "../i18n";
import { Icon } from "../icons";
import { useQuery } from "../store";
import { errorText } from "../ui";
import { useCopied } from "../ui/components";
import { OverflowMenu, Sheet } from "../ui/dialogs";
import { Banner } from "../ui/primitives";
import { type CoachResult, type CoachView, POSITION_KEY, seedMessage } from "./coach";
import { FloatingWindow } from "../ui/primitives/floatingwindow";

export type PromptCoachProps = {
  parentId: string;
  /** The chat's draft when the button was pressed: the coach's starting prompt, or nothing. */
  draft: string;
  /** Changes each time the button is pressed while the window is open, which brings it back up. */
  raise: number;
  phone: boolean;
  toast: (text: string) => void;
  onClose: () => void;
  /** Put the prompt into the chat's message, replacing the draft. */
  onInsert: (prompt: string) => void;
  /** Open the saved file in the chat's Files panel, by its path in the chat's workspace. */
  onOpenFile?: (path: string) => void;
  /** The coach's session, drawn by the screen that owns the window. */
  chat: (sessionId: string) => ReactNode;
};

export function PromptCoach({ parentId, draft, raise, phone, toast, onClose, onInsert, onOpenFile, chat }: PromptCoachProps) {
  const [coach, setCoach] = useState<CoachView | null>(null);
  const [error, setError] = useState("");
  const [minimised, setMinimised] = useState(false);
  const draftAtOpen = useRef(draft);
  const path = `/api/sessions/${encodeURIComponent(parentId)}/coach`;
  const start = useCallback(async (fresh: boolean) => {
    setError("");
    if (fresh) setCoach(null);
    try {
      const opened = await api.post<{ coach: CoachView }>(path, { message: seedMessage(draftAtOpen.current), fresh });
      setCoach(opened.coach);
    } catch (e) {
      setError(errorText(e));
    }
  }, [path]);
  useEffect(() => { void start(false); }, [start]);
  useEffect(() => { if (raise) setMinimised(false); }, [raise]);
  // The result arrives with the coach's SavePrompt, written into its session by the host; a read every
  // few seconds while the window is open is all it takes, and survives a stream that reconnected.
  const polled = useQuery<{ coach: CoachView | null }>(coach ? path : null, { pollMs: 2500, staleMs: 0 });
  const shown = polled.data?.coach && polled.data.coach.session_id === coach?.session_id ? polled.data.coach : coach;
  const result = shown?.result ?? null;

  const insert = (prompt: string) => {
    onInsert(prompt);
    if (phone) onClose();
  };
  const menu = (
    <OverflowMenu small className="coach-menu" label={t("coach.menu")} items={[{ label: t("coach.restart"), icon: "reload", onSelect: () => void start(true) }]} />
  );
  const body = (
    <div className="coach-body">
      {result && <ResultCard result={result} phone={phone} onInsert={insert} toast={toast} onOpenFile={onOpenFile ? (file) => { if (phone) onClose(); onOpenFile(file); } : undefined} />}
      <div className="coach-chat">
        {error ? <div className="coach-note"><Banner tone="bad" icon="alert">{t("coach.failed", { error })}</Banner></div>
          : shown ? chat(shown.session_id)
          : <div className="coach-note sub" role="status">{t("coach.starting")}</div>}
      </div>
    </div>
  );
  if (phone) {
    return (
      <Sheet size="full" title={t("coach.title")} head={menu} onClose={onClose} className="ph-sheet coach-sheet">
        {body}
      </Sheet>
    );
  }
  return <FloatingWindow title={t("coach.title")} icon="wand" positionKey={POSITION_KEY} minimised={minimised} onMinimise={() => setMinimised((m) => !m)} onClose={onClose} menu={menu}>{body}</FloatingWindow>;
}

/** The saved prompt: its first lines, the way into the message, a copy, and where it was saved. */
function ResultCard({ result, phone, onInsert, toast, onOpenFile }: { result: CoachResult; phone: boolean; onInsert: (prompt: string) => void; toast: (text: string) => void; onOpenFile?: (path: string) => void }) {
  const [open, setOpen] = useState(false);
  const [copied, copyPrompt] = useCopied();
  const copy = async () => { if (!(await copyPrompt(result.prompt))) toast(t("coach.copy.failed")); };
  return (
    <section className="coach-result" aria-label={t("coach.ready")}>
      <div className="coach-result-head">
        <Icon name="check" size={16} />
        <span className="coach-result-title truncate">{result.title || t("coach.ready")}</span>
        {onOpenFile
          ? <button type="button" className="coach-path truncate" onClick={() => onOpenFile(result.path)} title={t("coach.saved", { path: result.path })}>{result.path}</button>
          : <span className="coach-path truncate sub" title={t("coach.saved", { path: result.path })}>{result.path}</span>}
      </div>
      {/* The first lines, which open to the whole prompt on a press: a third button for that did not fit
          beside Copy and Insert in Russian, and the text itself is where the eye already is. */}
      <pre className={`coach-prompt ${open ? "open" : ""}`} role="button" tabIndex={0} aria-expanded={open} title={t(open ? "coach.less" : "coach.more")}
        onClick={() => setOpen((o) => !o)} onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setOpen((o) => !o); } }}>{result.prompt}</pre>
      <div className="coach-result-actions">
        <button type="button" className={`btn ${phone ? "" : "small"} coach-copy`} onClick={() => void copy()}><Icon name={copied ? "check" : "copy"} size={16} />{t("coach.copy")}</button>
        <button type="button" className={`btn primary ${phone ? "" : "small"} coach-insert`} onClick={() => onInsert(result.prompt)}><Icon name="send" size={16} />{t("coach.insert")}</button>
      </div>
    </section>
  );
}
