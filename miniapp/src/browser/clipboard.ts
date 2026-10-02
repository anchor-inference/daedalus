// The operator's clipboard and the page's, which are two different clipboards: the page runs in a
// browser on the agent's machine, whose clipboard holds nothing of the operator's. A paste reads the
// operator's own and types it onto the page as text; a copy asks the daemon for the page's selection
// and writes it to the operator's own.
//
// Browsers let a page touch the clipboard only in a person's gesture. Safari, and so every iPhone,
// is the strictest: a write must start inside the key press or the tap itself, and the page's answer
// arrives a moment later. A ClipboardItem whose content is a promise starts the write in the gesture
// and fills it when the answer comes; where that is refused, the text is written plainly, and where
// that is refused too, a toast offers a button whose own tap writes it.

import { toast } from "../ui/dialogs";
import { plural, t } from "../i18n";
import { CopyError, type Copied, type LiveView } from "./live";

/**
 * The longest paste sent to the page. It goes as text inputs of a thousand characters each, and the
 * daemon keeps a queue of 64 inputs per viewer, dropping the newest past it: a longer paste would
 * reach the page with holes in it, which is worse than not at all.
 */
export const MAX_PASTE_CHARS = 40_000;

/** Put text from the operator's clipboard onto the page, or say why not. */
export function pasteText(live: LiveView | null, text: string): boolean {
  if (!live || !text) return false;
  if (text.length > MAX_PASTE_CHARS) {
    toast(t("browser.paste.long", { n: MAX_PASTE_CHARS.toLocaleString() }));
    return false;
  }
  return live.input({ t: "text", text });
}

/**
 * The paste button: reads the operator's clipboard, which the browser may refuse or ask about. On a
 * phone the refusal points at the line under the page, where a long press pastes as it does anywhere.
 */
export async function pasteFromClipboard(live: LiveView | null, phone: boolean): Promise<void> {
  let text = "";
  try {
    if (!navigator.clipboard?.readText) throw new Error("no clipboard");
    text = await navigator.clipboard.readText();
  } catch {
    toast(t(phone ? "browser.paste.refused.phone" : "browser.paste.refused"));
    return;
  }
  if (!text) {
    toast(t("browser.paste.empty"));
    return;
  }
  if (pasteText(live, text)) toast(plural("browser.paste.done", text.length));
}

/** Why a copy brought nothing to write: the words the toast says. */
class Nothing extends Error {}

/**
 * Copy the page's selection to the operator's clipboard. Call it inside the gesture — the key press,
 * the tap — and without awaiting anything first, or Safari refuses the write.
 */
export function copyFromPage(live: LiveView | null): Promise<void> {
  if (!live) return Promise.resolve();
  const answer = live.copy();
  const text = answer.then((c) => {
    if (c.withheld || !c.text) throw new Nothing();
    return c.text;
  });
  // The text is awaited only by the write begun below, if one is; a copy that brings nothing must
  // not leave its rejection unheard.
  text.catch(() => undefined);
  const clip = typeof navigator !== "undefined" ? navigator.clipboard : undefined;
  let started: Promise<void> | null = null;
  if (clip?.write && typeof ClipboardItem !== "undefined") {
    try {
      started = clip.write([new ClipboardItem({ "text/plain": text.then((s) => new Blob([s], { type: "text/plain" })) })]);
    } catch {
      started = null;
    }
  }
  return finish(answer, started);
}

async function finish(answer: Promise<Copied>, started: Promise<void> | null): Promise<void> {
  let copied: Copied;
  try {
    copied = await answer;
  } catch (e) {
    started?.catch(() => undefined);
    toast(e instanceof CopyError && e.message === "timeout" ? t("browser.copy.slow") : t("browser.copy.failed"));
    return;
  }
  if (copied.withheld || !copied.text) {
    started?.catch(() => undefined);
    toast(t(copied.withheld ? "browser.copy.withheld" : "browser.copy.empty"));
    return;
  }
  if (await wrote(started, copied.text)) {
    toast(copiedWords(copied));
    return;
  }
  // Both writes refused: the gesture was spent by the time the page answered. A tap on the toast's
  // button is a gesture of its own, and the write inside it is allowed.
  toast(plural("browser.copy.ready", copied.text.length), {
    ms: 12_000,
    action: {
      label: t("browser.copy.action"),
      run: () => {
        void navigator.clipboard?.writeText(copied.text).then(
          () => toast(copiedWords(copied)),
          () => toast(t("browser.copy.refused")),
        );
      },
    },
  });
}

/** Whether the text reached the operator's clipboard: the write begun in the gesture, else a plain one. */
async function wrote(started: Promise<void> | null, text: string): Promise<boolean> {
  if (started) {
    try {
      await started;
      return true;
    } catch {
      /* refused, or a browser whose ClipboardItem takes no promise: the plain write below */
    }
  }
  try {
    if (!navigator.clipboard?.writeText) return false;
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

function copiedWords(c: Copied): string {
  return plural(c.truncated ? "browser.copy.truncated" : "browser.copy.done", c.text.length);
}
