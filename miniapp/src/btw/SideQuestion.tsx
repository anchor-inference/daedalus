// The side question's window (/btw): a floating panel over the chat on a desktop, a tall sheet on a
// phone, the same frame as the prompt coach's. It asks the host for a side question about the chat and
// embeds the session that answers it (drawn by the caller, so this file does not import the session
// screen that imports it): the answer streams and renders there, and its composer takes follow-ups.
// The chat's own run goes on untouched, and nothing here reaches its conversation.

import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { FloatingWindow } from "../coach/PromptCoach";
import { t } from "../i18n";
import { enterSends, errorText } from "../ui";
import { OverflowMenu, Sheet, useLayer } from "../ui/dialogs";
import { Banner } from "../ui/phone";
import type { BtwView } from "./btw";

export type SideQuestionProps = {
  parentId: string;
  /** What was asked after /btw, or nothing from the button (the window opens on its empty field). */
  question: string;
  /** Changes with every /btw and every press of the button: a question starts a new side question,
   *  and either brings a minimised window back up. */
  ask: number;
  phone: boolean;
  onClose: () => void;
  /** The side question's session, drawn by the screen that owns the window. */
  chat: (sessionId: string) => ReactNode;
};

export function SideQuestion({ parentId, question, ask, phone, onClose, chat }: SideQuestionProps) {
  const [current, setCurrent] = useState<BtwView | null>(null);
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  const [draft, setDraft] = useState("");
  const [minimised, setMinimised] = useState(false);
  const path = `/api/sessions/${encodeURIComponent(parentId)}/btw`;
  const start = useCallback(async (text: string) => {
    setError("");
    setPending(true);
    setCurrent(null);
    try {
      const opened = await api.post<{ btw: BtwView }>(path, { question: text });
      setCurrent(opened.btw);
      setDraft("");
    } catch (e) {
      setError(errorText(e));
      setDraft(text);
    } finally {
      setPending(false);
    }
  }, [path]);
  const asked = useRef(question);
  asked.current = question;
  useEffect(() => {
    setMinimised(false);
    if (asked.current.trim()) void start(asked.current.trim());
  }, [ask, start]);
  // Closing the window, or leaving the chat, ends the side question: the host deletes its session and
  // stops an answer still being written, so nothing is left running that no window shows.
  useEffect(() => () => { void api.delete(path).catch(() => undefined); }, [path]);

  const restart = () => {
    void api.delete(path).catch(() => undefined);
    setCurrent(null);
    setError("");
  };
  const submit = () => {
    const text = draft.trim();
    if (text && !pending) void start(text);
  };
  const menu = current
    ? <OverflowMenu small className="coach-menu" label={t("btw.menu")} items={[{ label: t("btw.new"), icon: "plus", onSelect: restart }]} />
    : null;
  const form = (
    <form className="btw-ask" onSubmit={(e) => { e.preventDefault(); submit(); }}>
      <textarea className="field" value={draft} rows={3} autoFocus maxLength={20000} placeholder={t("btw.placeholder")} aria-label={t("btw.placeholder")}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing && enterSends()) { e.preventDefault(); submit(); }
        }} />
      <p className="sub btw-note">{t("btw.note")}</p>
      <div className="btw-actions">
        <button type="submit" className="btn primary" disabled={!draft.trim() || pending}>{t("btw.ask")}</button>
      </div>
    </form>
  );
  const body = (
    <div className="coach-body btw-body">
      {error && <div className="coach-note"><Banner tone="bad" icon="alert">{t("btw.failed", { error })}</Banner></div>}
      {current ? <div className="coach-chat">{chat(current.session_id)}</div>
        : pending ? <div className="coach-note sub" role="status">{t("btw.asking")}</div>
        : form}
    </div>
  );
  if (phone) {
    return (
      <Sheet size="full" title={t("btw.title")} head={menu} onClose={onClose} className="ph-sheet coach-sheet btw-sheet">
        {body}
      </Sheet>
    );
  }
  return (
    <FloatingWindow title={t("btw.title")} icon="ask" positionKey="daedalus.btw.position" className="btw-window" minimised={minimised} onMinimise={() => setMinimised((m) => !m)} onClose={onClose} menu={menu}>
      <EscapeCloses onClose={onClose} />
      {body}
    </FloatingWindow>
  );
}

/**
 * Escape closes the floating window as it closes a sheet: through the layer stack, so only the top layer
 * hears it and the key is marked handled. The chat's own handlers (the composer's blur, the list's lens)
 * see a handled key and leave it alone — one press must not close the window and do something else.
 */
function EscapeCloses({ onClose }: { onClose: () => void }) {
  useLayer(onClose);
  return null;
}
