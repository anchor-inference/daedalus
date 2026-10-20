// Injected into the agent's tabs only. Two jobs: a thin border so the operator can tell an agent tab
// at a glance, and a report of the operator's own input so the daemon hands control to the human.
//
// This file is a classic script, not a module (Chrome injects it with `scripting.executeScript`), so
// it imports nothing and keeps everything inside one function. It may be injected again on every
// navigation start and must notice that it already runs.

(() => {
  const marker = "__daedalusAgentTab";
  const scope = globalThis as unknown as Record<string, unknown>;
  if (scope[marker]) return;
  scope[marker] = true;

  // A custom element name, so that a page's own selectors never match it and `js/page.js` can skip
  // it by tag, and a closed shadow root, so the page's scripts cannot read or restyle what is inside.
  // Pointer events are off: the border must never take a click meant for the page.
  const host = document.createElement("daedalus-agent-border");
  host.setAttribute("aria-hidden", "true");
  host.style.cssText =
    "all:initial;position:fixed;inset:0;pointer-events:none;z-index:2147483647";
  const shadow = host.attachShadow({ mode: "closed" });
  const frame = document.createElement("div");
  frame.style.cssText =
    "position:absolute;inset:0;box-shadow:inset 0 0 0 3px rgba(31,111,235,.85),inset 0 0 18px rgba(31,111,235,.35);pointer-events:none";
  shadow.append(frame);

  const mount = () => {
    if (!host.isConnected && document.documentElement) document.documentElement.appendChild(host);
  };
  // Injected at navigation start, the new document may not have its root element yet.
  if (document.documentElement) mount();
  else document.addEventListener("DOMContentLoaded", mount, { once: true });

  let lastSent = 0;
  const onInput = (event: Event) => {
    // Script-made events are not the operator. The daemon's own `Input.dispatch*` events are trusted
    // too; the worker tells those apart by when the daemon last dispatched into this tab.
    if (!event.isTrusted) return;
    const now = Date.now();
    if (now - lastSent < 500) return;
    lastSent = now;
    chrome.runtime.sendMessage({ kind: "daedalus-input" }).catch(() => undefined);
  };
  const events = ["pointerdown", "keydown", "wheel"];
  for (const type of events) window.addEventListener(type, onInput, { capture: true, passive: true });

  chrome.runtime.onMessage.addListener((message) => {
    if (!message || message.kind !== "daedalus-border") return;
    if (message.visible === null) {
      // The tab left the agent's scope: take everything back out of the page.
      host.remove();
      for (const type of events) window.removeEventListener(type, onInput, { capture: true });
      scope[marker] = false;
      return;
    }
    // Hidden while the daemon takes a screenshot, so the agent sees the page as the page is.
    host.style.opacity = message.visible ? "1" : "0";
  });
})();
