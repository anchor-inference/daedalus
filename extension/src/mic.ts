// The microphone for the side panel, asked once in a tab of its own. A side panel cannot show Chrome's
// permission prompt, so dictation in the panel's frame was refused without a question; the permission
// is the extension's (the frame is let use it through `allow="microphone"`), and a page of the
// extension in an ordinary tab is where Chrome asks for it. Nothing is recorded: the stream is closed
// as soon as it opens.

import { isKey, language, t } from "./i18n.js";

const state = document.getElementById("mic-state") as HTMLElement;
const again = document.getElementById("mic-again") as HTMLButtonElement;

document.documentElement.lang = language();
for (const element of document.querySelectorAll<HTMLElement>("[data-i18n]")) {
  const key = element.dataset.i18n ?? "";
  if (isKey(key)) element.textContent = t(key);
}
document.title = t("mic.title");

async function ask(): Promise<void> {
  again.hidden = true;
  state.textContent = t("mic.asking");
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    for (const track of stream.getTracks()) track.stop();
    state.textContent = t("mic.allowed");
    setTimeout(() => window.close(), 1500);
  } catch {
    state.textContent = t("mic.refused");
    again.hidden = false;
  }
}

again.onclick = () => void ask();
void ask();
