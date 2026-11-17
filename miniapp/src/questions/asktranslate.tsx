// A question of the Questions tab in the operator's language: the translation the host keeps beside the
// agent's words (made by itself for a side question of a translated session, or on request), the card
// drawn from it with "Show original", and the "Translate" that asks for it. The options keep the
// agent's words underneath: a chip shows the translation and still answers with the option it stands for.

import { type ReactNode, useRef, useState } from "react";
import { api, type MessageTranslation, type WaitingQuestion } from "../api";
import { Icon } from "../icons";
import { t } from "../i18n";
import { alreadyIn, TranslationLine } from "../msgtranslate";
import { errorText } from "../ui";

/** What the host keeps in a question's `detail.translation`. */
export type AskTranslation = { state: "pending" | "done" | "failed"; from: string; to: string; title?: string; text?: string; options?: string[]; error?: string; asked?: boolean };

function markOf(q: WaitingQuestion): AskTranslation | null {
  const mark = (q.detail as Record<string, unknown> | undefined)?.translation;
  return mark && typeof mark === "object" && typeof (mark as AskTranslation).state === "string" ? (mark as AskTranslation) : null;
}

export type AskTranslated = {
  /** The question as the card draws it: translated words in place of the agent's, or the agent's own. */
  shown: WaitingQuestion;
  /** The words a chip shows for an option; the option itself stays what an answer sends. */
  label: (option: string, index: number) => string;
  line: ReactNode;
  offer: ReactNode;
};

export function useAskTranslation(q: WaitingQuestion): AskTranslated {
  const [answered, setAnswered] = useState<AskTranslation | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [original, setOriginal] = useState(false);
  const running = useRef(false);
  const own = markOf(q);
  const into = q.translate_into ?? "";
  // The host's word once it has a translation; the answer to a request made here until the list is read again.
  const current = own?.state === "done" || !answered ? own : answered;
  const mark: AskTranslation | null = busy ? { state: "pending", from: "", to: into } : error ? { state: "failed", from: "", to: into, error } : current;
  const ask = async () => {
    if (running.current) return;
    running.current = true;
    setBusy(true);
    setError("");
    setOriginal(false);
    try {
      const got = await api.post<{ translation: AskTranslation }>(`/api/asks/${encodeURIComponent(q.id)}/translate`);
      if (got?.translation) setAnswered(got.translation);
    } catch (e) {
      setError(errorText(e));
    } finally {
      running.current = false;
      setBusy(false);
    }
  };
  const showing = mark?.state === "done" && !original;
  const text = showing && mark.text ? mark.text : q.text;
  const title = showing && mark.title ? mark.title : q.title;
  // The heading is the title, or the first line of the text, as the host makes it from the agent's words.
  const heading = showing ? title || text.split("\n").find((line) => line.trim())?.trim() || q.heading : q.heading;
  const asLine: MessageTranslation | null = mark ? { direction: "in", state: mark.state, from: mark.from, to: mark.to, error: mark.error } : null;
  const offered = !mark && !!into && !alreadyIn(`${q.title}\n${q.text}`, into);
  const words = t("translation.ask", { lang: into.toUpperCase() });
  return {
    shown: showing ? { ...q, title, text, heading } : q,
    label: (option, index) => (showing && mark.options?.[index]) || option,
    line: asLine ? <TranslationLine translation={asLine} original={original} onFlip={() => setOriginal(!original)} onRetry={into ? () => void ask() : undefined} /> : null,
    offer: offered ? (
      <button type="button" className="linkbtn q-translate" data-translate="offer" onClick={() => void ask()} aria-label={words} title={words}>
        <Icon name="translate" size={12} />{t("translation.ask.short")}
      </button>
    ) : null,
  };
}
