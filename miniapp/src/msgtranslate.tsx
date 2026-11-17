// One of the agent's texts in the operator's language: the line under a translated text ("Translated
// EN → RU · Show original", "Translating…", a failure and its reason), and the "Translate" that asks the
// host for a translation of a text the automatic one left alone — a note written between steps, or an
// answer in a session that translates nothing. The host writes the translation onto the chat's copy of
// the message, never onto what the agent reads back, and answers with the row as the app draws it.

import { type ReactNode, useRef, useState } from "react";
import { api, type MessageTranslation } from "./api";
import { Icon, type IconName } from "./icons";
import { t } from "./i18n";
import { errorText } from "./ui";

/** The script each language is written in, for the ones a script alone tells apart. A language written in
 *  Latin letters shares them with a dozen others, so a text in one of them is always offered. */
const SCRIPTS: Record<string, RegExp> = {
  ru: /\p{Script=Cyrillic}/u,
  uk: /\p{Script=Cyrillic}/u,
  zh: /\p{Script=Han}/u,
  ja: /[\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Han}]/u,
  ko: /\p{Script=Hangul}/u,
};
const LETTER = /\p{L}/u;

/**
 * Whether `text` is plainly written in `language` already, so offering to translate it would be noise.
 * Code, inline code and paths are in no language and are left out of the count: a Russian note about
 * `src/app.ts` is still Russian. `false` whenever the script cannot tell.
 */
export function alreadyIn(text: string, language: string): boolean {
  const script = SCRIPTS[language];
  if (!script) return false;
  const words = text.replace(/```[\s\S]*?```/g, " ").replace(/`[^`\n]*`/g, " ").replace(/\S*[/\\]\S*/g, " ");
  let own = 0;
  let other = 0;
  for (const ch of words) {
    if (!LETTER.test(ch)) continue;
    if (script.test(ch)) own++;
    else other++;
  }
  return own > 0 && own >= other;
}

type Answer = { text: string; translation: MessageTranslation | null };

export type AskedTranslation = {
  /** The text to draw: the translation, or the original when that is what is shown. */
  text: string;
  /** Changes whenever `text` stands for a different side, for a rendering cache keyed by message. */
  key: string;
  /** The line under the text; null when there is nothing to say. */
  line: ReactNode;
  /** "Translate" for a row of actions under the text, when it may be asked for. */
  action: { icon: IconName; label: string; onSelect: () => unknown } | null;
  /** The same "Translate" as a quiet line of its own, for a text with no row of actions. */
  offer: ReactNode;
};

/**
 * The state of one text's translation, from the host's copy and from a request made here.
 *
 * `into` is the operator's language; without it (a host that does not say, a window that is not the
 * conversation's own) nothing is offered, though a translation the host made is still drawn. A request
 * in flight is never sent twice: the button turns into the "Translating…" line, and a second press that
 * slips through is dropped here and again by the host.
 */
export function useAskedTranslation({ sessionId, seq, text, translation, live, into }: { sessionId: string; seq?: number | null; text: string; translation?: MessageTranslation | null; live: boolean; into?: string }): AskedTranslation {
  const [answered, setAnswered] = useState<Answer | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [original, setOriginal] = useState(false);
  const running = useRef(false);
  const own = !live && translation?.direction === "in" ? translation : null;
  // The host's copy, once it has a translation, is the truth; until the chat reads it again the answer
  // to the request stands in for it — a note far up the chat may not be on the page the app rereads.
  const current: Answer = own?.state === "done" || !answered ? { text, translation: own } : answered;
  const shown: MessageTranslation | null = busy
    ? { direction: "in", state: "pending", from: "", to: into ?? "" }
    : error ? { direction: "in", state: "failed", from: "", to: into ?? "", error } : current.translation;
  const ask = async () => {
    if (running.current || !seq) return;
    running.current = true;
    setBusy(true);
    setError("");
    setOriginal(false);
    try {
      const got = await api.post<Answer>(`/api/sessions/${sessionId}/messages/${seq}/translate`);
      if (got.translation) setAnswered({ text: got.text, translation: got.translation });
    } catch (e) {
      setError(errorText(e));
    } finally {
      running.current = false;
      setBusy(false);
    }
  };
  const showingOriginal = original && shown?.state === "done" && !!shown.alternate;
  const offered = !live && !!into && !!seq && !shown && !!text.trim() && !alreadyIn(text, into);
  const label = t("translation.ask", { lang: (into ?? "").toUpperCase() });
  return {
    text: showingOriginal ? shown!.alternate! : current.text,
    key: showingOriginal ? "o" : shown?.state === "done" ? "t" : "",
    line: shown ? <TranslationLine translation={shown} original={showingOriginal} onFlip={() => setOriginal(!original)} onRetry={seq && into && !live ? () => void ask() : undefined} /> : null,
    action: offered ? { icon: "translate", label, onSelect: () => void ask() } : null,
    offer: offered ? (
      <div className="answer-translation offer">
        <button type="button" className="linkbtn" data-translate="offer" onClick={() => void ask()} aria-label={label} title={label}>
          <Icon name="translate" size={13} />{t("translation.ask.short")}
        </button>
      </div>
    ) : null,
  };
}

/** `useAskedTranslation` for a text drawn inside a list, where a hook cannot be called per item. */
export function Translatable({ children, ...props }: Parameters<typeof useAskedTranslation>[0] & { children: (asked: AskedTranslation) => ReactNode }) {
  return <>{children(useAskedTranslation(props))}</>;
}

/** Under a translated text: "translating…" while the translator works, the switch to the original once
 *  it is done, and the failure with its reason and another try when it failed — the original is shown. */
export function TranslationLine({ translation, original, onFlip, onRetry }: { translation: MessageTranslation; original: boolean; onFlip: () => void; onRetry?: () => void }) {
  const to = translation.to.toUpperCase();
  const from = translation.from.toUpperCase();
  if (translation.state === "pending") {
    return <div className="answer-translation" role="status" data-translation="pending"><span className="live-dot" />{t("translation.in.pending", { lang: to })}</div>;
  }
  if (translation.state === "failed") {
    return (
      <div className="answer-translation failed" role="status" data-translation="failed">
        <span>{t("translation.in.failed")}</span>
        {translation.error && <span className="answer-translation-error truncate" title={translation.error}>{translation.error}</span>}
        {onRetry && <button type="button" className="linkbtn" data-translate="retry" onClick={onRetry}>{t("translation.retry")}</button>}
      </div>
    );
  }
  const said = from
    ? t(original ? "translation.in.original" : "translation.in.done", { from, to })
    : t(original ? "translation.in.original.plain" : "translation.in.done.into", { to });
  return (
    <div className="answer-translation" data-translation="done">
      <span className="sub">{said}</span>
      <button type="button" className="linkbtn" aria-pressed={original} onClick={onFlip}>{t(original ? "translation.in.show.translation" : "translation.in.show.original")}</button>
    </div>
  );
}
